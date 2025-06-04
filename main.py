import os
import json
import time
import asyncio
import threading
import websockets
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

# 심볼 매핑 (Binance → Gate.io)
BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT",
    "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT"
}

# 계약 사양 설정 (모든 코인 레버리지 2배)
SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.0001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10000"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    }
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}

def log_debug(tag, msg, exc_info=False):
    if "포지션 없음" in msg: return
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {msg}")
    if exc_info:
        import traceback
        print(traceback.format_exc())

def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        total_str = str(acc.total).upper().replace("E", "e")
        total_equity = Decimal(total_str)
        account_cache.update({"time": now, "data": total_equity})
        log_debug("💰 계정", f"총 담보금: {total_equity.normalize()}")
        return total_equity
    except Exception as e:
        log_debug("❌ 계정 조회 실패", str(e), exc_info=True)
        return Decimal("0")

def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        price_str = str(ticker[0].last).upper().replace("E", "e")
        price = Decimal(price_str).normalize()
        log_debug(f"💲 가격 ({symbol})", f"{price}")
        return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def get_max_qty(symbol, side):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        total_equity = get_account_info(force=True)
        price = get_price(symbol)
        
        if total_equity <= 0 or price <= 0:
            return float(cfg["min_qty"])

        position_value = total_equity * 2
        contract_size = cfg["contract_size"]
        raw_qty = (position_value / (price * contract_size)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
        
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        qty = max(qty, cfg["min_qty"])
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e), exc_info=True)
        return float(cfg["min_qty"])

def update_position_state(symbol, timeout=5):
    acquired = position_lock.acquire(timeout=timeout)
    if not acquired:
        return False
    try:
        try:
            pos = api.get_position(SETTLE, symbol)
        except Exception as e:
            if "POSITION_NOT_FOUND" in str(e):
                position_state[symbol] = {
                    "price": None, "side": None,
                    "size": Decimal("0"), "value": Decimal("0"), 
                    "margin": Decimal("0"), "mode": "cross"
                }
                return True
            else:
                return False
        
        size = Decimal(str(pos.size))
        if size != 0:
            entry = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            contract_size = SYMBOL_CONFIG[symbol]["contract_size"]
            leverage = SYMBOL_CONFIG[symbol]["leverage"]
            
            position_state[symbol] = {
                "price": entry,
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "value": abs(size) * mark * contract_size,
                "margin": (abs(size) * mark * contract_size) / leverage,
                "mode": "cross"
            }
        else:
            position_state[symbol] = {
                "price": None, "side": None,
                "size": Decimal("0"), "value": Decimal("0"), 
                "margin": Decimal("0"), "mode": "cross"
            }
        return True
    finally:
        position_lock.release()

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        return False
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        min_qty = cfg["min_qty"]

        qty_dec = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        if qty_dec < min_qty:
            return False

        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        api.create_futures_order(SETTLE, order)
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        if retry > 0 and ("INVALID_PARAM" in error_msg or "POSITION_EMPTY" in error_msg):
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // step) * step
            retry_qty = max(retry_qty, min_qty)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False
    finally:
        position_lock.release()

