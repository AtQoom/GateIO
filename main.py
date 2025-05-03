import os, time, json, hmac, hashlib, threading, requests
from flask import Flask, request, jsonify

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

# 서버 시간 동기화
def get_timestamp():
    try:
        res = requests.get("https://api.gateio.ws/api/v4/spot/time", timeout=5)
        return str(res.json()["server_time"])
    except:
        return str(int(time.time() * 1000))

# 서명
def sign(payload: str) -> str:
    return hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha512).hexdigest()

# 헤더
def get_headers(method, endpoint, body=""):
    ts = get_timestamp()
    hashed = hashlib.sha512(body.encode()).hexdigest() if body else ""
    sign_str = f"{method}\n{endpoint}\n\n{hashed}\n{ts}"
    signature = sign(sign_str)
    return {
        "KEY": API_KEY,
        "Timestamp": ts,
        "SIGN": signature,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

# 시세
def get_price():
    try:
        res = requests.get(BASE_URL + "/futures/usdt/tickers")
        for t in res.json():
            if t["contract"] == SYMBOL:
                return float(t["last"])
    except:
        return 0

# 잔고
def get_equity():
    try:
        headers = get_headers("GET", "/futures/usdt/accounts")
        res = requests.get(BASE_URL + "/futures/usdt/accounts", headers=headers)
        if res.status_code == 200:
            return float(res.json()["available"])
    except:
        pass
    return 0

# 포지션 수량
def get_position_size():
    try:
        headers = get_headers("GET", "/futures/usdt/positions")
        res = requests.get(BASE_URL + "/futures/usdt/positions", headers=headers)
        if res.status_code == 200:
            for p in res.json():
                if p["contract"] == SYMBOL:
                    return float(p["size"])
    except:
        pass
    return 0

# 주문
def place_order(side, qty, reduce_only=False):
    global entry_price, entry_side
    if reduce_only:
        qty = get_position_size()
        if qty <= 0:
            print("⛔ 포지션 없음 → 종료 생략")
            return
    price = get_price()
    if price == 0:
        print("⛔ 가격 조회 실패")
        return
    notional = qty * price
    if notional < MIN_ORDER_USDT and not reduce_only:
        print(f"⛔ 주문 최소 금액 미만: {notional:.2f}")
        return

    payload = json.dumps({
        "contract": SYMBOL,
        "size": qty,
        "price": 0,
        "side": side,
        "tif": "ioc",
        "reduce_only": reduce_only,
        "close": reduce_only,
        "text": "entry" if not reduce_only else "exit"
    })

    headers = get_headers("POST", "/futures/usdt/orders", payload)
    try:
        res = requests.post(BASE_URL + "/futures/usdt/orders", headers=headers, data=payload)
        if res.status_code == 200:
            print(f"✅ 주문 성공: {side.upper()} {qty}")
            if not reduce_only:
                entry_price = price
                entry_side = side
        else:
            print(f"❌ 주문 실패: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"[오류] 주문 전송 실패: {e}")

# TP/SL 루프
def check_tp_sl_loop():
    global entry_price, entry_side
    while True:
        try:
            if entry_price and entry_side:
                price = get_price()
                if entry_side == "buy":
                    if price >= entry_price * 1.01:
                        print("🎯 롱 TP")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        print("🛑 롱 SL")
                        place_order("sell", 0, reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        print("🎯 숏 TP")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        print("🛑 숏 SL")
                        place_order("buy", 0, reduce_only=True)
                        entry_price = None
        except Exception as e:
            print(f"[오류] TP/SL 체크 실패: {e}")
        time.sleep(3)

# 웹훅 처리
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        signal = data.get("signal", "").lower()
        position = data.get("position", "").lower()
        strength = float(data.get("strength", "1.0"))
        print(f"[📩 웹훅 시그널] {signal} | 포지션: {position} | 강도: {strength}")

        if signal != "entry" or position not in ["long", "short"]:
            return jsonify({"error": "Invalid signal or position"}), 400

        # 종료 주문 먼저
        place_order("sell" if position == "long" else "buy", 0, reduce_only=True)

        # 신규 진입
        equity = get_equity()
        price = get_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        side = "buy" if position == "long" else "sell"
        qty = max(int((equity * RISK_PCT * LEVERAGE * strength) / price), MIN_QTY)
        place_order(side, qty)

        return jsonify({"status": "success", "side": side, "qty": qty})
    except Exception as e:
        print(f"[ERROR] 웹훅 처리 실패: {e}")
        return jsonify({"error": "internal error"}), 500

if __name__ == "__main__":
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
