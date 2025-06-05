import os
import json
import time
import asyncio
import threading
import websockets
import logging
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

# ----------- 로그 필터 및 설정 -----------
class CustomFilter(logging.Filter):
    def filter(self, record):
        filter_keywords = [
            "실시간 가격", "티커 수신", "포지션 없음", "계정 필드",
            "담보금 전환", "최종 선택", "전체 계정 정보"
        ]
        return not any(keyword in record.getMessage() for keyword in filter_keywords)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.addFilter(CustomFilter())
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
console_handler.setFormatter(formatter)
logger.handlers = []
logger.addHandler(console_handler)

def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception(msg)

# ----------- 서버 설정 -----------
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
actual_entry_prices = {}

def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        total_str = str(acc.total) if hasattr(acc, 'total') else "0"
        available_str = str(acc.available) if hasattr(acc, 'available') else "0"
        total_equity = Decimal(total_str.upper().replace("E", "e"))
        available_equity = Decimal(available_str.upper().replace("E", "e"))
        
        # 🔴 실제 사용 가능한 잔고만 사용 (테스트용 fallback 제거)
        final_equity = available_equity if total_equity < Decimal("1") else total_equity
        log_debug("💰 계정 정보", f"total: {total_equity}, available: {available_equity}, 사용: {final_equity}")
        
        account_cache.update({"time": now, "data": final_equity})
        return final_equity
    except Exception as e:
        log_debug("❌ 계정 조회 실패", str(e), exc_info=True)
        return Decimal("0")

def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker or len(ticker) == 0:
            return Decimal("0")
        price_str = str(ticker[0].last).upper().replace("E", "e")
        price = Decimal(price_str).normalize()
        return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def get_real_time_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker or len(ticker) == 0:
            return None
        ticker_data = ticker[0]
        mark_price = getattr(ticker_data, 'mark_price', None)
        last_price = getattr(ticker_data, 'last', None)
        if mark_price:
            price = Decimal(str(mark_price).upper().replace("E", "e")).normalize()
        elif last_price:
            price = Decimal(str(last_price).upper().replace("E", "e")).normalize()
        else:
            return None
        return price
    except Exception as e:
        log_debug(f"❌ 실시간 가격 조회 실패 ({symbol})", str(e))
        return None

def get_max_qty(symbol, side):
    """🔴 수정된 수량 계산 함수"""
    try:
        cfg = SYMBOL_CONFIG[symbol]
        total_equity = get_account_info(force=True)
        price = get_price(symbol)
        
        if price <= 0 or total_equity <= 0:
            log_debug(f"❌ 계산 불가 ({symbol})", f"가격: {price}, 잔고: {total_equity}")
            return float(cfg["min_qty"])
        
        leverage_multiplier = 2
        position_value = total_equity * leverage_multiplier
        contract_size = cfg["contract_size"]
        
        # 🔴 올바른 계산 공식: position_value / (price * contract_size)
        raw_qty = (position_value / (price * contract_size)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        qty = max(qty, cfg["min_qty"])
        
        # 🔴 상세 로그
        log_debug(f"📊 수량 계산 ({symbol})", 
            f"잔고:{total_equity} × 레버:{leverage_multiplier} = 포지션가치:{position_value}")
        log_debug(f"📊 수량 계산 ({symbol})", 
            f"가격:{price} × 계약단위:{contract_size} = {price * contract_size}")
        log_debug(f"📊 수량 계산 ({symbol})", 
            f"최종수량: {position_value} ÷ {price * contract_size} = {qty}")
        
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e), exc_info=True)
        return float(cfg["min_qty"])

def get_actual_fill_price(symbol):
    """체결가 추적: 실제 Gate.io API 사용하도록 수정 필요"""
    try:
        return None
    except Exception as e:
        log_debug("❌ 체결가 조회 실패", str(e))
        return None

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
                if symbol in actual_entry_prices:
                    del actual_entry_prices[symbol]
                return True
            else:
                log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
                return False
        size = Decimal(str(pos.size))
        if size != 0:
            api_entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            actual_price = actual_entry_prices.get(symbol)
            entry_price = actual_price if actual_price else api_entry_price
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            margin = value / SYMBOL_CONFIG[symbol]["leverage"]
            position_state[symbol] = {
                "price": entry_price, "side": "buy" if size > 0 else "sell",
                "size": abs(size), "value": value, "margin": margin,
                "mode": "cross"
            }
        else:
            position_state[symbol] = {
                "price": None, "side": None,
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
            }
            if symbol in actual_entry_prices:
                del actual_entry_prices[symbol]
        return True
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e), exc_info=True)
        return False
    finally:
        position_lock.release()

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 주문 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        min_qty = cfg["min_qty"]
        qty_dec = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        if qty_dec < min_qty:
            log_debug(f"⛔ 잘못된 수량 ({symbol})", f"{qty_dec} < 최소 {min_qty}")
            return False
        
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        log_debug(f"📤 주문 시도 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약, reduce_only={reduce_only}")
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        
        time.sleep(2)
        actual_price = get_actual_fill_price(symbol)
        if actual_price:
            actual_entry_prices[symbol] = actual_price
            log_debug(f"💾 실제 진입가 저장 ({symbol})", f"체결가: {actual_price}")
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", f"{error_msg}")
        if retry > 0 and ("INVALID_PARAM" in error_msg or "POSITION_EMPTY" in error_msg or "INSUFFICIENT_AVAILABLE" in error_msg):
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // step) * step
            retry_qty = max(retry_qty, min_qty)
            log_debug(f"🔄 재시도 ({symbol})", f"{qty} → {retry_qty}")
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False
    finally:
        position_lock.release()

