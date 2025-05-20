import os
import json
import time
import asyncio
import threading
import requests  # ✅ 누락된 모듈 추가됨
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

def set_leverage(symbol, leverage):
    log_debug(f"⚠️ 레버리지 설정 미지원 ({symbol})", f"{leverage}x (Gate.io SDK 버전 제한)")

def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        accounts = api.list_futures_accounts(SETTLE)
        available = Decimal(str(accounts.available))
        total = Decimal(str(accounts.total))
        unrealised_pnl = Decimal(str(getattr(accounts, 'unrealised_pnl', '0')))
        safe = available * MARGIN_BUFFER
        account_cache.update({"time": now, "data": safe})
        log_debug("💰 계정 정보", f"가용: {available}, 총액: {total}, 미실현손익: {unrealised_pnl}, 안전가용: {safe}")
        return safe
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return Decimal("100")

def update_position_state(symbol):
    try:
        pos = api.get_position(SETTLE, symbol)
        size = Decimal(str(getattr(pos, "size", "0")))
        if size != 0:
            entry_price = Decimal(str(getattr(pos, "entry_price", "0")))
            leverage = Decimal(str(getattr(pos, "leverage", "1")))
            mark_price = Decimal(str(getattr(pos, "mark_price", "0")))
            position_value = abs(size) * mark_price
            margin = position_value / leverage

            position_state[symbol] = {
                "price": entry_price,
                "side": "buy" if size > 0 else "sell",
                "leverage": leverage,
                "size": abs(size),
                "value": position_value,
                "margin": margin
            }
            log_debug(f"📊 포지션 ({symbol})",
                      f"수량: {abs(size)}, 진입가: {entry_price}, 방향: {'롱' if size > 0 else '숏'}, "
                      f"레버리지: {leverage}x, 포지션가치: {position_value}, 증거금: {margin}")
        else:
            position_state[symbol] = {
                "price": None, "side": None, "leverage": Decimal("1"),
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
            }
            log_debug(f"📊 포지션 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
        position_state[symbol] = {
            "price": None, "side": None, "leverage": Decimal("1"),
            "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
        }

def close_position(symbol):
    try:
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 청산 완료", symbol)
        time.sleep(0.5)
        update_position_state(symbol)
        account_cache["time"] = 0
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    payload = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri) as ws:
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
                    if "result" not in data:
                        continue
                    result = data["result"]
                    contract = result.get("contract")
                    last = Decimal(str(result.get("last", "0")))
                    state = position_state.get(contract, {})
                    entry_price = state.get("price")
                    side = state.get("side")
                    if not entry_price or not side:
                        continue
                    sl = SYMBOL_CONFIG[contract]["sl_pct"]
                    if (side == "buy" and last <= entry_price * (1 - sl)) or \
                       (side == "sell" and last >= entry_price * (1 + sl)):
                        log_debug(f"🛑 손절 발생 ({contract})", f"현재가: {last}, 진입가: {entry_price}")
                        close_position(contract)
        except Exception as e:
            log_debug("❌ WS 오류", str(e))
            await asyncio.sleep(5)

def start_price_listener():
    for sym, lev in SYMBOL_LEVERAGE.items():
        set_leverage(sym, lev)
    for sym in SYMBOL_CONFIG:
        update_position_state(sym)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw, raw)
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        log_debug("📩 웹훅 수신", f"심볼: {symbol}, 방향: {side}, 액션: {action}")
        if side in ["buy"]: side = "long"
        if side in ["sell"]: side = "short"
        desired = "buy" if side == "long" else "sell"
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "closed"})
        update_position_state(symbol)
        current = position_state.get(symbol, {}).get("side")
        if current and current != desired:
            close_position(symbol)
            time.sleep(1)
        qty = get_max_qty(symbol, desired)
        place_order(symbol, desired, qty)
        return jsonify({"status": "entry", "symbol": symbol, "side": desired})
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e))
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
