import time
import hmac
import hashlib
import json
import requests
import ntplib

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL, MARGIN_MODE

def get_server_time():
    try:
        ntp_client = ntplib.NTPClient()
        response = ntp_client.request("pool.ntp.org", version=3)
        return int(response.tx_time * 1000)  # milliseconds
    except Exception as e:
        print(f"[⚠️ NTP 오류] 로컬 시간 사용: {e}")
        return int(time.time() * 1000)


def sign_request(body, secret, timestamp):
    payload_str = f"{timestamp}\n{json.dumps(body)}"
    sign = hmac.new(secret.encode(), payload_str.encode(), hashlib.sha512).hexdigest()
    return sign


def place_order(side):
    url = f"{BASE_URL}/futures/usdt/orders"
    timestamp = get_server_time()

    body = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",
        "text": "entry",
        "reduce_only": False,
        "side": side
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "Timestamp": str(timestamp),
        "SIGN": sign_request(body, API_SECRET, timestamp)
    }

    try:
        res = requests.post(url, headers=headers, data=json.dumps(body))
        print(f"📤 Order Response ({res.status_code}): {res.text}")
        res.raise_for_status()
    except Exception as e:
        print(f"❌ Order failed: {e}")


def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    timestamp = get_server_time()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "Timestamp": str(timestamp),
        "SIGN": sign_request({}, API_SECRET, timestamp)
    }

    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        positions = res.json()

        for pos in positions:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])
    except Exception as e:
        print(f"⚠️ Position fetch error: {e}")
    return None


def close_position(side):
    print(f"🔁 Closing position with {side.upper()} order")
    place_order(side)
