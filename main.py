import os
import json
import time
import asyncio
import threading
import websockets
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

# 🔧 심볼 변환: Binance → Gate
BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"symbol": "ADA_USDT", "min_qty": 10, "qty_step": 10, "sl_pct": 0.0075},
    "BTC_USDT": {"symbol": "BTC_USDT", "min_qty": 0.0001, "qty_step": 0.0001, "sl_pct": 0.004},
    "SUI_USDT": {"symbol": "SUI_USDT", "min_qty": 1, "qty_step": 1, "sl_pct": 0.0075}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

position_state = {}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_equity():
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        return float(accounts.available)
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return 0

def get_available_equity():
    try:
        total_equity = get_equity()
        positions = api_instance.list_futures_positions(SETTLE)
        used_margin = sum([
            abs(float(p.size) * float(p.entry_price)) / float(p.leverage)
            for p in positions if hasattr(p, 'size') and hasattr(p, 'entry_price') and hasattr(p, 'leverage')
        ])
        available = max(total_equity - used_margin, 0)
        return available / len(SYMBOL_CONFIG)
    except Exception as e:
        log_debug("❌ 사용 가능 증거금 계산 실패", str(e))
        return 0

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size) if hasattr(pos, 'size') else 0
        if size != 0:
            position_state[symbol] = {
                "price": float(pos.entry_price),
                "side": "buy" if size > 0 else "sell"
            }
        else:
            position_state[symbol] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None}

def close_position(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size)
        if size == 0:
            log_debug(f"📭 포지션 없음 ({symbol})", "청산할 포지션이 없습니다.")
            return
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 전체 청산 성공 ({symbol})", result.to_dict())
        for _ in range(10):
            pos = api_instance.get_position(SETTLE, symbol)
            if float(pos.size) == 0:
                break
            time.sleep(0.5)
        position_state[symbol] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 전체 청산 실패 ({symbol})", str(e))

def get_max_qty(symbol):
    try:
        config = SYMBOL_CONFIG[symbol]
        available = get_available_equity()
        pos = api_instance.get_position(SETTLE, symbol)
        leverage = float(pos.leverage)
        mark_price = float(pos.mark_price)
        raw_qty = available * leverage / mark_price
        step = Decimal(str(config["qty_step"]))
        qty = (Decimal(str(raw_qty)) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
        return max(float(qty), config["min_qty"])
    except Exception as e:
        log_debug(f"❌ {symbol} 최대 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol]["min_qty"]

def place_order(symbol, side, qty, reduce_only=False):
    try:
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", result.to_dict())
        if not reduce_only:
            position_state[symbol] = {
                "price": float(result.fill_price or 0),
                "side": side
            }
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    contracts = [v["symbol"] for v in SYMBOL_CONFIG.values()]
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": contracts
                }))
                log_debug("📡 WebSocket", f"연결 성공 - 구독 중: {contracts}")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "result" not in data or not isinstance(data["result"], dict):
                        continue
                    symbol = data["result"].get("contract")
                    if symbol not in SYMBOL_CONFIG:
                        continue
                    price = float(data["result"].get("last", 0))
                    if price <= 0:
                        continue
                    update_position_state(symbol)
                    state = position_state.get(symbol, {})
                    entry_price = state.get("price")
                    entry_side = state.get("side")
                    if not entry_price or not entry_side:
                        continue
                    sl_pct = SYMBOL_CONFIG[symbol]["sl_pct"]
                    sl_hit = (
                        entry_side == "buy" and price <= entry_price * (1 - sl_pct) or
                        entry_side == "sell" and price >= entry_price * (1 + sl_pct)
                    )
                    if sl_hit:
                        log_debug(f"🛑 손절 조건 충족 ({symbol})", f"{price=}, {entry_price=}")
                        close_position(symbol)
        except Exception as e:
            log_debug("❌ WebSocket 연결 오류", str(e))
            await asyncio.sleep(5)

def start_price_listener():
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()
        symbol_raw = data.get("symbol", "").upper()
        symbol = BINANCE_TO_GATE_SYMBOL.get(symbol_raw, symbol_raw)
        if symbol not in SYMBOL_CONFIG or signal not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "유효하지 않은 요청"}), 400
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "청산 완료"})
        side = "buy" if signal == "long" else "sell"
        entry_side = state.get("side")
        if entry_side and ((signal == "long" and entry_side == "sell") or (signal == "short" and entry_side == "buy")):
            log_debug(f"🔁 반대포지션 감지 ({symbol})", f"{entry_side=} → 청산 후 재진입")
            close_position(symbol)
            time.sleep(0.5)
        qty = get_max_qty(symbol)
        place_order(symbol, side, qty)
        return jsonify({"status": "진입 완료", "symbol": symbol, "qty": qty})
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
