import os, time, json, hmac, hashlib, requests
from flask import Flask, request, jsonify
import threading

app = Flask(__name__)

# 환경 변수
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
BASE_URL = "https://api.gateio.ws/api/v4"

# 설정
SYMBOL = "SOL_USDT"
MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 6
RISK_PCT = 0.16

entry_price = None
entry_side = None

# 시그니처 생성
def sign_request(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()

# 서버 시간 동기화
def get_timestamp():
    try:
        r = requests.get(f"{BASE_URL}/spot/time", timeout=3)
        return str(r.json()["server_time"])
    except:
        return str(int(time.time() * 1000))

# 헤더 생성
def get_headers(method, endpoint, body=""):
    ts = get_timestamp()
    hashed = hashlib.sha512(body.encode()).hexdigest() if body else ""
    sign_str = f"{method}\n{endpoint}\n\n{hashed}\n{ts}"
    sign = sign_request(API_SECRET, sign_str)
    return {
        "KEY": API_KEY,
        "Timestamp": ts,
        "SIGN": sign,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

# 시세 조회
def get_price():
    try:
        r = requests.get(f"{BASE_URL}/futures/tickers", timeout=5)
        for item in r.json():
            if item["contract"] == SYMBOL:
                return float(item["last"])
    except:
        return 0

# 잔고 조회
def get_equity():
    try:
        headers = get_headers("GET", "/unified/account")
        r = requests.get(f"{BASE_URL}/unified/account", headers=headers, timeout=5)
        return float(r.json()["total"]["USDT"]["available"])
    except:
        print("❌ 잔고 조회 실패")
        return 0

# 포지션 사이즈 조회
def get_position_size():
    try:
        headers = get_headers("GET", "/unified/account/positions")
        r = requests.get(f"{BASE_URL}/unified/account/positions", headers=headers, timeout=5)
        for pos in r.json():
            if pos["contract"] == SYMBOL:
                return float(pos["size"])
    except:
        print("❌ 포지션 조회 실패")
    return 0

# 주문 전송
def place_order(side, qty, reduce_only=False):
    global entry_price, entry_side
    if reduce_only:
        qty = get_position_size()
        if qty <= 0:
            print("✅ 종료 스킵 (포지션 없음)")
            return

    price = get_price()
    if price == 0:
        print("❌ 시세 오류")
        return

    notional = qty * price
    if not reduce_only and notional < MIN_ORDER_USDT:
        print(f"❌ 최소 주문 금액 부족: {notional:.2f} USDT")
        return

    payload = {
        "contract": SYMBOL,
        "size": qty,
        "price": 0,
        "tif": "ioc",
        "close": reduce_only,
        "reduce_only": reduce_only,
        "side": side,
        "auto_size": ""
    }

    body = json.dumps(payload)
    headers = get_headers("POST", "/unified/account/orders", body)

    try:
        r = requests.post(f"{BASE_URL}/unified/account/orders", headers=headers, data=body)
        if r.status_code == 200:
            print(f"🚀 주문 성공: {side.upper()} {qty}")
            if not reduce_only:
                entry_price = price
                entry_side = side
        else:
            print(f"❌ 주문 실패: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"❌ 주문 오류: {e}")

# TP/SL 체크 루프
def check_tp_sl_loop():
    global entry_price, entry_side
    while True:
        try:
            if entry_price and entry_side:
                price = get_price()
                if entry_side == "buy":
                    if price >= entry_price * 1.01:
                        print("✅ 롱 TP 도달")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        print("🛑 롱 SL 도달")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        print("✅ 숏 TP 도달")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        print("🛑 숏 SL 도달")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
        except Exception as e:
            print(f"❌ TP/SL 오류: {e}")
        time.sleep(2)

# 웹훅 처리
@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json(force=True)
        signal = data.get("signal", "").lower()
        strength = float(data.get("strength", "1.0"))
        print(f"[📩 웹훅] 시그널: {signal} | 강도: {strength}")

        if "long" in signal:
            place_order("sell", 0, reduce_only=True)
            side = "buy"
        elif "short" in signal:
            place_order("buy", 0, reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "Invalid signal"}), 400

        equity = get_equity()
        price = get_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE * strength) / price), MIN_QTY)
        place_order(side, qty)
        return jsonify({"status": "주문 전송", "side": side, "qty": qty})

    except Exception as e:
        print(f"❌ 웹훅 처리 오류: {e}")
        return jsonify({"error": "internal error"}), 500

if __name__ == "__main__":
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
