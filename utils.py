import time
import hashlib
import hmac
import requests
import json
import uuid
import ntplib

from config import BASE_URL, SYMBOL, API_KEY, API_SECRET

# 서버 시간 동기화
def get_server_time():
    try:
        ntp_client = ntplib.NTPClient()
        response = ntp_client.request("pool.ntp.org", version=3)
        return int(response.tx_time * 1000)
    except Exception as e:
        print(f"[⚠️ NTP 오류] 로컬 시간 사용: {e}")
        return int(time.time() * 1000)

# 공통 헤더 생성
def get_headers(method, path, body=""):
    t = str(get_server_time())
    msg = f"{t}{method.upper()}{path}{body}"
    signature = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()

    return {
        "KEY": API_KEY,
        "Timestamp": t,
        "SIGN": signature,
        "Content-Type": "application/json"
    }

# 현재 포지션 평단가 확인
def get_open_position():
    try:
        path = f"/futures/usdt/positions/{SYMBOL}"
        url = BASE_URL + path
        headers = get_headers("GET", path)
        res = requests.get(url, headers=headers)
        pos_data = res.json()

        if isinstance(pos_data, dict) and float(pos_data.get("size", 0)) > 0:
            return float(pos_data.get("entry_price"))
    except Exception as e:
        print(f"❌ 포지션 조회 실패: {e}")
    return None

# 주문 실행
def place_order(side):
    try:
        path = "/futures/usdt/orders"
        url = BASE_URL + path

        order = {
            "contract": SYMBOL,
            "size": 1,
            "price": 0,  # 시장가
            "tif": "ioc",
            "text": f"webhook-{uuid.uuid4().hex[:6]}",
            "side": side
        }

        body = json.dumps(order)
        headers = get_headers("POST", path, body)
        res = requests.post(url, headers=headers, data=body)

        print(f"📤 주문 전송: {side.upper()}, 응답: {res.status_code}")
    except Exception as e:
        print(f"❌ 주문 오류: {e}")

# 포지션 청산
def close_position(side):
    try:
        path = "/futures/usdt/orders"
        url = BASE_URL + path

        order = {
            "contract": SYMBOL,
            "size": -1,
            "price": 0,
            "tif": "ioc",
            "text": f"close-{uuid.uuid4().hex[:6]}",
            "side": side
        }

        body = json.dumps(order)
        headers = get_headers("POST", path, body)
        res = requests.post(url, headers=headers, data=body)

        print(f"📤 청산 시도: {side.upper()}, 응답: {res.status_code}")
    except Exception as e:
        print(f"❌ 청산 오류: {e}")
