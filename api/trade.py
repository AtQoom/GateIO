import os
import hmac
import hashlib
import time
import requests
import json
from api.config import SYMBOL

API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
GATE_URL = "https://api.gateio.ws"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "KEY": API_KEY
}

def sign_payload(method, url_path, body=""):
    nonce = str(int(time.time() * 1000))
    query = f"{method}{url_path}{nonce}{body}"
    signature = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha512).hexdigest()
    return nonce, signature

def get_balance():
    path = "/api/v4/wallet/total_balance"
    method = "GET"
    url = GATE_URL + path
    nonce, sign = sign_payload(method, path)
    headers = {
        **HEADERS,
        "Timestamp": nonce,
        "SIGN": sign
    }
    res = requests.get(url, headers=headers)
    data = res.json()
    return float(data["available"]["USDT"])

def place_order(symbol, side, price, reduce_only=False):
    balance = get_balance()
    size = round(balance / price, 4)

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
        "side": side,
    }

    payload = json.dumps(body)
    nonce, sign = sign_payload(method, path, payload)

    headers = {
        **HEADERS,
        "Timestamp": nonce,
        "SIGN": sign
    }

    res = requests.post(url, headers=headers, data=payload)
    print(f"📤 주문 요청 결과: {res.status_code} {res.text}")
    return res.json()