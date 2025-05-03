import os, time, json, hmac, hashlib, requests, threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# === 환경변수 로딩 ===
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
BASE_URL = "https://api.gateio.ws/api/v4"
SYMBOL = "SOL_USDT"

# === 설정 ===
MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 6
RISK_PCT = 0.16

entry_price = None
entry_side = None

# === 서버 시간 가져오기 (정확도 중요) ===
def get_server_timestamp():
    try:
        res = requests.get(f"{BASE_URL}/spot/time", timeout=2)
        return str(res.json()["server_time"])
    except Exception as e:
        print(f"[⚠️ 서버 시간 오류 → 로컬 시간 사용] {e}")
        return str(int(time.time() * 1000))

# === 서명(Signature) 생성 ===
def sign_payload(method, endpoint, query="", body=""):
    timestamp = get_server_timestamp()
    hashed_body = hashlib.sha512(body.encode()).hexdigest() if body else ""
    message = f"{method.upper()}\n/api/v4{endpoint}\n{query}\n{hashed_body}\n{timestamp}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha512).hexdigest()
    headers = {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": signature,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    return headers

# === 진입/청산 주문 ===
def place_order(side, qty, reduce_only=False):
    global entry_price, entry_side

    if reduce_only:
        qty = get_position_size()
        if qty <= 0:
            print("[⛔️ 종료 실패 - 포지션 없음]")
            return

    price = get_market_price()
    if price == 0:
        print("[⛔️ 시세 조회 실패]")
        return

    notional = qty * price
    if not reduce_only and notional < MIN_ORDER_USDT:
        print(f"[⛔️ 주문 금액 {notional:.2f} < 최소 {MIN_ORDER_USDT}]")
        return

    payload = {
        "contract": SYMBOL,
        "size": qty,
        "price": 0,
        "side": side,
        "tif": "ioc",
        "reduce_only": reduce_only,
        "close": reduce_only,
        "auto_size": "",
        "text": "entry" if not reduce_only else "exit"
    }
    body = json.dumps(payload)
    headers = sign_payload("POST", "/futures/usdt/orders", body=body)

    try:
        res = requests.post(f"{BASE_URL}/futures/usdt/orders", headers=headers, data=body, timeout=10)
        res.raise_for_status()
        print(f"[✅ 주문 성공] {side.upper()} {qty}개")
        if not reduce_only:
            entry_price = price
            entry_side = side
    except requests.exceptions.RequestException as e:
        print(f"[❌ 주문 실패] {e.response.status_code if e.response else '?'} - {e.response.text if e.response else e}")

# === 잔고 조회 ===
def get_equity():
    try:
        headers = sign_payload("GET", "/futures/usdt/accounts")
        res = requests.get(f"{BASE_URL}/futures/usdt/accounts", headers=headers)
        res.raise_for_status()
        return float(res.json()["available"])
    except Exception as e:
        print(f"[❌ 잔고 오류] {e}")
        return 0

# === 시세 조회 ===
def get_market_price():
    try:
        res = requests.get(f"{BASE_URL}/futures/usdt/tickers", timeout=5)
        for item in res.json():
            if item["contract"] == SYMBOL:
                return float(item["last"])
    except Exception as e:
        print(f"[❌ 시세 오류] {e}")
    return 0

# === 포지션 사이즈 확인 ===
def get_position_size():
    try:
        headers = sign_payload("GET", "/futures/usdt/positions")
        res = requests.get(f"{BASE_URL}/futures/usdt/positions", headers=headers)
        res.raise_for_status()
        for p in res.json():
            if p["contract"] == SYMBOL:
                return float(p["size"])
    except Exception as e:
        print(f"[❌ 포지션 조회 실패] {e}")
    return 0

# === TP/SL 루프 ===
def check_tp_sl_loop(interval=5):
    global entry_price, entry_side
    while True:
        try:
            if entry_price and entry_side:
                price = get_market_price()
                if not price:
                    continue
                if entry_side == "buy":
                    if price >= entry_price * 1.01:
                        print("[✅ TP 도달 - 롱 종료]")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        print("[🛑 SL 도달 - 롱 종료]")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        print("[✅ TP 도달 - 숏 종료]")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        print("[🛑 SL 도달 - 숏 종료]")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
        except Exception as e:
            print(f"[TP/SL 오류] {e}")
        time.sleep(interval)

# === 웹훅 수신 ===
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print(f"[DEBUG] 웹훅 데이터: {data}")
        signal = data.get("signal", "").lower()
        position = data.get("position", "").lower()
        strength = float(data.get("strength", 1.0))

        if signal != "entry" or position not in ["long", "short"]:
            return jsonify({"error": "Invalid data"}), 400

        side = "buy" if position == "long" else "sell"
        place_order("sell" if position == "long" else "buy", 0, reduce_only=True)  # 기존 포지션 정리

        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고/시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE * strength) / price), MIN_QTY)
        place_order(side, qty)

        return jsonify({"status": "주문 전송 완료", "side": side, "qty": qty})
    except Exception as e:
        print(f"[❌ 웹훅 처리 오류] {e}")
        return jsonify({"error": "internal error"}), 500

# === 실행 ===
if __name__ == "__main__":
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
