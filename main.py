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

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT",
    "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT"
}

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
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {msg}")
    if exc_info:
        import traceback
        print(traceback.format_exc())

def get_account_info(force=False):
    """총 담보금(잔고+미실현손익) 반환 - total이 비정상적으로 작으면 available로 대체, 여전히 작으면 100 USDT 사용(테스트용)"""
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        log_debug("🔍 API 원시 응답", f"전체 계정 정보: {acc}")

        total_str = str(acc.total) if hasattr(acc, 'total') else "0"
        available_str = str(acc.available) if hasattr(acc, 'available') else "0"
        unrealised_pnl_str = str(acc.unrealised_pnl) if hasattr(acc, 'unrealised_pnl') else "0"
        log_debug("💰 계정 필드", f"total: {total_str}, available: {available_str}, unrealised_pnl: {unrealised_pnl_str}")

        total_equity = Decimal(total_str.upper().replace("E", "e"))
        available_equity = Decimal(available_str.upper().replace("E", "e"))

        if total_equity < Decimal("1"):
            log_debug("⚠️ 담보금 전환", f"total({total_equity}) < 1, available({available_equity}) 사용")
            final_equity = available_equity
        else:
            final_equity = total_equity

        if final_equity < Decimal("10"):
            log_debug("⚠️ 담보금 부족", f"계산된 담보금: {final_equity}, 테스트용 100 USDT로 설정")
            final_equity = Decimal("100")

        account_cache.update({"time": now, "data": final_equity})
        log_debug("💰 최종 선택", f"사용할 담보금: {final_equity}")
        return final_equity

    except Exception as e:
        log_debug("❌ 계정 조회 실패", str(e), exc_info=True)
        return Decimal("100")  # 테스트용 fallback값

def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker or len(ticker) == 0:
            log_debug(f"❌ 티커 데이터 없음 ({symbol})", "")
            return Decimal("0")
        price_str = str(ticker[0].last).upper().replace("E", "e")
        price = Decimal(price_str).normalize()
        log_debug(f"💲 가격 ({symbol})", f"{price}")
        return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def get_max_qty(symbol, side):
    """수량 계산 (디버깅 강화)"""
    try:
        cfg = SYMBOL_CONFIG[symbol]
        total_equity = get_account_info(force=True)
        price = get_price(symbol)

        if price <= 0:
            log_debug(f"❌ 가격 오류 ({symbol})", f"가격: {price}")
            return float(cfg["min_qty"])

        leverage_multiplier = 2
        position_value = total_equity * leverage_multiplier
        contract_size = cfg["contract_size"]

        log_debug(f"📊 계산 1단계 ({symbol})", f"담보금:{total_equity} × 레버리지:{leverage_multiplier} = 포지션가치:{position_value}")
        price_x_contract = price * contract_size
        log_debug(f"📊 계산 2단계 ({symbol})", f"가격:{price} × 계약단위:{contract_size} = {price_x_contract}")

        raw_qty = (position_value / price_x_contract).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
        log_debug(f"📊 계산 3단계 ({symbol})", f"포지션가치:{position_value} ÷ {price_x_contract} = 원시수량:{raw_qty}")

        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        qty = max(qty, cfg["min_qty"])

        log_debug(f"📊 최종 결과 ({symbol})", f"원시수량:{raw_qty} → 정규화:{qty}")
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
        log_debug("📥 진입", f"{symbol} {side} {qty_dec} 계약")
        return True
    except Exception as e:
        error_msg = str(e)
        if retry > 0 and ("INVALID_PARAM" in error_msg or "POSITION_EMPTY" in error_msg):
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // step) * step
            retry_qty = max(retry_qty, min_qty)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        log_debug("❌ 진입 실패", f"{symbol} {side} {qty} - {error_msg}")
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
        log_debug("📤 청산", f"{symbol} 포지션 청산")
        return True
    except Exception as e:
        log_debug("❌ 청산 실패", f"{symbol} - {str(e)}")
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
        log_debug("❌ 웹훅 전체 실패", str(e), exc_info=True)
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
        log_debug("❌ 상태 조회 실패", str(e), exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug_account():
    try:
        acc = api.list_futures_accounts(SETTLE)
        debug_info = {
            "raw_response": str(acc),
            "total": str(acc.total) if hasattr(acc, 'total') else "없음",
            "available": str(acc.available) if hasattr(acc, 'available') else "없음",
            "unrealised_pnl": str(acc.unrealised_pnl) if hasattr(acc, 'unrealised_pnl') else "없음",
            "order_margin": str(acc.order_margin) if hasattr(acc, 'order_margin') else "없음",
            "position_margin": str(acc.position_margin) if hasattr(acc, 'position_margin') else "없음"
        }
        return jsonify(debug_info)
    except Exception as e:
        return jsonify({"error": str(e)})

async def send_ping(ws):
    while True:
        try:
            await ws.ping()
        except:
            break
        await asyncio.sleep(10)

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                asyncio.create_task(send_ping(ws))
                reconnect_delay = 5
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("event") == "subscribe":
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
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        break
                    except:
                        continue
        except Exception as e:
            log_debug("❌ 웹소켓 연결 실패", str(e), exc_info=True)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

def process_ticker_data(ticker):
    try:
        contract = ticker.get("contract")
        last = ticker.get("last")
        if not contract or not last or contract not in SYMBOL_CONFIG:
            return
        try:
            price = Decimal(str(last).replace("E", "e")).normalize()
        except (InvalidOperation, ValueError):
            return
        if not update_position_state(contract, timeout=1):
            return
        pos = position_state.get(contract, {})
        entry = pos.get("price")
        if entry and pos.get("side"):
            cfg = SYMBOL_CONFIG[contract]
            side = pos["side"]
            if side == "buy":
                sl = entry * (1 - cfg["sl_pct"])
                tp = entry * (1 + cfg["tp_pct"])
                if price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가: {price} <= SL: {sl}")
                    close_position(contract)
                elif price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가: {price} >= TP: {tp}")
                    close_position(contract)
            else:
                sl = entry * (1 + cfg["sl_pct"])
                tp = entry * (1 - cfg["tp_pct"])
                if price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가: {price} >= SL: {sl}")
                    close_position(contract)
                elif price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가: {price} <= TP: {tp}")
                    close_position(contract)
    except Exception as e:
        log_debug("❌ 티커 처리 실패", str(e))

def backup_position_loop():
    while True:
        try:
            for sym in SYMBOL_CONFIG:
                update_position_state(sym, timeout=1)
            time.sleep(300)
        except Exception as e:
            log_debug("❌ 백업 루프 오류", str(e))
            time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    threading.Thread(target=backup_position_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
