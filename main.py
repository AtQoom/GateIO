from datetime import datetime
import os
import json
import time
import threading
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, GateApiException

app = Flask(__name__)

# 환경 변수에서 API 키 불러오기
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SYMBOL = "SOL_USDT"
SETTLE = "usdt"
MIN_QTY = 1
RISK_PCT = 0.5

# Gate API 클라이언트 초기화
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
        account = accounts[0] if isinstance(accounts, list) else accounts
        log_debug("잔고 조회", account.to_dict())
        return float(account.available)
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return 0


def get_position_size():
    try:
        position = api_instance.get_futures_position(SETTLE, SYMBOL)
        log_debug("포지션 조회", position.to_dict())
        return float(position.size)
    except Exception as e:
        log_debug("❌ 포지션 조회 실패", str(e))
        return 0


def get_market_price():
    try:
        ticker = api_instance.get_futures_ticker(SETTLE, SYMBOL)
        return float(ticker.last)
    except Exception as e:
        log_debug("❌ 가격 조회 실패", str(e))
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
        response = api_instance.create_futures_order(SETTLE, order)
        log_debug("✅ 주문 성공", response.to_dict())

        if not reduce_only:
            entry_price = float(response.fill_price or 0)
            entry_side = side
    except GateApiException as e:
        log_debug("❌ 주문 실패", f"{e.status} - {e.body}")
    except Exception as e:
        log_debug("❌ 예외 발생", str(e))


def check_tp_sl_loop():
    global entry_price, entry_side

    while True:
        try:
            if entry_price and entry_side:
                position = api_instance.get_futures_position(SETTLE, SYMBOL)
                price = float(position.mark_price)
                if entry_side == "buy":
                    if price >= entry_price * 1.01 or price <= entry_price * 0.985:
                        log_debug("TP/SL 조건 충족", f"{price=}, {entry_price=}")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                        entry_side = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99 or price >= entry_price * 1.015:
                        log_debug("TP/SL 조건 충족", f"{price=}, {entry_price=}")
                        place_order("buy", reduce_only=True)
                        entry_price = None
                        entry_side = None
        except Exception as e:
            log_debug("❌ TP/SL 오류", str(e))
        time.sleep(3)


@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side

    try:
        data = request.get_json(force=True)
        log_debug("📨 웹훅 수신", json.dumps(data))

        signal = data.get("signal", "").lower()
        position = data.get("position", "").lower()

        if not signal or not position:
            return jsonify({"error": "신호 또는 포지션 누락"}), 400

        # 반대 포지션 정리
        if position == "long":
            place_order("sell", reduce_only=True)
            side = "buy"
        elif position == "short":
            place_order("buy", reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "invalid position"}), 400

        # 신규 진입
        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 가격 오류"}), 500

        qty = max(int(equity * RISK_PCT / price), MIN_QTY)
        log_debug("🧮 주문 계산", f"{equity=}, {price=}, {qty=}")
        place_order(side, qty)
        return jsonify({"status": "주문 완료", "side": side, "qty": qty})
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 오류"}), 500


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


if __name__ == "__main__":
    log_debug("🚀 서버 시작", "TP/SL 감시 스레드 실행")
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
