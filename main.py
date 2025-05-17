import os
import json
import time
import asyncio
import threading
import websockets
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
MARGIN_BUFFER = Decimal("0.6")  # 60%만 사용 (더 보수적으로)
ALLOCATION_RATIO = Decimal("0.33")

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": Decimal("10"), "qty_step": Decimal("10"), "sl_pct": Decimal("0.0075"), "min_order_usdt": Decimal("1")},
    "BTC_USDT": {"min_qty": Decimal("0.0001"), "qty_step": Decimal("0.0001"), "sl_pct": Decimal("0.004"), "min_order_usdt": Decimal("1")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "sl_pct": Decimal("0.0075"), "min_order_usdt": Decimal("1")}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

position_state = {}
account_cache = {"time": 0, "data": None}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_account_info(force_refresh=False):
    now = time.time()
    if not force_refresh and account_cache["time"] > now - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        available = Decimal(str(accounts.available))
        safe_available = available * MARGIN_BUFFER
        account_cache.update({"time": now, "data": safe_available})
        log_debug("💰 계정 정보", f"가용: {available:.2f}, 안전가용: {safe_available:.2f}")
        return safe_available
    except Exception as e:
        log_debug("❌ 증거금 조회 실패", str(e))
        return Decimal("100")

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = Decimal(str(getattr(pos, 'size', '0')))
        if size != 0:
            position_state[symbol] = {
                "price": Decimal(str(getattr(pos, 'entry_price', '0'))),
                "side": "buy" if size > 0 else "sell"
            }
            log_debug(f"📊 포지션 상태 ({symbol})", f"수량: {abs(size)}, 가격: {position_state[symbol]['price']}")
        else:
            position_state[symbol] = {"price": None, "side": None}
            log_debug(f"📊 포지션 상태 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 상태 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None}

def get_current_price(symbol):
    try:
        tickers = api_instance.list_futures_tickers(settle=SETTLE, contract=symbol)
        if tickers and hasattr(tickers[0], 'last'):
            return Decimal(str(tickers[0].last))
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
    return Decimal("0")

def get_max_qty(symbol, desired_side):
    """최소 주문금액, 최소수량, 10의 배수, 증거금 버퍼 모두 반영"""
    try:
        config = SYMBOL_CONFIG[symbol]
        safe_equity = get_account_info()
        price = get_current_price(symbol)
        if price <= 0:
            return float(config["min_qty"])

        leverage = Decimal("3")  # 기본 레버리지 3x 가정
        order_value = safe_equity * ALLOCATION_RATIO * leverage

        # 최소 주문금액 보장 (ex. ADA 10개 x 0.75 ≒ 7.5 USDT, 최소 1 USDT 이상)
        min_order_usdt = config.get("min_order_usdt", Decimal("1"))
        min_qty_by_usdt = (min_order_usdt / price).quantize(config["qty_step"], rounding=ROUND_DOWN)
        min_qty = max(config["min_qty"], min_qty_by_usdt)

        # 수량 계산 및 10의 배수 등 단위 내림
        step = config["qty_step"]
        raw_qty = (order_value / price).quantize(step, rounding=ROUND_DOWN)
        quantized = (raw_qty / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
        final_qty = max(quantized, min_qty)

        # 10의 배수, 0.0001의 배수 등으로 강제 내림
        if step >= 1:
            final_qty = (final_qty // step) * step
        else:
            final_qty = (final_qty // step) * step

        log_debug(f"📐 수량 계산 ({symbol})", f"가격: {price}, 수량: {final_qty}, 최소: {min_qty}, 단위: {step}")
        return float(final_qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e))
        return float(SYMBOL_CONFIG[symbol]["min_qty"])

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    if qty <= 0:
        log_debug(f"⚠️ 주문 무시 ({symbol})", "수량이 0 이하")
        return False
    try:
        api_instance.set_futures_leverage(SETTLE, symbol, leverage=3)
        
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"수량: {size}")
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", error_msg)
        # 증거금 부족/단위 오류 시 수량 40% 감소 후 재시도 (최소 단위 보장)
        if retry > 0 and ("INSUFFICIENT_AVAILABLE" in error_msg or 
                          "INVALID_PARAM_VALUE" in error_msg or 
                          "Bad Request" in error_msg):
            config = SYMBOL_CONFIG[symbol]
            step = config["qty_step"]
            reduced_qty = max((Decimal(str(qty)) * Decimal("0.6") // step) * step, config["min_qty"])
            if reduced_qty >= config["min_qty"]:
                log_debug(f"🔄 주문 재시도 ({symbol})", f"수량 감소: {qty} → {reduced_qty}")
                time.sleep(1)
                return place_order(symbol, side, float(reduced_qty), reduce_only, retry-1)
        return False

def close_position(symbol):
    try:
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 포지션 청산 완료 ({symbol})", "")
        position_state[symbol] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 포지션 청산 실패 ({symbol})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                log_debug("📡 WebSocket 연결 성공", f"구독: {symbols}")
                last_ping_time = time.time()
                last_msg_time = time.time()
                while True:
                    try:
                        current_time = time.time()
                        if current_time - last_ping_time > 30:
                            await ws.send(json.dumps({
                                "time": int(current_time),
                                "channel": "futures.ping",
                                "event": "ping",
                                "payload": []
                            }))
                            last_ping_time = current_time
                        if current_time - last_msg_time > 300:
                            log_debug("⚠️ 오랜 시간 메시지 없음", "연결 재설정")
                            break
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        last_msg_time = time.time()
                        data = json.loads(msg)
                        if 'event' in data:
                            if data['event'] == 'subscribe':
                                log_debug("✅ WebSocket 구독완료", f"{data.get('channel')}")
                            continue
                        if "result" in data and isinstance(data["result"], dict):
                            contract = data["result"].get("contract")
                            last_price = data["result"].get("last")
                            if not contract or contract not in SYMBOL_CONFIG or not last_price:
                                continue
                            price = Decimal(str(last_price))
                            state = position_state.get(contract, {})
                            entry_price = state.get("price")
                            side = state.get("side")
                            if not entry_price or not side:
                                continue
                            sl_pct = SYMBOL_CONFIG[contract]["sl_pct"]
                            if (side == "buy" and price <= entry_price * (Decimal("1") - sl_pct)) or \
                               (side == "sell" and price >= entry_price * (Decimal("1") + sl_pct)):
                                log_debug(f"🛑 손절 조건 충족 ({contract})", f"현재가: {price}, 진입가: {entry_price}")
                                close_position(contract)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        log_debug("❌ WebSocket 메시지 처리 오류", str(e))
                        continue
        except Exception as e:
            log_debug("❌ WebSocket 연결 오류", str(e))
            await asyncio.sleep(5)

def start_price_listener():
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        raw_symbol = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw_symbol, raw_symbol)
        action = data.get("action", "").lower()
        side = data.get("side", "").lower()
        if side in ["buy"]: side = "long"
        if side in ["sell"]: side = "short"
        if symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "알 수 없는 심볼"}), 400
        if side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "유효하지 않은 요청"}), 400
        desired = "buy" if side == "long" else "sell"
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        current = state.get("side")
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "청산 완료"})
        if current == desired:
            qty = get_max_qty(symbol, desired)
            place_order(symbol, desired, qty)
        elif current and current != desired:
            close_position(symbol)
            time.sleep(1)
            qty = get_max_qty(symbol, desired)
            place_order(symbol, desired, qty)
        else:
            qty = get_max_qty(symbol, desired)
            place_order(symbol, desired, qty)
        return jsonify({"status": "진입 완료", "symbol": symbol, "side": desired})
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong"

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
