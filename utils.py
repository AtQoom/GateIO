import time
import hmac
import hashlib
import requests
import json
from config import API_KEY, API_SECRET, BASE_URL, SYMBOL


def get_headers(method, endpoint, body=""):
    timestamp = str(int(time.time()))
    message = f"{timestamp}{method.upper()}{endpoint}{body}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha512).hexdigest()

    return {
        "KEY": API_KEY,
        "Timestamp": timestamp,
        "SIGN": signature,
        "Content-Type": "application/json"
    }


def get_balance():
    url = f"{BASE_URL}/futures/usdt/accounts"
    headers = get_headers("GET", "/futures/usdt/accounts")
    res = requests.get(url, headers=headers).json()
    return float(res.get("available", 0))


def get_current_price():
    url = f"{BASE_URL}/futures/usdt/tickers?contract={SYMBOL}"
    res = requests.get(url).json()
    return float(res["tickers"][0]["last"])


def place_order(side: str):
    balance = get_balance()
    price = get_current_price()
    quantity = round(balance / price, 4)

    print(f"📌 주문 수량 계산: {balance} USDT / {price} = {quantity} {SYMBOL}")

    size = quantity if side == "buy" else -quantity

    body = {
        "contract": SYMBOL,
        "size": size,
        "price": 0,
        "tif": "ioc"
    }

    headers = get_headers("POST", "/futures/usdt/orders", json.dumps(body))
    url = f"{BASE_URL}/futures/usdt/orders"
    res = requests.post(url, headers=headers, json=body)
    print(f"🚀 주문 전송: {side.upper()}, 상태코드: {res.status_code}")
    return res.json()


def get_open_position():
    url = f"{BASE_URL}/futures/usdt/positions"
    headers = get_headers("GET", "/futures/usdt/positions")
    res = requests.get(url, headers=headers).json()

    for pos in res:
        if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
            return float(pos["entry_price"])
    return None


def close_position(side: str):
    # 반대 방향으로 전량 청산
    url = f"{BASE_URL}/futures/usdt/positions"
    headers = get_headers("GET", "/futures/usdt/positions")
    res = requests.get(url, headers=headers).json()

    for pos in res:
        if pos["contract"] == SYMBOL and float(pos["size"]) > 0:
            size = float(pos["size"])
            close_side = "sell" if side == "buy" else "buy"
            order = {
                "contract": SYMBOL,
                "size": -size if close_side == "sell" else size,
                "price": 0,
                "tif": "ioc"
            }
            headers = get_headers("POST", "/futures/usdt/orders", json.dumps(order))
            r = requests.post(f"{BASE_URL}/futures/usdt/orders", headers=headers, json=order)
            print(f"🔚 포지션 청산 완료: {close_side.upper()}, 수량: {size}, 상태코드: {r.status_code}")
            return r.json()

    print("❗ 청산할 포지션 없음")
    return None
