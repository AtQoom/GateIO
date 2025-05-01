import time
import hmac
import hashlib
import json
import requests
import ntplib

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL, MARGIN_MODE

# NTP 기반 서버 시간 동기화
def get_server_time():
    try:
        client = ntplib.NTPClient()
        response = client.request("pool.ntp.org", version=3)
        return int(response.tx_time * 1000)  # ms
    except Exception as e:
        print(f"⚠️ NTP 오류. 로컬 시간 사용: {e}")
        return int(time.time() * 1000)

# API 요청 헤더 생성
def get_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "SIGN": ""  # 이후에 추가
    }

# HMAC 서명 생성
def sign_request(body, secret):
    return hmac.new(
        secret.encode(), 
        body.encode(), 
        hashlib.sha512
    ).hexdigest()

# 주문 실행
def place_order(side):
    url = f"{BASE_URL}/futures/usdt/orders"
    timestamp = get_server_time()

    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",  # 즉시 체결
        "text": "entry",
        "reduce_only": False,
        "side": side,
        "auto_size": "",
        "margin_mode": MARGIN_MODE,
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

# 현재 포지션의 진입 가격 조회
def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    try:
        headers = get_headers()
        timestamp = get_server_time()
        headers["Timestamp"] = str(timestamp)

        res = requests.get(url, headers=headers)
        positions = res.json()

        for pos in positions:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])
    except Exception as e:
        print(f"⚠️ 포지션 조회 오류: {e}")
    return None

# 반대 방향으로 포지션 정리
def close_position(side):
    print(f"🔁 포지션 청산: {side.upper()}")
    place_order(side)
