
import os
import json
import time
import asyncio
import threading
import websockets
import requests
import hmac
import hashlib
from decimal import Decimal
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
MARGIN_BUFFER = Decimal("0.9")

SYMBOL_LEVERAGE = {
    "BTC_USDT": Decimal("10"),
    "ADA_USDT": Decimal("10"),
    "SUI_USDT": Decimal("10"),
}

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": Decimal("10"), "qty_step": Decimal("10"), "sl_pct": Decimal("0.0075"), "min_order_usdt": Decimal("5")},
    "BTC_USDT": {"min_qty": Decimal("0.0001"), "qty_step": Decimal("0.0001"), "sl_pct": Decimal("0.004"), "min_order_usdt": Decimal("5")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "sl_pct": Decimal("0.0075"), "min_order_usdt": Decimal("5")}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}
account_cache = {"time": 0, "data": None}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def set_leverage(symbol, leverage):
    try:
        url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/positions/{symbol}/leverage"
        timestamp = str(int(time.time()))
        body = json.dumps({"leverage": int(leverage)})
        method = "POST"
        path = f"/api/v4/futures/{SETTLE}/positions/{symbol}/leverage"

        sign_str = f"{timestamp}{method}{path}{body}"
        sign = hmac.new(
            API_SECRET.encode(), sign_str.encode(), hashlib.sha512
        ).hexdigest()

        headers = {
            "KEY": API_KEY,
            "Timestamp": timestamp,
            "SIGN": sign,
            "Content-Type": "application/json"
        }

        response = requests.post(url, headers=headers, data=body)
        if response.status_code == 200:
            log_debug(f"✅ 레버리지 설정 성공 ({symbol})", f"{leverage}x")
        else:
            log_debug(f"❌ 레버리지 설정 실패 ({symbol})", response.text)
    except Exception as e:
        log_debug(f"❌ 레버리지 설정 예외 발생 ({symbol})", str(e))

# 나머지 서버 코드는 기존과 동일하게 이어서 붙이세요.