def close_position(symbol):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 청산 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "size=0 주문")
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "")
        if symbol in actual_entry_prices:
            del actual_entry_prices[symbol]
        time.sleep(1)
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
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
        log_debug("🔄 웹훅 시작", "요청 수신")
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
        data = request.get_json()
        log_debug("📥 웹훅 데이터", json.dumps(data))
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        reason = data.get("reason", "")
        if action == "exit" and reason == "reverse_signal":
            success = close_position(symbol)
            log_debug(f"🔁 반대 신호 청산 ({symbol})", f"성공: {success}")
            return jsonify({"status": "success" if success else "error"})
        if side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "Invalid side/action"}), 400
        if not update_position_state(symbol, timeout=1):
            return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
        current_side = position_state.get(symbol, {}).get("side")
        desired_side = "buy" if side == "long" else "sell"
        if action == "exit":
            success = close_position(symbol)
            log_debug(f"🔁 일반 청산 ({symbol})", f"성공: {success}")
            return jsonify({"status": "success" if success else "error"})
        if current_side and current_side != desired_side:
            log_debug("🔄 역포지션 처리", f"현재: {current_side} → 목표: {desired_side}")
            if not close_position(symbol):
                log_debug("❌ 역포지션 청산 실패", "")
                return jsonify({"status": "error", "message": "역포지션 청산 실패"})
            time.sleep(3)
            if not update_position_state(symbol):
                log_debug("❌ 역포지션 후 상태 갱신 실패", "")
        qty = get_max_qty(symbol, desired_side)
        log_debug(f"🧮 수량 계산 완료 ({symbol})", f"{qty} 계약")
        if qty <= 0:
            log_debug("❌ 수량 오류", f"계산된 수량: {qty}")
            return jsonify({"status": "error", "message": "수량 계산 오류"})
        success = place_order(symbol, desired_side, qty)
        log_debug(f"📨 최종 결과 ({symbol})", f"주문 성공: {success}")
        return jsonify({"status": "success" if success else "error", "qty": qty})
    except Exception as e:
        log_debug(f"❌ 웹훅 전체 실패 ({symbol or 'unknown'})", str(e))
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
            "positions": positions,
            "actual_entry_prices": {k: float(v) for k, v in actual_entry_prices.items()}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
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
            log_debug("📡 웹소켓 핑", "핑 전송 성공")
        except Exception as e:
            log_debug("❌ 핑 실패", str(e))
            break
        await asyncio.sleep(30)

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60
    log_debug("📡 웹소켓", f"시작 - URI: {uri}, 심볼: {symbols}")
    while True:
        try:
            log_debug("📡 웹소켓", f"연결 시도: {uri}")
            async with websockets.connect(uri, ping_interval=30, ping_timeout=15) as ws:
                log_debug("📡 웹소켓", f"연결 성공: {uri}")
                subscribe_msg = {
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }
                await ws.send(json.dumps(subscribe_msg))
                log_debug("📡 웹소켓", f"구독 요청 전송: {subscribe_msg}")
                ping_task = asyncio.create_task(send_ping(ws))
                reconnect_delay = 5
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=45)
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
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
        if not contract or not last or contract not in SYMBOL_CONFIG:
            return
        real_price = get_real_time_price(contract)
        if not real_price:
            real_price = Decimal(str(last).replace("E", "e")).normalize()
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
            if side == "buy":
                sl = entry * (1 - cfg["sl_pct"])
                tp = entry * (1 + cfg["tp_pct"])
                if real_price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{real_price} <= SL:{sl} (진입가:{entry})")
                    close_position(contract)
                elif real_price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{real_price} >= TP:{tp} (진입가:{entry})")
                    close_position(contract)
            else:
                sl = entry * (1 + cfg["sl_pct"])
                tp = entry * (1 - cfg["tp_pct"])
                if real_price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{real_price} >= SL:{sl} (진입가:{entry})")
                    close_position(contract)
                elif real_price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{real_price} <= TP:{tp} (진입가:{entry})")
                    close_position(contract)
        finally:
            position_lock.release()
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
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"Railway에서 {port} 포트로 시작")
    app.run(host="0.0.0.0", port=port)
