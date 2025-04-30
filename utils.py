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
        "SIGN": "",
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
        "iceberg": 0,
        "text": "entry",
        "close": False,
        "side": side,
    }
    body = json.dumps(payload)
    headers = get_headers()
    headers["SIGN"] = sign_request(body, API_SECRET)

    try:
        res = requests.post(url, headers=headers, data=body)
        print(f"[ORDER] {res.status_code}: {res.text}")
    except Exception as e:
        print(f"❌ Order error: {e}")

def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    try:
        res = requests.get(url, headers=get_headers())
        positions = res.json()
        for pos in positions:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])
    except Exception as e:
        print(f"❌ Position error: {e}")
    return None

def close_position(side):
    place_order(side)
