import os, time, json, hmac, hashlib, requests
from flask import Flask, request, jsonify
import threading

app = Flask(__name__)

# 🔑 환경 변수
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
BASE_URL = "https://api.gateio.ws/api/v4"
SYMBOL = "SOL_USDT"

# ⚙️ 설정
MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 1
RISK_PCT = 0.16

entry_price = None
entry_side = None

# 🕒 서버 시간
def get_server_timestamp():
    try:
        url = f"{BASE_URL}/spot/time"
        r = requests.get(url, timeout=5)
        return str(r.json()["server_time"])
    except Exception as e:
        print(f"[⚠️ 서버 시간 실패, 로컬 사용] {e}")
        return str(int(time.time() * 1000))

# 🔐 시그니처 생성
def sign_request(secret, payload: str):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()

# 📦 요청 헤더 생성
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

# 📊 현재 자산 조회
def get_equity():
    endpoint = "/futures/usdt/accounts"
    headers = get_headers("GET", endpoint)
    try:
        r = requests.get(BASE_URL + endpoint, headers=headers, timeout=10)
        r.raise_for_status()
        return float(r.json()["available"])
    except Exception as e:
        print(f"[❌ 잔고 오류] {e}")
    return 0

# 📈 실시간 가격
def get_market_price():
    endpoint = "/futures/usdt/tickers"
    headers = get_headers("GET", endpoint)
    try:
        r = requests.get(BASE_URL + endpoint, headers=headers)
        r.raise_for_status()
        for item in r.json():
            if item["contract"] == SYMBOL:
                return float(item["last"])
    except Exception as e:
        print(f"[❌ 가격 조회 오류] {e}")
    return 0

# 📌 보유 포지션 수량
def get_position_size():
    endpoint = "/futures/usdt/positions"
    headers = get_headers("GET", endpoint)
    try:
        r = requests.get(BASE_URL + endpoint, headers=headers)
        r.raise_for_status()
        for p in r.json():
            if p["contract"] == SYMBOL and float(p["size"]) > 0:
                return float(p["size"])
    except Exception as e:
        print(f"[❌ 포지션 조회 오류] {e}")
    return 0

# 📬 주문 전송
def place_order(side, qty=1, reduce_only=False):
    global entry_price, entry_side
    if reduce_only:
        qty = get_position_size()
        if qty <= 0:
            print("📭 종료 주문 불필요 - 포지션 없음")
            return
    price = get_market_price()
    if price == 0:
        return
    notional = qty * price
    if notional < MIN_ORDER_USDT and not reduce_only:
        print(f"[❌ 주문 금액 부족: {notional:.2f} < 최소 {MIN_ORDER_USDT}]")
        return

    payload = {
        "contract": SYMBOL,
        "size": qty,
        "price": 0,
        "side": side,
        "tif": "ioc",
        "reduce_only": reduce_only,
        "close": reduce_only
    }

    body = json.dumps(payload)
    headers = get_headers("POST", "/futures/usdt/orders", body)

    try:
        r = requests.post(BASE_URL + "/futures/usdt/orders", headers=headers, data=body)
        if r.status_code == 200:
            print(f"[📥 주문 전송 성공] {side.upper()} | 수량: {qty}")
            if not reduce_only:
                entry_price = price
                entry_side = side
        else:
            print(f"[❌ 주문 실패] {r.status_code} - {r.text}")
    except Exception as e:
        print(f"[❌ 주문 오류] {e}")

# 📉 자동 청산 감시 루프
def check_tp_sl_loop():
    global entry_price, entry_side
    while True:
        try:
            if entry_price and entry_side:
                price = get_market_price()
                if entry_side == "buy":
                    if price >= entry_price * 1.01:
                        print("[🎯 TP 도달] 롱 종료")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        print("[⚠️ SL 도달] 롱 종료")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        print("[🎯 TP 도달] 숏 종료")
                        place_order("buy", reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        print("[⚠️ SL 도달] 숏 종료")
                        place_order("buy", reduce_only=True)
                        entry_price = None
        except Exception as e:
            print(f"[❌ TP/SL 체크 오류] {e}")
        time.sleep(3)

# 🌐 웹훅 엔드포인트
@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json(force=True)
        signal = data.get("signal", "")
        position = data.get("position", "").lower()

        print(f"[📨 웹훅 수신] 시그널: {signal} | 포지션: {position}")

        # 기존 포지션 종료
        if position == "long":
            place_order("sell", reduce_only=True)
            side = "buy"
        elif position == "short":
            place_order("buy", reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "잘못된 포지션"}), 400

        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE) / price), MIN_QTY)
        place_order(side, qty)
        return jsonify({"status": "주문 완료", "side": side, "qty": qty})
    except Exception as e:
        print(f"[❌ 웹훅 처리 오류] {e}")
        return jsonify({"error": "내부 오류"}), 500

# 🧠 메인 실행
if __name__ == "__main__":
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
