import time
import json
import hmac
import hashlib
import requests
from config import BASE_URL, API_KEY, API_SECRET, SYMBOL

# ⏱ 타임스탬프 생성 함수 (Gate.io는 공식 시간 API 없음 → 로컬 시간 사용)
def get_timestamp():
    try:
        return str(int(time.time() * 1000))
    except Exception as e:
        print(f"[⚠️ 시간 오류 → 로컬 시간 사용] {e}")
        return str(int(time.time() * 1000))

# 🔐 서명 생성
def sign_request(secret, payload: str):
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha512).hexdigest()

# 📬 요청 헤더 구성
def get_headers(method, endpoint, body=""):
    timestamp = get_timestamp()
    hashed_payload = hashlib.sha512(body.encode()).hexdigest() if body else ""
    sign_str = f"{method}\n{endpoint}\n\n{hashed_payload}\n{timestamp}"
    sign = sign_request(API_SECRET, sign_str)
    return {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": sign,
        "Content-Type": "application/json"
    }

# 🟢 진입 주문
def place_order(side):
    endpoint = "/futures/usdt/orders"
    url = f"{BASE_URL}{endpoint}"
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
    headers = get_headers("POST", endpoint, body)

    try:
        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 주문 성공: {res.status_code} - {res.text}")
    except requests.exceptions.HTTPError as e:
        print(f"❌ 주문 실패 (HTTP): {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"❌ 주문 실패: {e}")

# 📈 포지션 진입가 확인
def get_open_position():
    endpoint = "/futures/usdt/positions"
    url = f"{BASE_URL}{endpoint}"
    headers = get_headers("GET", endpoint)

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

# 🔴 포지션 종료 주문
def close_position(side):
    print(f"📤 종료 요청: {side.upper()}")
    endpoint = "/futures/usdt/orders"
    url = f"{BASE_URL}{endpoint}"
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
    headers = get_headers("POST", endpoint, body)

    try:
        res = requests.post(url, headers=headers, data=body)
        res.raise_for_status()
        print(f"✅ 종료 성공: {res.status_code} - {res.text}")
    except requests.exceptions.HTTPError as e:
        print(f"❌ 종료 실패 (HTTP): {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"❌ 종료 실패: {e}")
