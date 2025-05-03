import os, time, json, hmac, hashlib, requests, threading
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# 🔐 환경변수에서 API 키 정보 불러오기
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
BASE_URL = "https://api.gateio.ws/api/v4"
SYMBOL = "SOL_USDT"

# 📊 트레이딩 파라미터
MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 1
RISK_PCT = 0.5

entry_price = None
entry_side = None
time_offset = 0  # 서버 시간 보정용 (ms 단위)

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

# ✅ 시간 동기화
def sync_time_with_gate():
    global time_offset
    try:
        r = requests.get(f"{BASE_URL}/spot/time")
        if r.status_code == 200:
            server_time = int(r.json()['server_time'])
            local_time = int(time.time() * 1000)
            time_offset = server_time - local_time
            log_debug("🕒 시간 동기화", f"offset: {time_offset}ms")
    except Exception as e:
        log_debug("❌ 시간 동기화 실패", str(e))

# 🧾 서명 생성
def sign_request(secret, payload: str):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()

# 🧾 헤더 구성
def get_headers(method, endpoint, query="", body=""):
    timestamp = str(int(time.time() * 1000) + time_offset)
    full_path = f"/api/v4{endpoint}"
    hashed_payload = hashlib.sha512((body or "").encode()).hexdigest()
    sign_str = f"{method.upper()}\n{full_path}\n{query}\n{hashed_payload}\n{timestamp}"
    signature = sign_request(API_SECRET, sign_str)
    return {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": signature,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def debug_api_response(name, response):
    log_debug(name, f"HTTP {response.status_code} - {response.text}")

# 💰 잔고 조회 (통합 계정)
def get_equity():
    endpoint = "/unified/accounts"
    headers = get_headers("GET", endpoint)
    try:
        r = requests.get(BASE_URL + endpoint, headers=headers)
        debug_api_response("잔고 조회", r)
        r.raise_for_status()
        for item in r.json():
            if item["currency"] == "USDT":
                return float(item["available"])
    except Exception as e:
        log_debug("❌ 잔고 오류", str(e))
    return 0

# 📈 시세 조회
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
        log_debug("❌ 시세 오류", str(e))
    return 0

# 📦 포지션 수량 조회
def get_position_size():
    endpoint = "/futures/usdt/positions"
    headers = get_headers("GET", endpoint)
    try:
        r = requests.get(BASE_URL + endpoint, headers=headers)
        debug_api_response("포지션 조회", r)
        r.raise_for_status()
        for item in r.json():
            if item["contract"] == SYMBOL:
                return float(item["size"])
    except Exception as e:
        log_debug("❌ 포지션 오류", str(e))
    return 0

# 🛒 주문 실행
def place_order(side, qty=1, reduce_only=False):
    global entry_price, entry_side
    if reduce_only:
        qty = get_position_size()
        if qty <= 0:
            log_debug("⛔ 종료 스킵", "포지션 없음")
            return

    price = get_market_price()
    if price == 0:
        log_debug("❌ 주문 실패", "가격 없음")
        return

    if not reduce_only and (qty * price) < MIN_ORDER_USDT:
        log_debug("❌ 주문 금액 부족", f"{qty * price:.2f} < {MIN_ORDER_USDT}")
        return

    body = json.dumps({
        "contract": SYMBOL,
        "size": qty,
        "price": 0,
        "side": side,
        "tif": "ioc",
        "reduce_only": reduce_only,
        "close": reduce_only
    })

    endpoint = "/futures/usdt/orders"
    headers = get_headers("POST", endpoint, "", body)

    try:
        r = requests.post(BASE_URL + endpoint, headers=headers, data=body)
        debug_api_response("주문 전송", r)
        if r.status_code == 200:
            log_debug("✅ 주문 성공", f"{side.upper()} {qty}개")
            if not reduce_only:
                entry_price = price
                entry_side = side
        else:
            log_debug("❌ 주문 실패", r.text)
    except Exception as e:
        log_debug("❌ 주문 예외", str(e))

# 🎯 TP/SL 체크 루프
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
                        log_debug("🎯 롱 TP", f"{price:.2f} ≥ {entry_price * 1.01:.2f}")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                    elif price <= entry_price * 0.985:
                        log_debug("🛑 롱 SL", f"{price:.2f} ≤ {entry_price * 0.985:.2f}")
                        place_order("sell", reduce_only=True)
                        entry_price = None
                elif entry_side == "sell":
                    if price <= entry_price * 0.99:
                        log_debug("🎯 숏 TP", f"{price:.2f} ≤ {entry_price * 0.99:.2f}")
                        place_order("buy", reduce_only=True)
                        entry_price = None
                    elif price >= entry_price * 1.015:
                        log_debug("🛑 숏 SL", f"{price:.2f} ≥ {entry_price * 1.015:.2f}")
                        place_order("buy", reduce_only=True)
                        entry_price = None
        except Exception as e:
            log_debug("❌ TP/SL 오류", str(e))
        time.sleep(3)

# 📨 웹훅 핸들러
@app.route("/", methods=["POST"])
def webhook():
    global entry_price, entry_side
    try:
        data = request.get_json(force=True)
        log_debug("📨 웹훅 수신", json.dumps(data, ensure_ascii=False))

        signal = data.get("signal", "").lower()
        position = data.get("position", "").lower()

        if not signal or not position:
            return jsonify({"error": "signal 또는 position 누락"}), 400

        if position == "long":
            place_order("sell", reduce_only=True)
            side = "buy"
        elif position == "short":
            place_order("buy", reduce_only=True)
            side = "sell"
        else:
            return jsonify({"error": "invalid position"}), 400

        equity = get_equity()
        price = get_market_price()
        if equity == 0 or price == 0:
            return jsonify({"error": "잔고 또는 시세 오류"}), 500

        qty = max(int((equity * RISK_PCT * LEVERAGE) / price), MIN_QTY)
        log_debug("🧮 주문 계산", f"잔고: {equity}, 가격: {price}, 수량: {qty}")
        place_order(side, qty)
        return jsonify({"status": "주문 완료", "side": side, "qty": qty})
    except Exception as e:
        log_debug("❌ 웹훅 처리 예외", str(e))
        return jsonify({"error": "internal error"}), 500

# ⏱️ 업타임 로봇 Ping 확인용 엔드포인트
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

# 🧠 앱 실행
if __name__ == "__main__":
    sync_time_with_gate()
    log_debug("🚀 서버 시작", "TP/SL 감시 쓰레드 실행")
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
