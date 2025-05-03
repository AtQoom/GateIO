import os, time, json, hmac, hashlib, requests, threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# 🧩 API 환경 변수
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
BASE_URL = "https://api.gateio.ws/api/v4"

# 🛠️ 설정
SYMBOL = "SOL_USDT"
LEVERAGE = 1
MIN_QTY = 1
MIN_ORDER_USDT = 3
RISK_PCT = 0.1

entry_price = None
entry_side = None

# 🕒 서버 시간 동기화
def get_timestamp():
    return str(int(time.time() * 1000))

# 🔐 시그니처 생성
def sign_request(secret, payload: str):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()

# 📬 헤더 생성
def get_headers(method, endpoint, body=""):
    timestamp = get_timestamp()
    hashed_payload = hashlib.sha512(body.encode()).hexdigest() if body else ""
    sign_str = f"{method.upper()}\n/api/v4{endpoint}\n\n{hashed_payload}\n{timestamp}"
    signature = sign_request(API_SECRET, sign_str)
    return {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": signature,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

# 💰 잔고 조회
def get_equity():
    try:
        endpoint = "/futures/usdt/accounts"
        headers = get_headers("GET", endpoint)
        res = requests.get(BASE_URL + endpoint, headers=headers)
        if res.status_code == 200:
            return float(res.json()["available"])
        print(f"[❌ 잔고 오류] {res.status_code}: {res.text}")
    except Exception as e:
        print(f"[ERROR] 잔고 조회 실패: {e}")
    return 0

# 📈 시세 조회
def get_market_price():
    try:
        endpoint = "/futures/usdt/tickers"
        headers = get_headers("GET", endpoint)
        res = requests.get(BASE_URL + endpoint, headers=headers)
        if res.status_code == 200:
            for item in res.json():
                if item["contract"] == SYMBOL:
                    return float(item["last"])
    except Exception as e:
        print(f"[ERROR] 시세 조회 실패: {e}")
    return 0

# 📊 포지션 크기 확인
def get_position_size():
    try:
        endpoint = "/futures/usdt/positions"
        headers = get_headers("GET", endpoint)
        res = requests.get(BASE_URL + endpoint, headers=headers)
        if res.status_code == 200:
            for p in res.json():
                if p["contract"] == SYMBOL:
                    return float(p["size"])
    except Exception as e:
        print(f"[ERROR] 포지션 조회 실패: {e}")
    return 0

# 📦 주문 실행
def place_order(side, qty, reduce_only=False):
    global entry_price, entry_side
    if reduce_only:
        qty = get_position_size()
        if qty <= 0:
            print("[🛑 종료 실패] 포지션 없음")
            return
    price = get_market_price()
    if not price:
        return
    notional = qty * price
    if not reduce_only and notional < MIN_ORDER_USDT:
        print(f"[❌ 주문 최소금액 미만] {notional:.2f} USDT")
        return
    payload = {
        "contract": SYMBOL,
        "size": qty,
        "price": 0,
        "tif": "ioc",
        "reduce_only": reduce_only,
        "close": reduce_only,
        "side": side
    }
    body = json.dumps(payload)
    headers = get_headers("POST", "/futures/usdt/orders", body)
    try:
        res = requests.post(BASE_URL + "/futures/usdt/orders", headers=headers, data=body)
        if res.status_code == 200:
            print(f"[🚀 주문 성공] {side.upper()} {qty}개")
            if not reduce_only:
                entry_price = price
                entry_side = side
        else:
            print(f"[❌ 주문 실패] {res.status_code}: {res.text}")
    except Exception as e:
        print(f"[ERROR] 주문 실패: {e}")

# 📉 TP/SL 루프
def check_tp_sl_loop():
    global entry_price, entry_side
    while True:
        try:
            price = get_market_price()
            if entry_price and price:
                if entry_side == "buy":
                    if price >= entry_price * 1.01:
                        print("[✅ TP 도달] 롱 청산")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        print("[🛑 SL 도달] 롱 청산")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        print("[✅ TP 도달] 숏 청산")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        print("[🛑 SL 도달] 숏 청산")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
        except Exception as e:
            print(f"[ERROR] TP/SL 확인 실패: {e}")
        time.sleep(3)

# 🌐 웹훅 처리
@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json()
        signal = data.get("signal", "")
        position = data.get("position", "").lower()
        print(f"[📨 웹훅 수신] 시그널: {signal} | 포지션: {position}")

        if position not in ["long", "short"]:
            return jsonify({"error": "Invalid position"}), 400

        # 종료 먼저
        place_order("sell" if position == "long" else "buy", 0, reduce_only=True)

        # 진입 주문
        equity = get_equity()
        price = get_market_price()
        if not equity or not price:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE) / price), MIN_QTY)
        place_order("buy" if position == "long" else "sell", qty)
        return jsonify({"status": "주문 전송", "side": position, "qty": qty})
    except Exception as e:
        print(f"[ERROR] 웹훅 처리 실패: {e}")
        return jsonify({"error": "internal error"}), 500

# ▶️ 메인 실행
if __name__ == "__main__":
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
