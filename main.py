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
SYMBOL = "SOL_USDT"
SETTLE = "usdt"
RISK_PCT = 1.0
MIN_QTY = 1

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

entry_price = None
entry_side = None
peak_price = None
floor_price = None

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_equity():
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        return float(accounts.available)
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return 0

def get_market_price():
    try:
        tickers = api_instance.list_futures_tickers(settle=SETTLE)
        for t in tickers:
            if t.contract == SYMBOL:
                return float(t.last)
        return 0
    except Exception as e:
        log_debug("❌ 시세 조회 실패", str(e))
        return 0

def place_order(side, qty=1, reduce_only=False):
    global entry_price, entry_side, peak_price, floor_price
    try:
        size = qty if side == "buy" else -qty
        if reduce_only:
            size = -size
        order = FuturesOrder(contract=SYMBOL, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug("✅ 주문 성공", result.to_dict())

        if not reduce_only:
            entry_price = float(result.fill_price or 0)
            entry_side = side
            peak_price = entry_price
            floor_price = entry_price
    except Exception as e:
        log_debug("❌ 주문 실패", str(e))

def get_trailing_pct(profit_ratio):
    if profit_ratio >= 0.04:
        return 0.018
    elif profit_ratio >= 0.03:
        return 0.015
    elif profit_ratio >= 0.02:
        return 0.010
    elif profit_ratio >= 0.01:
        return 0.008
    else:
        return 0.006

def update_position_state():
    global entry_price, entry_side, peak_price, floor_price
    try:
        pos = api_instance.get_position(SETTLE, SYMBOL)
        size = float(pos.size)
        if size > 0:
            entry_price = float(pos.entry_price)
            entry_side = "buy"
            peak_price = entry_price if peak_price is None else max(peak_price, entry_price)
        elif size < 0:
            entry_price = float(pos.entry_price)
            entry_side = "sell"
            floor_price = entry_price if floor_price is None else min(floor_price, entry_price)
        else:
            entry_price, entry_side, peak_price, floor_price = None, None, None, None
    except Exception as e:
        log_debug("❌ 포지션 감지 실패", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "time": int(time.time()),
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": [SYMBOL]
        }))
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            if 'result' in data and isinstance(data['result'], dict):
                price = float(data['result'].get("last", 0))
                update_position_state()
                if not (entry_price and entry_side):
                    continue

                if entry_side == "buy":
                    global peak_price
                    peak_price = max(peak_price, price)
                    profit_ratio = (peak_price / entry_price) - 1
                    trail_pct = get_trailing_pct(profit_ratio)
                    trail_price = peak_price * (1 - trail_pct)

                    if price <= entry_price * 0.994 or price >= entry_price * 1.022:
                        log_debug("🎯 롱 TP/SL", f"{price=}, {entry_price=}")
                        place_order("sell", reduce_only=True)
                        entry_price, entry_side = None, None
                    elif price <= trail_price:
                        log_debug("🐾 롱 트레일링 청산", f"{price=}, trail={trail_price}")
                        place_order("sell", reduce_only=True)
                        entry_price, entry_side = None, None

                elif entry_side == "sell":
                    global floor_price
                    floor_price = min(floor_price, price)
                    profit_ratio = (entry_price / floor_price) - 1
                    trail_pct = get_trailing_pct(profit_ratio)
                    trail_price = floor_price * (1 + trail_pct)

                    if price >= entry_price * 1.006 or price <= entry_price * 0.978:
                        log_debug("🎯 숏 TP/SL", f"{price=}, {entry_price=}")
                        place_order("buy", reduce_only=True)
                        entry_price, entry_side = None, None
                    elif price >= trail_price:
                        log_debug("🐾 숏 트레일링 청산", f"{price=}, trail={trail_price}")
                        place_order("buy", reduce_only=True)
                        entry_price, entry_side = None, None

def start_price_listener():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json(force=True)
        signal = data.get("side", "").lower()
        if signal not in ["long", "short"]:
            return jsonify({"error": "invalid signal"}), 400

        if entry_side == "buy":
            place_order("sell", qty=1, reduce_only=True)
        elif entry_side == "sell":
            place_order("buy", qty=1, reduce_only=True)

        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int(equity * RISK_PCT / price), MIN_QTY)
        side = "buy" if signal == "long" else "sell"
        place_order(side, qty)
        return jsonify({"status": "주문 완료", "side": side, "qty": qty})
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "WebSocket 감시 쓰레드 실행")
    threading.Thread(target=start_price_listener, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
