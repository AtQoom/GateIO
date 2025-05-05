from datetime import datetime
import os
import json
import hmac
import time
import hashlib
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
BASE_URL = "https://api.gateio.ws/api/v4"
SYMBOL = "SOL_USDT"
SETTLE = "usdt"

MIN_ORDER_USDT = 3
MIN_QTY = 1
LEVERAGE = 1
RISK_PCT = 0.5

entry_price = None
entry_side = None

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_server_timestamp():
    try:
        r = requests.get(f"{BASE_URL}/spot/time", timeout=3)
        r.raise_for_status()
        server_time = int(r.json().get("serverTime", time.time() * 1000))
        offset = server_time - int(time.time() * 1000)
        log_debug("⏱️ 시간 동기화", f"offset: {offset}ms")
        return str(server_time)
    except Exception as e:
        log_debug("❌ 시간 조회 실패", str(e))
        return str(int(time.time() * 1000))

def sign_request(secret, payload: str):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()

def safe_request(method, url, **kwargs):
    for i in range(3):
        try:
            r = requests.request(method, url, timeout=5, **kwargs)
            if r.status_code == 503:
                log_debug("⏳ 재시도", f"{url} - 503 오류 발생, {i + 1}/3회 재시도")
                time.sleep(3)
                continue
            return r
        except Exception as e:
            log_debug("❌ 요청 실패", str(e))
    return None

def get_headers(method, endpoint, body="", query=""):
    timestamp = get_server_timestamp()
    full_path = f"/api/v4{endpoint}"
    hashed_body = hashlib.sha512((body or "").encode()).hexdigest()
    sign_str = f"{method.upper()}\n{full_path}\n{query}\n{hashed_body}\n{timestamp}"
{full_path}
{query}
{hashed_body}
{timestamp}"
    sign = sign_request(API_SECRET, sign_str)
    return {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": sign,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def debug_api_response(name, response):
    if response:
        log_debug(name, f"HTTP {response.status_code} - {response.text}")
    else:
        log_debug(name, "응답 없음 (None)")

def get_equity():
    endpoint = f"/futures/{SETTLE}/accounts"
    headers = get_headers("GET", endpoint)
    r = safe_request("GET", BASE_URL + endpoint, headers=headers)
    debug_api_response("잔고 조회", r)
    if r and r.status_code == 200:
        return float(r.json().get("available", 0))
    return 0

def get_market_price():
    endpoint = f"/futures/{SETTLE}/tickers"
    headers = get_headers("GET", endpoint)
    r = safe_request("GET", BASE_URL + endpoint, headers=headers)
    if r and r.status_code == 200:
        for item in r.json():
            if item.get("contract") == SYMBOL:
                return float(item.get("last", 0))
    return 0

def get_position_size():
    endpoint = f"/futures/{SETTLE}/positions/{SYMBOL}"
    headers = get_headers("GET", endpoint)
    r = safe_request("GET", BASE_URL + endpoint, headers=headers)
    debug_api_response("포지션 조회", r)
    if r and r.status_code == 200:
        return float(r.json().get("size", 0))
    return 0

def place_order(side, qty=1, reduce_only=False):
    global entry_price, entry_side

    if reduce_only:
        qty = abs(get_position_size())
        if qty <= 0:
            log_debug("⛔ 평청 스킵", "포지션 없음")
            return

    price = get_market_price()
    if price == 0:
        log_debug("❌ 주문 실패", "가격 없음")
        return

    if not reduce_only and (qty * price) < MIN_ORDER_USDT:
        log_debug("❌ 주문 금액 부족", f"{qty * price:.2f} < {MIN_ORDER_USDT}")
        return

    size = qty if side == "buy" else -qty
    if reduce_only:
        size = -size

    body = json.dumps({
        "contract": SYMBOL,
        "size": size,
        "price": 0,
        "tif": "ioc",
        "reduce_only": str(reduce_only).lower()
    })

    endpoint = f"/futures/{SETTLE}/orders"
    headers = get_headers("POST", endpoint, body)
    r = safe_request("POST", BASE_URL + endpoint, headers=headers, data=body)
    debug_api_response("주문 전송", r)
    if r and r.status_code == 200:
        log_debug("✅ 주문 성공", f"{side.upper()} {qty}개")
        if not reduce_only:
            entry_price = price
            entry_side = side
    else:
        log_debug("❌ 주문 실패", r.text if r else "응답 없음")

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
        import traceback
        log_debug("❌ 웹훅 예외", traceback.format_exc())
        return jsonify({"error": "internal error"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "TP/SL 감시 쓰레드 실행")
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
