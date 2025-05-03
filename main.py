import os, time, json, hmac, hashlib, requests, threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# 환경 변수
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
BASE_URL = "https://api.gateio.ws/api/v4"

# 설정값
SYMBOL = "SOL_USDT"
MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 1  # 필요시 6 등으로 조정 가능
RISK_PCT = 0.16

entry_price = None
entry_side = None

# 서버 시간 불러오기
def get_server_timestamp():
    try:
        r = requests.get(BASE_URL + "/spot/time", timeout=5)
        return str(r.json()["server_time"])
    except Exception as e:
        print(f"[⚠️ 시간 조회 실패] {e}")
        return str(int(time.time() * 1000))

# 시그니처 생성
def sign_request(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()

# 요청 헤더 생성
def get_headers(method, endpoint, body=""):
    timestamp = get_server_timestamp()
    hashed_payload = hashlib.sha512(body.encode()).hexdigest() if body else ""
    sign_str = f"{method}\n{endpoint}\n\n{hashed_payload}\n{timestamp}"
    sign = sign_request(API_SECRET, sign_str)
    return {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": sign,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

# 잔고 조회
def get_equity():
    try:
        url = f"/futures/usdt/accounts"
        headers = get_headers("GET", url)
        r = requests.get(BASE_URL + url, headers=headers, timeout=10)
        if r.status_code == 200:
            return float(r.json()["available"])
        print(f"[❌ 잔고 오류] {r.status_code} - {r.text}")
    except Exception as e:
        print(f"[ERROR] 잔고 조회 실패: {e}")
    return 0

# 시세 조회
def get_market_price():
    try:
        url = f"/futures/usdt/tickers"
        headers = get_headers("GET", url)
        r = requests.get(BASE_URL + url, headers=headers, timeout=10)
        if r.status_code == 200:
            for t in r.json():
                if t["contract"] == SYMBOL:
                    return float(t["last"])
    except Exception as e:
        print(f"[ERROR] 시세 조회 실패: {e}")
    return 0

# 포지션 사이즈 조회
def get_position_size():
    try:
        url = f"/futures/usdt/positions"
        headers = get_headers("GET", url)
        r = requests.get(BASE_URL + url, headers=headers, timeout=10)
        if r.status_code == 200:
            for p in r.json():
                if p["contract"] == SYMBOL:
                    return float(p["size"])
    except Exception as e:
        print(f"[ERROR] 포지션 조회 실패: {e}")
    return 0

# 주문 전송
def place_order(side, qty, reduce_only=False):
    global entry_price, entry_side
    if reduce_only:
        qty = get_position_size()
        if qty <= 0:
            print("⛔️ 종료 실패 - 포지션 없음")
            return

    price = get_market_price()
    if price == 0:
        return

    notional = qty * price
    if notional < MIN_ORDER_USDT and not reduce_only:
        print(f"[❌ 주문 금액 {notional:.2f} < 최소 {MIN_ORDER_USDT}]")
        return

    body_data = {
        "contract": SYMBOL,
        "size": qty,
        "price": 0,
        "side": side,
        "tif": "ioc",
        "reduce_only": reduce_only,
        "close": reduce_only,
        "auto_size": ""
    }

    body = json.dumps(body_data)
    headers = get_headers("POST", "/futures/usdt/orders", body)

    try:
        r = requests.post(BASE_URL + "/futures/usdt/orders", headers=headers, data=body, timeout=10)
        if r.status_code == 200:
            print(f"[🚀 주문] {side.upper()} {qty}개")
            if not reduce_only:
                entry_price = price
                entry_side = side
        else:
            print(f"[❌ 주문 실패] {r.status_code} - {r.text}")
    except Exception as e:
        print(f"[ERROR] 주문 실패: {e}")

# TP/SL 체크 루프
def check_tp_sl_loop(interval=3):
    global entry_price, entry_side
    while True:
        try:
            if entry_price and entry_side:
                price = get_market_price()
                if price:
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
            print(f"[ERROR] TP/SL 체크 실패: {e}")
        time.sleep(interval)

# 웹훅
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        signal = data.get("signal", "")
        position = data.get("position", "").lower()
        strength = float(data.get("strength", 1.0))
        print(f"📨 웹훅 시그널: {signal} | 포지션: {position} | 강도: {strength}")

        # 기존 포지션 종료
        if position == "long":
            place_order("sell", 0, reduce_only=True)
            side = "buy"
        elif position == "short":
            place_order("buy", 0, reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "Invalid position"}), 400

        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE * strength) / price), MIN_QTY)
        place_order(side, qty)
        return jsonify({"status": "주문 전송", "side": side, "qty": qty})
    except Exception as e:
        print(f"[ERROR] 웹훅 처리 실패: {e}")
        return jsonify({"error": "internal error"}), 500

# 실행
if __name__ == "__main__":
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
