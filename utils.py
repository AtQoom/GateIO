import time
import json
import hmac
import hashlib
import requests

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL

def get_timestamp():
    return str(int(time.time() * 1000))

def get_headers(method, endpoint, body=""):
    timestamp = get_timestamp()
    hashed_payload = hashlib.sha512(body.encode()).hexdigest() if body else ""
    sign_str = f"{method}\n{endpoint}\n\n{hashed_payload}\n{timestamp}"
    sign = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha512).hexdigest()
    return {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": sign,
        "Content-Type": "application/json"
    }

def place_order(side):
    endpoint = "/futures/usdt/orders"
    url = f"{BASE_URL}{endpoint}"

    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",
        "text": "entry",
        "iceberg": 0,
        "close": False,
        "reduce_only": False,
        "side": side,
        "auto_size": ""
    }

    body = json.dumps(payload)
    headers = get_headers("POST", endpoint, body)

    try:
        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 주문 성공: {res.status_code} - {res.text}")
    except requests.exceptions.HTTPError as e:
        print(f"❌ 주문 실패 (HTTP): {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"❌ 주문 실패: {e}")

def get_open_position():
    endpoint = "/futures/usdt/positions"
    url = f"{BASE_URL}{endpoint}"
    headers = get_headers("GET", endpoint)

    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        for pos in res.json():
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])
    except Exception as e:
        print(f"⚠️ 포지션 조회 실패: {e}")
    return None

def close_position(side):
    print(f"📤 종료 요청: {side.upper()}")
    endpoint = "/futures/usdt/orders"
    url = f"{BASE_URL}{endpoint}"

    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",
        "text": "exit",
        "iceberg": 0,
        "close": True,
        "reduce_only": True,
        "side": side,
        "auto_size": ""
    }

    body = json.dumps(payload)
    headers = get_headers("POST", endpoint, body)

    try:
        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 종료 성공: {res.status_code} - {res.text}")
    except requests.exceptions.HTTPError as e:
        print(f"❌ 종료 실패 (HTTP): {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"❌ 종료 실패: {e}")
