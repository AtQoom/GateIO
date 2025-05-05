import os
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
import gate_api.exceptions
import threading
import time

app = Flask(__name__)

# 환경변수에서 API 키 로딩
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
SETTLE = "usdt"
SYMBOL = "SOL_USDT"

# 위험 관리 설정
MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 1
RISK_PCT = 0.5

# 진입 정보 추적
entry_price = None
entry_side = None

# SDK 초기화
configuration = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(configuration)
api = FuturesApi(client)


def log(tag, msg):
    print(f"[{tag}] {msg}")


def get_equity():
    try:
        account = api.get_futures_account(SETTLE)
        return float(account.available)
    except Exception as e:
        log("❌ 잔고 조회 실패", e)
        return 0


def get_market_price():
    try:
        tickers = api.list_futures_tickers(SETTLE)
        for t in tickers:
            if t.contract == SYMBOL:
                return float(t.last)
    except Exception as e:
        log("❌ 시세 조회 실패", e)
    return 0


def get_position_size():
    try:
        pos = api.get_futures_position(SETTLE, SYMBOL)
        return float(pos.size)
    except Exception as e:
        log("❌ 포지션 조회 실패", e)
        return 0


def place_order(side: str, qty: float = 1, reduce_only=False):
    global entry_price, entry_side

    if reduce_only:
        qty = abs(get_position_size())
        if qty <= 0:
            log("⛔ 평청 스킵", "포지션 없음")
            return

    price = get_market_price()
    if price == 0:
        log("❌ 주문 실패", "시세 없음")
        return

    if not reduce_only and (qty * price < MIN_ORDER_USDT):
        log("❌ 최소 주문 미달", f"{qty * price:.2f} < {MIN_ORDER_USDT}")
        return

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

    try:
        result = api.create_futures_order(SETTLE, order)
        log("✅ 주문 성공", f"{side.upper()} {qty}개")
        if not reduce_only:
            entry_price = price
            entry_side = side
    except gate_api.exceptions.ApiException as e:
        log("❌ 주문 오류", f"{e.status}: {e.body}")
    except Exception as e:
        log("❌ 예외 발생", str(e))


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
                        log("🎯 롱 TP", f"{price:.2f} ≥ {entry_price * 1.01:.2f}")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        log("🛑 롱 SL", f"{price:.2f} ≤ {entry_price * 0.985:.2f}")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        log("🎯 숏 TP", f"{price:.2f} ≤ {entry_price * 0.99:.2f}")
                        place_order("buy", reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        log("🛑 숏 SL", f"{price:.2f} ≥ {entry_price * 1.015:.2f}")
                        place_order("buy", reduce_only=True)
                        entry_price = None
        except Exception as e:
            log("❌ TP/SL 예외", e)
        time.sleep(3)


@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json(force=True)
        log("📨 웹훅", data)

        signal = data.get("signal", "").lower()
        position = data.get("position", "").lower()

        if not signal or not position:
            return jsonify({"error": "signal 또는 position 누락"}), 400

        # 평청 처리
        if position == "long":
            place_order("sell", reduce_only=True)
            side = "buy"
        elif position == "short":
            place_order("buy", reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "잘못된 position"}), 400

        # 신규 진입
        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE) / price), MIN_QTY)
        log("🧮 주문 계산", f"잔고: {equity}, 가격: {price}, 수량: {qty}")
        place_order(side, qty)
        return jsonify({"status": "주문 완료", "side": side, "qty": qty})
    except Exception as e:
        import traceback
        log("❌ 예외 발생", traceback.format_exc())
        return jsonify({"error": "internal error"}), 500


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


if __name__ == "__main__":
    log("🚀 서버 시작", "TP/SL 감시 쓰레드 실행")
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
