import time
import hmac
import hashlib
import json
import requests
import ntplib

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL

def get_server_time():
    try:
        ntp_client = ntplib.NTPClient()
        response = ntp_client.request("pool.ntp.org", version=3)
        return int(response.tx_time * 1000)  # 밀리초 단위
    except Exception as e:
        print(f"[⚠️ NTP 오류] 로컬 시간 사용: {e}")
        return int(time.time() * 1000)

def get_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "SIGN": "",  # 서명은 나중에 추가
        "Timestamp": ""  # 타임스탬프는 나중에 추가
    }

def sign_request(method, url_path, query_string, body, secret, timestamp):
    hashed_payload = hashlib.sha512(body.encode()).hexdigest()
    signature_string = f"{method}\n{url_path}\n{query_string}\n{hashed_payload}\n{timestamp}"
    sign = hmac.new(secret.encode(), signature_string.encode(), hashlib.sha512).hexdigest()
    return sign

def place_order(side):
    url_path = "/futures/usdt/orders"
    url = f"{BASE_URL}{url_path}"
    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",
        "text": "entry",
        "reduce_only": False,
        "side": side
    }
    body = json.dumps(payload)
    timestamp = str(int(get_server_time() / 1000))  # 초 단위
    headers = get_headers()
    headers["Timestamp"] = timestamp
    headers["SIGN"] = sign_request("POST", url_path, "", body, API_SECRET, timestamp)

    try:
        res = requests.post(url, headers=headers, data=body)
        print(f"📤 주문 응답 ({res.status_code}): {res.text}")
        res.raise_for_status()
    except Exception as e:
        print(f"❌ 주문 실패: {e}")

def get_open_position():
    url_path = "/futures/usdt/positions"
    url = f"{BASE_URL}{url_path}"
    timestamp = str(int(get_server_time() / 1000))  # 초 단위
    headers = get_headers()
    headers["Timestamp"] = timestamp
    headers["SIGN"] = sign_request("GET", url_path, "", "", API_SECRET, timestamp)

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
    print(f"📉 포지션 종료: {side.upper()} 주문 실행")
    place_order(side)
