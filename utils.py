import time
import json
import hmac
import hashlib
import ntplib
import requests

from config import BASE_URL, API_KEY, API_SECRET, SYMBOL

# ⏱ 서버 시간 동기화
def get_server_time():
    try:
        client = ntplib.NTPClient()
        response = client.request("pool.ntp.org")
        return int(response.tx_time * 1000)
    except Exception as e:
        print(f"⚠️ NTP 오류: {e} → 로컬 시간 사용")
        return int(time.time() * 1000)

# 🧾 시그니처 생성
def sign_request(secret, payload):
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha512
    ).hexdigest()

# 📬 요청 헤더 생성
def get_headers(timestamp, sign):
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": sign
    }

# 🟢 진입 주문 실행
def place_order(side):
    url = f"{BASE_URL}/futures/usdt/orders"

    payload = {
        "contract": SYMBOL,
        "size": 1,
        "price": 0,
        "tif": "ioc",             # 즉시체결후취소
        "text": "entry",          # 구분용 태그
        "iceberg": 0,
        "close": False,
        "reduce_only": False,
        "side": side,
        "auto_size": ""
    }

    body = json.dumps(payload)
    timestamp = str(get_server_time())
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

# 📈 진입가 조회
def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    timestamp = str(get_server_time())
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
    timestamp = str(get_server_time())
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
