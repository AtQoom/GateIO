import time
import hmac
import hashlib
import json
import requests
import ntplib

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL

def get_server_time():
    try:
        client = ntplib.NTPClient()
        res = client.request("pool.ntp.org", version=3)
        return int(res.tx_time)
    except Exception as e:
        print(f"[⚠️ NTP 오류] 로컬 시간 사용: {e}")
        return int(time.time())

def generate_signature(method, path, query_string, body, timestamp, secret):
    payload = body if body else ""
    hashed_payload = hashlib.sha512(payload.encode()).hexdigest()
    signature_string = f"{method}\n{path}\n{query_string}\n{hashed_payload}\n{timestamp}"
    return hmac.new(secret.encode(), signature_string.encode(), hashlib.sha512).hexdigest()

def get_headers(signature, timestamp):
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "SIGN": signature,
        "Timestamp": str(timestamp)
    }

def place_order(side):
    path = "/futures/usdt/orders"
    url = BASE_URL + path
    body_data = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",
        "text": "entry",
        "reduce_only": False,
        "side": side
    }
    body = json.dumps(body_data)
    timestamp = get_server_time()
    signature = generate_signature("POST", path, "", body, timestamp, API_SECRET)
    headers = get_headers(signature, timestamp)

    try:
        res = requests.post(url, headers=headers, data=body)
        print(f"📤 주문 응답 ({res.status_code}): {res.text}")
        res.raise_for_status()
    except Exception as e:
        print(f"❌ 주문 실패: {e}")

def get_open_position():
    path = "/futures/usdt/positions"
    url = BASE_URL + path
    timestamp = get_server_time()
    signature = generate_signature("GET", path, "", "", timestamp, API_SECRET)
    headers = get_headers(signature, timestamp)

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
    print(f"📉 포지션 종료: {side.upper()}")
    place_order(side)
