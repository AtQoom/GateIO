import os
import hmac
import hashlib
import time
import requests
import base64
import ntplib
from config import BASE_URL, SYMBOL, API_KEY, API_SECRET

def get_server_time():
    try:
        ntp_client = ntplib.NTPClient()
        response = ntp_client.request("pool.ntp.org", version=3)
        return int(response.tx_time * 1000)
    except Exception as e:
        print(f"[⚠️ NTP 오류] 로컬 시간 사용: {e}")
        return int(time.time() * 1000)

def get_headers(method, path, query_string='', body=''):
    t = str(get_server_time())
    msg = f"{method.upper()}\n{path}\n{query_string}\n{body}\n{t}"
    signature = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()

    return {
        "KEY": API_KEY,
        "Timestamp": t,
        "SIGN": signature,
        "Content-Type": "application/json"
    }

def fetch_current_price():
    try:
        url = f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}"
        response = requests.get(url).json()
        return float(response["tickers"][0]["last"])
    except Exception as e:
        print(f"⚠️ 가격 조회 실패: {e}")
        return None

def place_order(side):
    url = f"{BASE_URL}/futures/usdt/orders"
    path = "/futures/usdt/orders"

    order_data = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,  # 시장가
        "tif": "ioc",  # 즉시 체결 또는 취소
        "close": False,
        "reduce_only": False,
        "side": side
    }

    headers = get_headers("POST", path, body=json_dumps(order_data))
    try:
        response = requests.post(url, headers=headers, json=order_data)
        print(f"📤 Order placed ({side.upper()}): {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ 주문 실패: {e}")

def close_position(side):
    url = f"{BASE_URL}/futures/usdt/orders"
    path = "/futures/usdt/orders"

    order_data = {
        "contract": SYMBOL,
        "size": 0,  # 0이면 전체 청산
        "price": 0,
        "tif": "ioc",
        "close": True,
        "reduce_only": True,
        "side": side
    }

    headers = get_headers("POST", path, body=json_dumps(order_data))
    try:
        response = requests.post(url, headers=headers, json=order_data)
        print(f"🔁 Close order ({side.upper()}): {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ 청산 실패: {e}")

def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    path = "/futures/usdt/positions"
    headers = get_headers("GET", path)

    try:
        response = requests.get(url, headers=headers)
        positions = response.json()

        for pos in positions:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])
        return None
    except Exception as e:
        print(f"⚠️ 포지션 확인 실패: {e}")
        return None

def json_dumps(obj):
    import json
    return json.dumps(obj, separators=(",", ":"))
