import time
import json
import hmac
import hashlib
import ntplib
import requests

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL

def get_server_time():
    try:
        ntp_client = ntplib.NTPClient()
        response = ntp_client.request("pool.ntp.org", version=3)
        return int(response.tx_time * 1000)
    except Exception as e:
        print(f"⚠️ NTP 오류! 로컬 시간 사용: {e}")
        return int(time.time() * 1000)

def get_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY
    }

def sign_request(payload: str, secret: str):
    return hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha512
    ).hexdigest()

def place_order(side):
    url = f"{BASE_URL}/futures/usdt/orders"

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
    headers = get_headers()
    timestamp = str(get_server_time())
    signature = sign_request(timestamp + body, API_SECRET)

    headers["Timestamp"] = timestamp
    headers["SIGN"] = signature

    try:
        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 주문 완료: {res.status_code} {res.text}")
    except Exception as e:
        print(f"❌ 주문 실패: {e}")

def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    headers = get_headers()
    timestamp = str(get_server_time())
    signature = sign_request(timestamp, API_SECRET)

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
