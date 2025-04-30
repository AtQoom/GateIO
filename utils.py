import requests
import traceback
import json
from config import SYMBOL, BASE_URL, API_KEY, API_SECRET

# ✅ 헤더 생성
def get_headers():
    return {
        "Content-Type": "application/json",
        "KEY": API_KEY,
        "SIGN": API_SECRET  # 실제 Gate.io는 서명 필요하지만 생략한 구조
    }

# 📤 진입 주문 실행
def place_order(side):
    try:
        url = f"{BASE_URL}/spot/orders"
        data = {
            "currency_pair": SYMBOL,
            "type": "market",
            "side": side,
            "amount": "",  # 최대 수량 지정 시 수정 필요
            "account": "spot"
        }
        resp = requests.post(url, headers=get_headers(), data=json.dumps(data))
        if resp.status_code == 200:
            print(f"🚀 Entry order ({side.upper()}) sent")
            return True
        else:
            print(f"❌ Order failed: {resp.text}")
            return False
    except Exception as e:
        print("⚠️ Exception during place_order:", e)
        traceback.print_exc()
        return False

# 📊 진입 후 평단가 조회
def get_open_position():
    try:
        url = f"{BASE_URL}/spot/accounts"
        resp = requests.get(url, headers=get_headers())
        if resp.status_code == 200:
            balances = resp.json()
            for item in balances:
                if item['currency'] == SYMBOL.split('_')[0]:
                    print(f"🔍 Found position: {item}")
                    return float(item['available'])
        else:
            print(f"❌ Position lookup failed: {resp.text}")
    except Exception as e:
        print("⚠️ Exception during get_open_position:", e)
        traceback.print_exc()
    return None

# 💣 포지션 청산
def close_position(side):
    try:
        print(f"📤 Closing position: {side.upper()}")
        url = f"{BASE_URL}/spot/orders"
        data = {
            "currency_pair": SYMBOL,
            "type": "market",
            "side": side,
            "amount": "",  # 최대 수량 지정 필요
            "account": "spot"
        }
        resp = requests.post(url, headers=get_headers(), data=json.dumps(data))
        if resp.status_code == 200:
            print(f"✅ Close order ({side.upper()}) completed")
        else:
            print(f"❌ Close order failed: {resp.text}")
    except Exception as e:
        print("⚠️ Exception during close_position:", e)
        traceback.print_exc()
