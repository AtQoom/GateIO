import time
import hmac
import hashlib
import base64
import json
import requests
import ntplib
from config import API_KEY, API_SECRET, SYMBOL, BASE_URL


def get_server_time():
    try:
        client = ntplib.NTPClient()
        response = client.request("pool.ntp.org", version=3)
        return int(response.tx_time * 1000)
    except Exception as e:
        print(f"[⚠️ NTP 오류] 로컬 시간 사용: {e}")
        return int(time.time() * 1000)


def get_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "Timestamp": str(get_server_time())
    }


def sign_request(method, url, query_string="", payload_str=""):
    t = str(get_server_time())
    hashed_payload = hashlib.sha512(payload_str.encode()).hexdigest()
    s = f"{method.upper()}\n{url}\n{query_string}\n{hashed_payload}\n{t}"
    sign = hmac.new(API_SECRET.encode(), s.encode(), hashlib.sha512).hexdigest()

    headers = {
        "KEY": API_KEY,
        "Timestamp": t,
        "SIGN": sign,
        "Content-Type": "application/json"
    }
    return headers


def place_order(side):
    url_path = "/futures/usdt/orders"
    url = BASE_URL + url_path

    payload = {
        "contract": SYMBOL,
        "size": 0,  # 0 means market order with all available
        "price": 0,
        "tif": "ioc",
        "close": False,
        "reduce_only": False,
        "side": side
    }

    try:
        headers = sign_request("POST", url_path, "", json.dumps(payload))
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        response.raise_for_status()
        print(f"📈 Order placed: {side.upper()}")
    except Exception as e:
        print(f"❌ Order placement failed: {e}")


def close_position(side):
    url_path = "/futures/usdt/orders"
    url = BASE_URL + url_path

    payload = {
        "contract": SYMBOL,
        "size": 0,
        "price": 0,
        "tif": "ioc",
        "close": True,
        "reduce_only": True,
        "side": side
    }

    try:
        headers = sign_request("POST", url_path, "", json.dumps(payload))
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        response.raise_for_status()
        print(f"📤 Position closed: {side.upper()}")
    except Exception as e:
        print(f"❌ Close failed: {e}")


def get_open_position():
    url_path = f"/futures/usdt/positions"
    url = BASE_URL + url_path

    try:
        headers = sign_request("GET", url_path)
        response = requests.get(url, headers=headers)
        data = response.json()

        for pos in data:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])

        return None
    except Exception as e:
        print(f"❌ Get position failed: {e}")
        return None
