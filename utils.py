import time
import json
import hmac
import hashlib
import ntplib
import requests
from urllib.parse import urlencode

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL


def get_server_time():
    try:
        client = ntplib.NTPClient()
        response = client.request('pool.ntp.org')
        return int(response.tx_time * 1000)
    except Exception as e:
        print(f"⚠️ NTP 오류! 로컬 시간 사용: {e}")
        return int(time.time() * 1000)


def sign_request(method, path, query_string, body, timestamp):
    hashed_body = hashlib.sha512(body.encode()).hexdigest()
    signature_string = f"{method}\n{path}\n{query_string}\n{hashed_body}\n{timestamp}"
    signature = hmac.new(API_SECRET.encode(), signature_string.encode(), hashlib.sha512).hexdigest()
    return signature


def get_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
    }


def place_order(side):
    method = "POST"
    path = "/futures/usdt/orders"
    url = BASE_URL + path
    query_string = ""

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
    timestamp = str(get_server_time())
    signature = sign_request(method, path, query_string, body, timestamp)

    headers = get_headers()
    headers["Timestamp"] = timestamp
    headers["SIGN"] = signature

    try:
        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 주문 완료: {res.status_code} {res.text}")
    except Exception as e:
        print(f"❌ 주문 실패: {e}")


def get_open_position():
    method = "GET"
    path = "/futures/usdt/positions"
    url = BASE_URL + path
    query_string = ""
    body = ""

    timestamp = str(get_server_time())
    signature = sign_request(method, path, query_string, body, timestamp)

    headers = get_headers()
    headers["Timestamp"] = timestamp
    headers["SIGN"] = signature

    try:
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
