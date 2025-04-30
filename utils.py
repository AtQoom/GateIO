import os
import hmac
import hashlib
import time
import httpx

GATE_API = "https://api.gateio.ws/api/v4"
API_KEY = os.getenv("GATE_API_KEY")
API_SECRET = os.getenv("GATE_API_SECRET")
SYMBOL = os.getenv("GATE_SYMBOL", "SOL_USDT")

def verify_signature(data, secret):
    return True  # placeholder

async def send_order(side, price):
    endpoint = "/futures/usdt/orders"
    url = GATE_API + endpoint

    headers = {
        "KEY": API_KEY,
        "Timestamp": str(int(time.time())),
    }

    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": str(price),
        "tif": "ioc",
        "iceberg": 0,
        "close": False,
        "reduce_only": False,
        "auto_size": side,
    }

    signature_string = f"{headers['Timestamp']}{endpoint}{str(payload)}"
    signature = hmac.new(API_SECRET.encode(), signature_string.encode(), hashlib.sha512).hexdigest()
    headers["SIGN"] = signature

    async with httpx.AsyncClient() as client:
        res = await client.post(url, headers=headers, json=payload)
        return res.json()