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
RISK_PCT = 0.4
MIN_QTY = 10
STOP_LOSS_PCT = 0.008  # 0.8%

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(Configuration(key=API_KEY, secret=API_SECRET))
api_instance = FuturesApi(client)

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

def get_position():
    try:
        pos = api_instance.get_position(SETTLE, SYMBOL)
        return float(pos.size), float(pos.entry_price)
    except Exception as e:
        log_debug("❌ 포지션 조회 실패", str(e))
        return 0, 0

def place_order(side, qty=10, reduce_only=False):
    global entry_side
    try:
        size = qty if side == "buy" else -qty
        if reduce_only:
            size = -size
        order = FuturesOrder(contract=SYMBOL, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug("✅ 주문 성공", result.to_dict())

        if not reduce_only:
            entry_side = side
    except Exception as e:
        log_debug("❌ 주문 실패", str(e))

def check_stop_loss():
    global entry_side
    try:
        size, entry_price = get_position()
        if size == 0 or entry_price == 0:
            entry_side = None
            return
        price = get_market_price()
        if entry_side == "buy" and price <= entry_price * (1 - STOP_LOSS_PCT):
            log_debug("🔻 롱 손절 실행", f"{price=}, {entry_price=}")
            place_order("sell", qty=10, reduce_only=True)
            entry_side = None
        elif entry_side == "sell" and price >= entry_price * (1 + STOP_LOSS_PCT):
            log_debug("🔻 숏 손절 실행", f"{price=}, {entry_price=}")
            place_order("buy", qty=10, reduce_only=True)
            entry_side = None
    except Exception as e:
        log_debug("❌ 손절 검사 실패", str(e))

async def price_watcher():
    while True:
        check_stop_loss()
        await asyncio.sleep(2)

def start_price_watcher():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_watcher())

@app.route("/", methods=["POST"])
def webhook():
    global entry_side
    try:
        data = request.get_json(force=True)
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()

        if signal not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "invalid signal"}), 400

        if action == "exit":
            if signal == "long":
                place_order("sell", qty=10, reduce_only=True)
            elif signal == "short":
                place_order("buy", qty=10, reduce_only=True)
            entry_side = None
        else:
            equity = get_equity()
            price = get_market_price()
            if equity == 0 or price == 0:
                return jsonify({"error": "잔고 또는 시세 오류"}), 500

            qty = max(int(equity * RISK_PCT / price), MIN_QTY)
            qty = qty - (qty % 10)
            if qty < MIN_QTY:
                log_debug("❌ 주문 생략", f"수량 부족: {qty}")
                return jsonify({"error": "수량 부족"}), 200

            side = "buy" if signal == "long" else "sell"
            place_order(side, qty)

        return jsonify({"status": "요청 처리 완료"})
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "손절 감시 쓰레드 실행")
    threading.Thread(target=start_price_watcher, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
