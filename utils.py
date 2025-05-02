import time
import json
import hmac
import hashlib
import requests
from config import BASE_URL, API_KEY, API_SECRET, SYMBOL

# ⏱ 서버 시간 (Gate.io 공식 API)
def get_server_time():
    try:
        res = requests.get(f"{BASE_URL}/futures/usdt/server_time", timeout=3)
        res.raise_for_status()
        ts = res.json().get("server_time")
        return str(ts) if isinstance(ts, int) else str(int(time.time() * 1000))
    except Exception as e:
        print(f"⚠️ 서버 시간 오류: {e} → 로컬 시간 사용")
        return str(int(time.time() * 1000))


# 🔐 서명 생성
def sign_request(secret, payload: str):
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha512).hexdigest()


# 📬 요청 헤더
def get_headers(timestamp: str, sign: str):
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": sign
    }


# 🟢 진입 주문
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
    timestamp = get_server_time()
    sign = sign_request(API_SECRET, timestamp + body)
    headers = get_headers(timestamp, sign)

    try:
        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 주문 성공: {res.status_code} - {res.text}")
    except requests.exceptions.HTTPError as e:
        print(f"❌ 주문 실패 (HTTP): {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"❌ 주문 실패: {e}")


# 📈 포지션 조회
def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    timestamp = get_server_time()
    sign = sign_request(API_SECRET, timestamp)
    headers = get_headers(timestamp, sign)

    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        data = res.json()
        for pos in data:
            if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
                return float(pos["entry_price"])
    except requests.exceptions.HTTPError as e:
        print(f"⚠️ 포지션 조회 실패 (HTTP): {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"⚠️ 포지션 조회 실패: {e}")
    return None


# 🔴 포지션 종료
def close_position(side):
    print(f"📤 종료 요청: {side.upper()}")
    url = f"{BASE_URL}/futures/usdt/orders"
    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",
        "text": "exit",
        "iceberg": 0,
        "close": True,
        "reduce_only": True,
        "side": side,
        "auto_size": ""
    }

    body = json.dumps(payload)
    timestamp = get_server_time()
    sign = sign_request(API_SECRET, timestamp + body)
    headers = get_headers(timestamp, sign)

    try:
        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 종료 성공: {res.status_code} - {res.text}")
    except requests.exceptions.HTTPError as e:
        print(f"❌ 종료 실패 (HTTP): {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"❌ 종료 실패: {e}")
