import os
import hmac
import hashlib
import time
import requests

API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

GATE_URL = "https://api.gateio.ws"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "KEY": API_KEY,
}

def sign_payload(method, url_path, body=""):
    nonce = str(int(time.time() * 1000))
    query = f"{method}\n{url_path}\n{nonce}\n{body}\n"
    signature = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha512).hexdigest()
    return nonce, signature

def place_order(symbol, side, price, size, reduce_only=False):
    path = "/api/v4/futures/usdt/orders"
    method = "POST"
    url = GATE_URL + path

    body = {
        "contract": symbol,
        "size": size,
        "price": price,
        "tif": "gtc",
        "text": "auto-trade",
        "reduce_only": reduce_only,
    }

    if side == "buy":
        body["side"] = "buy"
    else:
        body["side"] = "sell"

    import json
    payload = json.dumps(body)
    nonce, sign = sign_payload(method, path, payload)

    headers = {
        **HEADERS,
        "Timestamp": nonce,
        "SIGN": sign
    }

    res = requests.post(url, headers=headers, data=payload)
    print(f"[📡] 주문 응답: {res.status_code} {res.text}")
    return res.json()
