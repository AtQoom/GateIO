
import os
import json
import time
import asyncio
import threading
import requests  # <-- Required for REST leverage setting
import websockets
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

# Gate REST API leverage set (workaround for SDK)
def set_leverage(symbol, leverage):
    try:
        url = f"https://api.gateio.ws/api/v4/futures/usdt/positions/{symbol}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "KEY": API_KEY,
            "Timestamp": str(int(time.time())),
        }
        payload = {"leverage": str(leverage)}
        r = requests.post(url, headers=headers, data=json.dumps(payload), auth=(API_KEY, API_SECRET))
        if r.status_code == 200:
            print(f"[레버리지 설정 성공] {symbol} → {leverage}x")
        else:
            print(f"[레버리지 설정 실패] {symbol} → {leverage}x 응답: {r.text}")
    except Exception as e:
        print(f"[레버리지 설정 예외] {symbol}: {str(e)}")

# Placeholder body for rest of the code (preserved structure)
if __name__ == "__main__":
    print("🚀 서버가 시작됩니다. (샘플 구조)")
