import os
import json
import time
import asyncio
import threading
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

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}
account_cache = {"time": 0, "data": None}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

# 계속 이어짐 (2/2)

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    payload = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": payload
                }))
                log_debug("📡 WebSocket", f"연결 성공 - {payload}")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)

                    # "result"는 list일 수 있음 → 방어 처리
                    result = data.get("result", None)
                    if not isinstance(result, dict):
                        log_debug("⚠️ 잘못된 result 형식", f"{type(result)}")
                        continue

                    contract = result.get("contract")
                    last = result.get("last")

                    if not contract or contract not in SYMBOL_CONFIG:
                        continue

                    last_price = Decimal(str(last))
                    state = position_state.get(contract, {})
                    entry_price = state.get("price")
                    side = state.get("side")
                    if not entry_price or not side:
                        continue

                    sl = SYMBOL_CONFIG[contract]["sl_pct"]
                    if (side == "buy" and last_price <= entry_price * (1 - sl)) or \
                       (side == "sell" and last_price >= entry_price * (1 + sl)):
                        log_debug(f"🛑 손절 발생 ({contract})", f"현재가: {last_price}, 진입가: {entry_price}, 손절폭: {sl}")
                        close_position(contract)

        except Exception as e:
            log_debug("❌ WS 오류", str(e))
            await asyncio.sleep(5)

def start_price_listener():
    for sym, lev in SYMBOL_LEVERAGE.items():
        log_debug(f"⚠️ 레버리지 설정 미지원 ({sym})", f"{lev}x (Gate.io SDK 버전 제한)")
    for sym in SYMBOL_CONFIG:
        update_position_state(sym)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        log_debug("📥 웹훅 수신", json.dumps(data))

        raw_symbol = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw_symbol, raw_symbol)
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()

        if side == "buy": side = "long"
        elif side == "sell": side = "short"
        else: return jsonify({"error": "Invalid side"}), 400

        desired = "buy" if side == "long" else "sell"

        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "closed", "symbol": symbol})

        update_position_state(symbol)
        state = position_state.get(symbol, {})
        if state.get("side") and state.get("side") != desired:
            close_position(symbol)
            time.sleep(1)

        qty = get_max_qty(symbol, desired)
        place_order(symbol, desired, qty)
        return jsonify({"status": "entry", "symbol": symbol, "side": desired})
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e))
        return jsonify({"error": str(e)}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
        return jsonify({
            "status": "running",
            "equity": float(equity),
            "positions": {
                k: {sk: float(sv) if isinstance(sv, Decimal) else sv for sk, sv in v.items()}
                for k, v in position_state.items()
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
