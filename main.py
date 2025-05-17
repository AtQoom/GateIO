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
MARGIN_BUFFER = Decimal("0.7")
ALLOCATION_RATIO = Decimal("0.33")

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": Decimal("10"), "qty_step": Decimal("10"), "sl_pct": Decimal("0.0075")},
    "BTC_USDT": {"min_qty": Decimal("0.0001"), "qty_step": Decimal("0.0001"), "sl_pct": Decimal("0.004")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "sl_pct": Decimal("0.0075")}
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
        size = Decimal(str(pos.size))
        price = Decimal(str(pos.entry_price))
        side = "buy" if size > 0 else "sell" if size < 0 else None
        if size != 0:
            position_state[symbol] = {"price": price, "side": side}
        else:
            position_state[symbol] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 포지션 상태 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None}

def get_current_price(symbol):
    try:
        tickers = api_instance.list_futures_tickers(settle=SETTLE, contract=symbol)
        if tickers:
            return Decimal(str(tickers[0].last))
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
    return Decimal("0")

def get_max_qty(symbol, desired_side):
    try:
        config = SYMBOL_CONFIG[symbol]
        safe_equity = get_account_info()
        price = get_current_price(symbol)
        if price <= 0:
            return float(config["min_qty"])

        leverage = Decimal("3")  # 기본 레버리지 3x 가정
        order_value = safe_equity * ALLOCATION_RATIO * leverage
        raw_qty = order_value / price
        step = config["qty_step"]
        min_qty = config["min_qty"]
        quantized = (raw_qty / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
        final_qty = max(quantized, min_qty)
        log_debug(f"📐 수량 계산 ({symbol})", f"가격: {price}, 수량: {final_qty}")
        return float(final_qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e))
        return float(SYMBOL_CONFIG[symbol]["min_qty"])

def place_order(symbol, side, qty, reduce_only=False):
    try:
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", result.to_dict())
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol})", str(e))
        return False

def close_position(symbol):
    try:
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 포지션 청산 완료 ({symbol})", "")
    except Exception as e:
        log_debug(f"❌ 포지션 청산 실패 ({symbol})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    payload = [v for v in SYMBOL_CONFIG]
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "time": int(time.time()),
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": payload
        }))
        log_debug("📡 WebSocket 연결 성공", f"{payload=}")
        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                if "result" not in data:
                    continue
                result = data["result"]
                symbol = result.get("contract")
                price = Decimal(str(result.get("last", 0)))
                if not symbol or price <= 0:
                    continue
                state = position_state.get(symbol, {})
                entry_price = state.get("price")
                side = state.get("side")
                if not entry_price or not side:
                    continue
                sl_pct = SYMBOL_CONFIG[symbol]["sl_pct"]
                if (side == "buy" and price <= entry_price * (1 - sl_pct)) or \
                   (side == "sell" and price >= entry_price * (1 + sl_pct)):
                    log_debug(f"🛑 손절 조건 충족 ({symbol})", f"현재가: {price}, 진입가: {entry_price}")
                    close_position(symbol)
            except Exception as e:
                log_debug("❌ WebSocket 오류", str(e))
                await asyncio.sleep(5)

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
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
