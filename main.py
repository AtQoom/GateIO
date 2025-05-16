
import os
import json
import time
import threading
import asyncio
import websockets
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
STOP_LOSS_PCT = 0.0075

# 심볼 설정: 최소 수량, 수량 단위
SYMBOL_CONFIG = {
    "ADAUSDT": {"symbol": "ADA_USDT", "min_qty": 10, "qty_step": 10},
    "BTCUSDT": {"symbol": "BTC_USDT", "min_qty": 0.0001, "qty_step": 0.0001},
    "SUIUSDT": {"symbol": "SUI_USDT", "min_qty": 1, "qty_step": 1},
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

position_state = {}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def update_position_state(symbol_key):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size)
        if size != 0:
            position_state[symbol_key] = {
                "price": float(pos.entry_price),
                "side": "buy" if size > 0 else "sell"
            }
        else:
            position_state[symbol_key] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 포지션 감지 실패 ({symbol_key})", str(e))
        position_state[symbol_key] = {"price": None, "side": None}

def get_equity():
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        return float(accounts.available)
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return 0

def get_max_qty(symbol_key):
    try:
        config = SYMBOL_CONFIG[symbol_key]
        symbol = config["symbol"]
        available = get_equity() / 3
        pos = api_instance.get_position(SETTLE, symbol)
        leverage = float(pos.leverage)
        mark_price = float(pos.mark_price)
        raw_qty = available * leverage / mark_price
        step = config["qty_step"]
        min_qty = config["min_qty"]
        qty = max((int(raw_qty / step) * step), min_qty)
        log_debug(f"📈 {symbol_key} 진입 수량", f"{qty=}, {leverage=}, {available=}, {mark_price=}, {raw_qty=}")
        return qty
    except Exception as e:
        log_debug(f"❌ {symbol_key} 최대 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol_key]["min_qty"]

def place_order(symbol_key, side, qty, reduce_only=False):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        size = qty if side == "buy" else -qty
        if reduce_only:
            size = -size
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 {symbol_key}", result.to_dict())
        if not reduce_only:
            position_state[symbol_key] = {"price": float(result.fill_price or 0), "side": side}
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol_key})", str(e))

def close_position(symbol_key):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        pos = api_instance.get_position(SETTLE, symbol)
        if float(pos.size) == 0:
            log_debug(f"📭 {symbol_key} 포지션 없음", "청산할 포지션이 없습니다.")
            return
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 전체 청산 성공 ({symbol_key})", result.to_dict())
        position_state[symbol_key] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 전체 청산 실패 ({symbol_key})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = [v["symbol"] for v in SYMBOL_CONFIG.values()]
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "time": int(time.time()),
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": symbols
        }))
        while True:
            msg = await ws.recv()
            data = json.loads(msg)

            if 'result' not in data or not isinstance(data['result'], dict):
                continue

            symbol = data["result"]["contract"]
            price = float(data["result"]["last"])

            if symbol not in symbol_states:
                continue

            state = symbol_states[symbol]
            update_position_state(symbol)

            entry_price = state["entry_price"]
            entry_side = state["entry_side"]

            if entry_price is None or entry_side is None:
                continue

            log_debug(f"📡 {symbol} 가격 수신", f"{price=}, {entry_price=}, {entry_side=}")

            sl_hit = (
                (entry_side == "buy" and price <= entry_price * (1 - STOP_LOSS_PCT)) or
                (entry_side == "sell" and price >= entry_price * (1 + STOP_LOSS_PCT))
            )

            if sl_hit:
                log_debug(f"🛑 {symbol} 손절 조건 충족", f"{price=}, {entry_price=}")
                close_position(symbol)

def start_price_listener(symbol_key):
    update_position_state(symbol_key)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener(symbol_key))

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        symbol_input = data.get("symbol", "").upper()
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()

        if signal == "buy":
            signal = "long"
        elif signal == "sell":
            signal = "short"

        if signal not in ["long", "short"] or action not in ["entry", "exit"] or symbol_input not in SYMBOL_CONFIG:
            return jsonify({"error": "invalid input"}), 400

        symbol_key = symbol_input
        update_position_state(symbol_key)
        state = position_state.get(symbol_key, {"side": None})
        entry_side = state["side"]

        if action == "exit":
            close_position(symbol_key)
            return jsonify({"status": f"{symbol_key} 청산 완료"})

        qty = get_max_qty(symbol_key)
        side = "buy" if signal == "long" else "sell"

        if (signal == "long" and entry_side == "sell") or (signal == "short" and entry_side == "buy"):
            log_debug(f"🔄 반대 포지션 감지 ({symbol_key})", f"{entry_side=}, 반대 방향으로 진입 시도 → 청산")
            close_position(symbol_key)
            time.sleep(1)

        place_order(symbol_key, side, qty)
        return jsonify({"status": f"{symbol_key} 진입 완료", "side": side, "qty": qty})
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    for sym in SYMBOL_CONFIG.keys():
        threading.Thread(target=start_price_listener, args=(sym,), daemon=True).start()
    log_debug("🚀 서버 시작", "모든 WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
