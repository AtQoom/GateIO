from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
import gate_api.exceptions
import os
import threading
import time
from datetime import datetime

app = Flask(__name__)

# Load API key and secret from environment variables
api_key = os.environ.get("API_KEY", "")
api_secret = os.environ.get("API_SECRET", "")

config = Configuration(key=api_key, secret=api_secret)
client = ApiClient(config)
api = FuturesApi(client)

SYMBOL = "SOL_USDT"
SETTLE = "usdt"

MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 1
RISK_PCT = 0.5

entry_price = None
entry_side = None

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_equity():
    try:
        response = api_instance.list_futures_accounts()
        for acc in response:
            if acc.settle == "usdt":
                return float(acc.available)
        log_debug("❌ USDT 계좌 없음", "")
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
    return 0

def get_market_price():
    try:
        tickers = api.list_futures_tickers(settle=SETTLE)
        for ticker in tickers:
            if ticker.contract == SYMBOL:
                return float(ticker.last)
    except Exception as e:
        log_debug("❌ 가격 조회 실패", str(e))
    return 0

def get_position_size():
    try:
        position = api.get_position(settle=SETTLE, contract=SYMBOL)
        return float(position.size)
    except Exception as e:
        log_debug("❌ 포지션 조회 실패", str(e))
        return 0

def place_order(side, qty=1, reduce_only=False):
    global entry_price, entry_side

    if reduce_only:
        qty = abs(get_position_size())
        if qty <= 0:
            log_debug("⛔ 평청 스킵", "포지션 없음")
            return

    price = get_market_price()
    if price == 0:
        log_debug("❌ 주문 실패", "가격 없음")
        return

    if not reduce_only and (qty * price) < MIN_ORDER_USDT:
        log_debug("❌ 주문 금액 부족", f"{qty * price:.2f} < {MIN_ORDER_USDT}")
        return

    size = qty if side == "buy" else -qty
    if reduce_only:
        size = -size

    try:
        order = FuturesOrder(
            contract=SYMBOL,
            size=size,
            price="0",
            tif="ioc",
            reduce_only=reduce_only
        )
        response = api.create_futures_order(settle=SETTLE, futures_order=order)
        log_debug("✅ 주문 성공", f"{side.upper()} {qty}개")
        if not reduce_only:
            entry_price = price
            entry_side = side
    except gate_api.exceptions.ApiException as e:
        log_debug("❌ 주문 실패", f"{e.status} - {e.body}")

def check_tp_sl_loop():
    global entry_price, entry_side
    while True:
        try:
            if entry_price and entry_side:
                price = get_market_price()
                if not price:
                    continue
                if entry_side == "buy":
                    if price >= entry_price * 1.01:
                        log_debug("🎯 롱 TP", f"{price:.2f} ≥ {entry_price * 1.01:.2f}")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        log_debug("🛑 롱 SL", f"{price:.2f} ≤ {entry_price * 0.985:.2f}")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        log_debug("🎯 숏 TP", f"{price:.2f} ≤ {entry_price * 0.99:.2f}")
                        place_order("buy", reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        log_debug("🛑 숏 SL", f"{price:.2f} ≥ {entry_price * 1.015:.2f}")
                        place_order("buy", reduce_only=True)
                        entry_price = None
        except Exception as e:
            log_debug("❌ TP/SL 오류", str(e))
        time.sleep(3)

@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json(force=True)
        log_debug("📨 웹훅 수신", str(data))

        signal = data.get("signal", "").lower()
        position = data.get("position", "").lower()

        if not signal or not position:
            return jsonify({"error": "signal 또는 position 누락"}), 400

        if position == "long":
            place_order("sell", reduce_only=True)
            side = "buy"
        elif position == "short":
            place_order("buy", reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "invalid position"}), 400

        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE) / price), MIN_QTY)
        log_debug("🧮 주문 계산", f"잔고: {equity}, 가격: {price}, 수량: {qty}")
        place_order(side, qty)
        return jsonify({"status": "주문 완료", "side": side, "qty": qty})

    except Exception as e:
        import traceback
        log_debug("❌ 웹훅 예외", traceback.format_exc())
        return jsonify({"error": "internal error"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "TP/SL 감시 쓰레드 실행")
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
