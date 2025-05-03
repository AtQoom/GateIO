import os, time, json, hmac, hashlib, requests, threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# 실거래 API 서버
BASE_URL = "https://api.gateio.ws/api/v4"

# 환경 변수
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")

# 설정
SYMBOL = "SOL_USDT"
MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 1
RISK_PCT = 0.16

entry_price = None
entry_side = None


def get_server_timestamp():
    try:
        r = requests.get(f"{BASE_URL}/spot/time", timeout=3)
        return str(r.json()["server_time"])
    except Exception as e:
        print(f"[⚠️ 시간 조회 실패 → 로컬 사용] {e}")
        return str(int(time.time() * 1000))


def sign_request(method, endpoint, query="", body=""):
    timestamp = get_server_timestamp()
    hashed_body = hashlib.sha512((body or "").encode()).hexdigest()
    sign_str = f"{method.upper()}\n/api/v4{endpoint}\n{query}\n{hashed_body}\n{timestamp}"
    signature = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha512).hexdigest()
    return {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": signature,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


def get_market_price():
    try:
        r = requests.get(f"{BASE_URL}/futures/usdt/tickers", timeout=5)
        for ticker in r.json():
            if ticker["contract"] == SYMBOL:
                return float(ticker["last"])
    except Exception as e:
        print(f"[❌ 시세 조회 실패] {e}")
    return 0


def get_equity():
    try:
        headers = sign_request("GET", "/futures/usdt/accounts")
        r = requests.get(f"{BASE_URL}/futures/usdt/accounts", headers=headers, timeout=5)
        return float(r.json()["available"])
    except Exception as e:
        print(f"[❌ 잔고 조회 실패] {e}")
        return 0


def get_position_size():
    try:
        headers = sign_request("GET", "/futures/usdt/positions")
        r = requests.get(f"{BASE_URL}/futures/usdt/positions", headers=headers, timeout=5)
        for pos in r.json():
            if pos["contract"] == SYMBOL:
                return float(pos["size"])
    except Exception as e:
        print(f"[❌ 포지션 조회 실패] {e}")
    return 0


def place_order(side, qty, reduce_only=False):
    global entry_price, entry_side

    if reduce_only:
        qty = get_position_size()
        if qty <= 0:
            print("[ℹ️ 청산할 포지션 없음]")
            return

    price = get_market_price()
    if price == 0:
        print("[❌ 시세 조회 실패 → 주문 중단]")
        return

    notional = qty * price
    if not reduce_only and notional < MIN_ORDER_USDT:
        print(f"[❌ 주문 금액 {notional:.2f} < 최소 {MIN_ORDER_USDT}]")
        return

    payload = {
        "contract": SYMBOL,
        "size": qty,
        "price": 0,
        "tif": "ioc",
        "reduce_only": reduce_only,
        "close": reduce_only,
        "side": side,
        "auto_size": ""
    }

    body = json.dumps(payload)
    headers = sign_request("POST", "/futures/usdt/orders", body=body)

    try:
        r = requests.post(f"{BASE_URL}/futures/usdt/orders", headers=headers, data=body, timeout=5)
        if r.status_code == 200:
            print(f"[✅ 주문 성공] {side.upper()} {qty}개")
            if not reduce_only:
                entry_price = price
                entry_side = side
        else:
            print(f"[❌ 주문 실패] {r.status_code} - {r.text}")
    except Exception as e:
        print(f"[❌ 주문 요청 실패] {e}")


def check_tp_sl_loop(interval=3):
    global entry_price, entry_side
    while True:
        try:
            if entry_price and entry_side:
                price = get_market_price()
                if entry_side == "buy":
                    if price >= entry_price * 1.01:
                        print("[📈 롱 익절]")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        print("[🛑 롱 손절]")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        print("[📉 숏 익절]")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        print("[🛑 숏 손절]")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
        except Exception as e:
            print(f"[TP/SL 오류] {e}")
        time.sleep(interval)


@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json(force=True)
        signal = data.get("signal", "").lower()
        print(f"[📨 웹훅 수신] 시그널: {signal}")

        if "long" in signal:
            place_order("sell", 0, reduce_only=True)
            side = "buy"
        elif "short" in signal:
            place_order("buy", 0, reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "signal must include 'long' or 'short'"}), 400

        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE) / price), MIN_QTY)
        place_order(side, qty)
        return jsonify({"status": "진입 주문 전송", "side": side, "qty": qty})
    except Exception as e:
        print(f"[❌ 웹훅 처리 실패] {e}")
        return jsonify({"error": "internal error"}), 500


if __name__ == "__main__":
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
