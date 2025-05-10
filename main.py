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
SYMBOL = "ADA_USDT"
SETTLE = "usdt"
MIN_QTY = 10
QTY_STEP = 10
STOP_LOSS_PCT = 0.0075
RISK_PCT = 1.0  # ✅ 추가: 자산의 100% 리스크 설정

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

entry_price = None
entry_side = None

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

def place_order(side, qty, reduce_only=False):
    global entry_price, entry_side
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
    except Exception as e:
        log_debug("❌ 주문 실패", str(e))

def update_position_state():
    global entry_price, entry_side
    try:
        pos = api_instance.get_position(SETTLE, SYMBOL)
        size = float(pos.size)
        if size > 0:
            entry_price = float(pos.entry_price)
            entry_side = "buy"
        elif size < 0:
            entry_price = float(pos.entry_price)
            entry_side = "sell"
        else:
            entry_price, entry_side = None, None
    except Exception as e:
        log_debug("❌ 포지션 감지 실패", str(e))

async def price_listener():
    global entry_price, entry_side
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
                if entry_price is None or entry_side is None:
                    continue
                sl_hit = (
                    (entry_side == "buy" and price <= entry_price * (1 - STOP_LOSS_PCT)) or
                    (entry_side == "sell" and price >= entry_price * (1 + STOP_LOSS_PCT))
                )
                if sl_hit:
                    log_debug("🛑 손절 조건 충족", f"{price=}, {entry_price=}")
                    place_order("sell" if entry_side == "buy" else "buy", qty=MIN_QTY, reduce_only=True)
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
        action = data.get("action", "").lower()

        if signal not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "invalid signal"}), 400

        update_position_state()
        if action == "exit":
            if entry_side == "buy":
                place_order("sell", MIN_QTY, reduce_only=True)
            elif entry_side == "sell":
                place_order("buy", MIN_QTY, reduce_only=True)
            entry_price, entry_side = None, None
            return jsonify({"status": "청산 완료"})

        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int(equity * RISK_PCT / price), MIN_QTY)
        side = "buy" if signal == "long" else "sell"
        place_order(side, qty)
        return jsonify({"status": "진입 완료", "side": side, "qty": qty})
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
