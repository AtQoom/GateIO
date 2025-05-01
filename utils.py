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
        print(f"⚠️ NTP 오류: 로컬 시간 사용: {e}")
        return int(time.time() * 1000)


def get_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "SIGN": ""  # 이후에 서명으로 채움
    }


def sign_request(body, secret):
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()


def place_order(side):
    url = f"{BASE_URL}/futures/usdt/orders"
    timestamp = get_server_time()

    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",
        "text": "entry",
        "reduce_only": False,
        "side": side,
        "margin_mode": MARGIN_MODE,
        "auto_size": "",
        "timestamp": timestamp
    }

    body = json.dumps(payload)
    headers = get_headers()
    headers["SIGN"] = sign_request(body, API_SECRET)

    try:
        res = requests.post(url, headers=headers, data=body)
        print(f"📤 주문 응답: {res.status_code} - {res.text}")
        res.raise_for_status()
    except Exception as e:
        print(f"❌ 주문 실패: {e}")


def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    timestamp = get_server_time()

    headers = get_headers()
    headers["SIGN"] = sign_request("{}", API_SECRET)

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
    print(f"🔁 Close position: {side.upper()}")
    place_order(side)