def close_position(symbol):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        return False
    try:
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        return False
    finally:
        position_lock.release()

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    symbol = None
    try:
        data = request.get_json()
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400
            
        action = data.get("action", "").lower()
        reason = data.get("reason", "")
        
        if action == "exit" and reason == "reverse_signal":
            success = close_position(symbol)
            return jsonify({"status": "success" if success else "error"})
            
        side = data.get("side", "").lower()
        if side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "Invalid side/action"}), 400
            
        if not update_position_state(symbol, timeout=1):
            return jsonify({"status": "error", "message": "Position update failed"}), 500
            
        if action == "exit":
            success = close_position(symbol)
            return jsonify({"status": "success" if success else "error"})
            
        current_side = position_state.get(symbol, {}).get("side")
        desired_side = "buy" if side == "long" else "sell"
        
        if current_side and current_side != desired_side:
            if not close_position(symbol):
                return jsonify({"status": "error", "message": "Reverse position close failed"}), 500
            time.sleep(1)
            if not update_position_state(symbol):
                return jsonify({"status": "error", "message": "Position update failed after close"}), 500
        
        qty = get_max_qty(symbol, desired_side)
        if qty <= 0:
            return jsonify({"status": "error", "message": "Invalid quantity"}), 400
            
        success = place_order(symbol, desired_side, qty)
        return jsonify({"status": "success" if success else "error", "qty": qty})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            if update_position_state(sym, timeout=1):
                pos = position_state.get(sym, {})
                if pos.get("side"):
                    positions[sym] = {k: float(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": positions
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug_account():
    try:
        acc = api.list_futures_accounts(SETTLE)
        return jsonify({
            "raw_response": str(acc),
            "total": str(acc.total) if hasattr(acc, 'total') else "없음",
            "available": str(acc.available) if hasattr(acc, 'available') else "없음"
        })
    except Exception as e:
        return jsonify({"error": str(e)})

async def send_ping(ws):
    while True:
        try:
            await ws.ping()
            log_debug("📡 웹소켓 핑", "핑 전송 성공")
        except Exception as e:
            log_debug("❌ 핑 실패", str(e))
            break
        await asyncio.sleep(10)

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60
    
    log_debug("📡 웹소켓", f"시작 - URI: {uri}, 심볼: {symbols}")
    
    while True:
        try:
            log_debug("📡 웹소켓", f"연결 시도: {uri}")
            async with websockets.connect(uri, ping_interval=30, ping_timeout=10) as ws:
                log_debug("📡 웹소켓", f"연결 성공: {uri}")
                
                # 🔴 수정된 구독 메시지
                subscribe_msg = {
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe", 
                    "payload": symbols
                }
                
                await ws.send(json.dumps(subscribe_msg))
                log_debug("📡 웹소켓", f"구독 요청 전송: {subscribe_msg}")
                
                # 핑 태스크 시작
                ping_task = asyncio.create_task(send_ping(ws))
                reconnect_delay = 5
                
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        try:
                            data = json.loads(msg)
                            log_debug("📡 웹소켓 수신", f"메시지: {data}")
                        except json.JSONDecodeError:
                            log_debug("⚠️ 웹소켓", f"JSON 파싱 실패: {msg}")
                            continue
                            
                        if not isinstance(data, dict):
                            continue
                            
                        if data.get("event") == "subscribe":
                            log_debug("✅ 웹소켓 구독", f"채널: {data.get('channel')}")
                            continue
                            
                        result = data.get("result")
                        if not result:
                            continue
                            
                        if isinstance(result, list):
                            for item in result:
                                if isinstance(item, dict):
                                    process_ticker_data(item)
                        elif isinstance(result, dict):
                            process_ticker_data(result)
                            
                    except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                        log_debug("⚠️ 웹소켓", f"연결 끊김: {str(e)}, 재연결 시도")
                        ping_task.cancel()
                        break
                    except Exception as e:
                        log_debug("❌ 웹소켓 메시지 처리", f"오류: {str(e)}")
                        continue
                        
        except Exception as e:
            log_debug("❌ 웹소켓 연결 실패", f"오류: {str(e)}")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

def process_ticker_data(ticker):
    try:
        contract = ticker.get("contract")
        last = ticker.get("last")
        
        log_debug("📊 티커 수신", f"계약: {contract}, 가격: {last}")
        
        if not contract or not last or contract not in SYMBOL_CONFIG:
            return
            
        price = Decimal(str(last).replace("E", "e")).normalize()
        
        acquired = position_lock.acquire(timeout=1)
        if not acquired:
            return
            
        try:
            if not update_position_state(contract, timeout=1):
                return
                
            pos = position_state.get(contract, {})
            entry = pos.get("price")
            size = pos.get("size", 0)
            side = pos.get("side")
            
            if not entry or size <= 0 or side not in ["buy", "sell"]:
                return
                
            cfg = SYMBOL_CONFIG[contract]
            
            # TP/SL 계산 및 로깅
            if side == "buy":
                sl = entry * (1 - cfg["sl_pct"])
                tp = entry * (1 + cfg["tp_pct"])
                log_debug(f"📊 TP/SL 체크 ({contract})", 
                    f"롱 포지션 - 현재가:{price}, 진입가:{entry}, SL:{sl}, TP:{tp}")
                if price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{price} <= SL:{sl}")
                    close_position(contract)
                elif price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{price} >= TP:{tp}")
                    close_position(contract)
            else:
                sl = entry * (1 + cfg["sl_pct"])
                tp = entry * (1 - cfg["tp_pct"])
                log_debug(f"📊 TP/SL 체크 ({contract})", 
                    f"숏 포지션 - 현재가:{price}, 진입가:{entry}, SL:{sl}, TP:{tp}")
                if price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{price} >= SL:{sl}")
                    close_position(contract)
                elif price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{price} <= TP:{tp}")
                    close_position(contract)
        finally:
            position_lock.release()
            
    except Exception as e:
        log_debug("❌ 티커 처리 실패", f"계약:{contract}, 오류:{str(e)}")

def backup_position_loop():
    while True:
        try:
            for sym in SYMBOL_CONFIG:
                update_position_state(sym, timeout=1)
            time.sleep(300)
        except:
            time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    threading.Thread(target=backup_position_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
