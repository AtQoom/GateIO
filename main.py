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
    current_time = time.time()
    if not force_refresh and account_cache["time"] > current_time - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        available = Decimal(str(getattr(accounts, 'available', 0)))
        safe_available = available * MARGIN_BUFFER
        log_debug("💰 계정 정보", f"가용: {available:.2f}, 안전가용: {safe_available:.2f} USDT")
        account_cache["time"] = current_time
        account_cache["data"] = safe_available
        return safe_available
    except Exception as e:
        log_debug("❌ 증거금 조회 실패", str(e))
        return Decimal("100") * MARGIN_BUFFER

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = Decimal(str(getattr(pos, 'size', 0)))
        mark_price = Decimal(str(getattr(pos, 'mark_price', 0)))
        leverage = Decimal(str(getattr(pos, 'leverage', 1)))
        if size != 0:
            position_state[symbol] = {
                "price": Decimal(str(getattr(pos, 'entry_price', 0))),
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "leverage": leverage,
                "value": abs(size) * mark_price,
                "margin": (abs(size) * mark_price) / leverage
            }
        else:
            position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1, "value": 0, "margin": 0}
    except Exception as e:
        log_debug(f"❌ 포지션 상태 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1, "value": 0, "margin": 0}

def get_current_price(symbol):
    try:
        tickers = api_instance.list_futures_tickers(settle=SETTLE, contract=symbol)
        if tickers:
            return Decimal(str(tickers[0].last))
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
    return Decimal("0")

def get_max_qty(symbol, side):
    try:
        config = SYMBOL_CONFIG[symbol]
        safe_available = get_account_info()
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        leverage = Decimal(str(state.get("leverage", 1)))
        price = get_current_price(symbol)
        if price <= 0:
            return float(config["min_qty"])

        target_margin = safe_available * ALLOCATION_RATIO
        order_value = target_margin * leverage
        raw_qty = order_value / price

        step = config["qty_step"]
        min_qty = config["min_qty"]
        quantized = (raw_qty / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
        final_qty = max(quantized, min_qty)
        log_debug(f"📐 수량 계산 ({symbol})", f"가격: {price}, 계산된 수량: {final_qty}")
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

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        raw_symbol = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw_symbol, raw_symbol)
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()

        if symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "심볼 오류"}), 400
        if signal not in ["long", "short", "buy", "sell"]:
            return jsonify({"error": "방향 오류"}), 400
        if action not in ["entry", "exit"]:
            return jsonify({"error": "행동 오류"}), 400

        if signal == "buy":
            signal = "long"
        elif signal == "sell":
            signal = "short"

        desired_side = "buy" if signal == "long" else "sell"
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        current_side = state.get("side")

        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "청산 완료"})

        if current_side == desired_side:
            qty = get_max_qty(symbol, desired_side)
            place_order(symbol, desired_side, qty)
        elif current_side and current_side != desired_side:
            close_position(symbol)
            time.sleep(1)
            qty = get_max_qty(symbol, desired_side)
            place_order(symbol, desired_side, qty)
        else:
            qty = get_max_qty(symbol, desired_side)
            place_order(symbol, desired_side, qty)

        return jsonify({"status": "진입 완료", "symbol": symbol, "side": desired_side})
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e))
        return jsonify({"error": "서버 오류"}), 500

def close_position(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size)
        if size == 0:
            return
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 포지션 청산 완료 ({symbol})", f"수량: {size}")
    except Exception as e:
        log_debug(f"❌ 포지션 청산 실패 ({symbol})", str(e))

@app.route("/ping", methods=["GET"])
def ping():
    return "pong"

if __name__ == "__main__":
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
