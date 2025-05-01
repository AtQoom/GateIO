import time
import json
import hmac
import hashlib
import requests

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL


def get_server_time():
    return int(time.time() * 1000)


def get_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY
    }


def sign_request(method: str, path: str, query_string: str, body: str, secret: str, timestamp: str):
    hashed_body = hashlib.sha512(body.encode()).hexdigest()
    payload = f"{method}\n{path}\n{query_string}\n{hashed_body}\n{timestamp}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()


def place_order(side):
    method = "POST"
    path = "/futures/usdt/orders"
    query_string = ""
    url = f"{BASE_URL}{path}"

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

    try:
        body = json.dumps(payload)
        timestamp = str(get_server_time())
        signature = sign_request(method, path, query_string, body, API_SECRET, timestamp)
        headers = get_headers()
        headers["Timestamp"] = timestamp
        headers["SIGN"] = signature

        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 주문 완료: {res.status_code} {res.text}")
    except Exception as e:
        print(f"❌ 주문 실패: {e}")


def get_open_position():
    method = "GET"
    path = "/futures/usdt/positions"
    query_string = ""
    url = f"{BASE_URL}{path}"

    try:
        timestamp = str(get_server_time())
        signature = sign_request(method, path, query_string, "", API_SECRET, timestamp)
        headers = get_headers()
        headers["Timestamp"] = timestamp
        headers["SIGN"] = signature

        res = requests.get(url, headers=headers)
        res.raise_for_status()
        positions = res.json()

        for pos in positions:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])
    except Exception as e:
        print(f"⚠️ 포지션 조회 오류: {e}")

    return None


def close_position(side):
    print(f"📤 포지션 종료 요청: {side.upper()}")
    place_order(side)
