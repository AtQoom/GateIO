import time
import hmac
import hashlib
import json
import requests
from config import BASE_URL, API_KEY, API_SECRET, SYMBOL, MARGIN_MODE

def get_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "SIGN": "",  # 여기에 서명 추가
    }

def sign_request(body, secret):
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()

def place_order(side):
    url = f"{BASE_URL}/futures/usdt/orders"
    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",
        "text": "entry",
        "reduce_only": False,
        "side": side
    }

    body = json.dumps(payload)
    headers = get_headers()
    headers["SIGN"] = sign_request(body, API_SECRET)

    try:
        res = requests.post(url, headers=headers, data=body)
        print(f"📥 Order Response ({res.status_code}): {res.text}")
        res.raise_for_status()
    except Exception as e:
        print(f"❌ Order failed: {e}")

def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    try:
        res = requests.get(url, headers=get_headers())
        res.raise_for_status()
        positions = res.json()
        for pos in positions:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])
    except Exception as e:
        print(f"⚠️ Position fetch error: {e}")
    return None

def close_position(side):
    print(f"🔁 Closing position with {side.upper()} order")
    place_order(side)
