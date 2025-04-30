import time
import hmac
import hashlib
import json
import requests

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL, MARGIN_MODE

def get_headers(timestamp: str) -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "SIGN": "",  # 여기에 나중에 sign_request로 추가
        "Timestamp": timestamp
    }

def sign_request(body: str, secret: str, timestamp: str) -> str:
    message = f"{timestamp}{body}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha512).hexdigest()

def place_order(side: str):
    url = f"{BASE_URL}/futures/usdt/orders"

    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",               # 시장가로 즉시 체결
        "text": "entry",            # 식별용 태그
        "reduce_only": False,       # 진입
        "side": side                # "buy" 또는 "sell"
    }

    body = json.dumps(payload)
    timestamp = str(int(time.time() * 1000))
    sign = sign_request(body, API_SECRET, timestamp)
    headers = get_headers(timestamp)
    headers["SIGN"] = sign

    try:
        res = requests.post(url, headers=headers, data=body)
        print(f"📤 Order Response ({res.status_code}): {res.text}")
        res.raise_for_status()
    except Exception as e:
        print(f"❌ Order failed: {e}")

def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    timestamp = str(int(time.time() * 1000))
    headers = get_headers(timestamp)
    body = ""  # GET 요청은 body 없음
    sign = sign_request(body, API_SECRET, timestamp)
    headers["SIGN"] = sign

    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        positions = res.json()

        for pos in positions:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])

    except Exception as e:
        print(f"⚠️ Position fetch error: {e}")
    return None

def close_position(side: str):
    print(f"📉 Closing position with {side.upper()} order")
    place_order(side)
