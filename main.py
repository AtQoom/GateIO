import os
import json
import time
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from gate_api.exceptions import ApiException

app = Flask(__name__)

# 환경 변수
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SYMBOL = "SOL_USDT"
SETTLE = "usdt"
MIN_QTY = 1
RISK_PCT = 0.5

# API 초기화
config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

entry_price = None
entry_side = None


def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")


def get_equity():
    try:
        accounts = api_instance.list_futures_accounts(SETTLE)
        for acc in accounts:
            if acc.contract == SYMBOL:
                log_debug("잔고 조회", acc.to_dict())
                return float(acc.available)
        return float(accounts[0].available) if accounts else 0
    except ApiException as e:
        log_debug("❌ 잔고 조회 실패", f"{e.status} - {e.body}")
        return 0


def get_position_size():
    try:
        pos = api_instance.get_position(SETTLE, SYMBOL)
        log_debug("포지션 조회", pos.to_dict())
        return float(pos.size)
    except ApiException as e:
        log_debug("❌ 포지션 조회 실패", f"{e.status} - {e.body}")
        return 0


def get_market_price():
    try:
        ticker = api_instance.get_futures_ticker(SETTLE, SYMBOL)
        return float(ticker.last)
    except ApiException as e:
        log_debug("❌ 시세 조회 실패", f"{e.status} - {e.body}")
        return 0


def place_order(side, qty=1, reduce_only=False):
    global entry_price, entry_side
    try:
        size = qty if side == "buy" else -qty
        if reduce_only:
            size = -size
        order = FuturesOrder(
            contract=SYMBOL,
            size=size,
            price="0",
            tif="ioc",
            reduce_only=reduce_only
        )
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug("✅ 주문 성공", result.to_dict())

        if not reduce_only:
            entry_price = float(result.fill_price or 0)
            entry_side = side
    except ApiException as e:
        log_debug("❌ 주문 실패", f"{e.status} - {e.body}")
    except Exception as e:
        log_debug("❌ 예외 발생", str(e))


def check_tp_sl_loop():
    global entry_price, entry_side
    while True:
        try:
            if entry_price and entry_side:
                pos = api_instance.get_position(SETTLE, SYMBOL)
                mark = float(pos.mark_price)
                if entry_side == "buy":
                    if mark >= entry_price * 1.01 or mark <= entry_price * 0.985:
                        log_debug("🎯 롱 TP/SL", f"{mark=}, {entry_price=}")
                        place_order("sell", reduce_only=True)
                        entry_price, entry_side = None, None
                elif entry_side == "sell":
                    if mark <= entry_price * 0.99 or mark >= entry_price * 1.015:
                        log_debug("🎯 숏 TP/SL", f"{mark=}, {entry_price=}")
                        place_order("buy", reduce_only=True)
                        entry_price, entry_side = None, None
        except Exception as e:
            log_debug("❌ TP/SL 오류", str(e))
        time.sleep(3)


@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json(force=True)
        signal = data.get("signal", "").lower()
        position = data.get("position", "").lower()
        log_debug("📨 웹훅 수신", json.dumps(data))

        if not signal or not position:
            return jsonify({"error": "signal 또는 position 누락"}), 400

        # 평청
        if position == "long":
            place_order("sell", reduce_only=True)
            side = "buy"
        elif position == "short":
            place_order("buy", reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "invalid position"}), 400

        # 진입
        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int(equity * RISK_PCT / price), MIN_QTY)
        log_debug("🧮 주문 계산", f"잔고: {equity}, 가격: {price}, 수량: {qty}")
        place_order(side, qty)
        return jsonify({"status": "주문 완료", "side": side, "qty": qty})
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 오류"}), 500


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


if __name__ == "__main__":
    log_debug("🚀 서버 시작", "TP/SL 감시 쓰레드 실행")
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
