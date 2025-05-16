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

# 🔐 환경변수에서 API 키 로딩
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
STOP_LOSS_PCT = 0.0075

# ✅ 코인별 세부 설정
SYMBOL_CONFIG = {
    "ADAUSDT": {"symbol": "ADA_USDT", "min_qty": 10, "qty_step": 10},
    "BTCUSDT": {"symbol": "BTC_USDT", "min_qty": 0.0001, "qty_step": 0.0001},
    "SUIUSDT": {"symbol": "SUI_USDT", "min_qty": 1, "qty_step": 1},
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

# 📊 상태 저장
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
            for p in positions
        ])
        available = max(total_equity - used_margin, 0)
        return available / len(SYMBOL_CONFIG)
    except Exception as e:
        log_debug("❌ 사용 가능 증거금 계산 실패", str(e))
        return 0

def update_position_state(symbol_key):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size)
        if size != 0:
            position_state[symbol_key] = {
                "price": float(pos.entry_price),
                "side": "buy" if size > 0 else "sell"
            }
        else:
            position_state[symbol_key] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol_key})", str(e))

def close_position(symbol_key):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 전체 청산 성공 ({symbol_key})", result.to_dict())
        # 🔁 포지션 종료 확인
        while True:
            pos = api_instance.get_position(SETTLE, symbol)
            if float(pos.size) == 0:
                break
            time.sleep(0.5)
        position_state[symbol_key] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 전체 청산 실패 ({symbol_key})", str(e))

def get_max_qty(symbol_key):
    try:
        config = SYMBOL_CONFIG[symbol_key]
        symbol = config["symbol"]
        available = get_available_equity()
        pos = api_instance.get_position(SETTLE, symbol)
        leverage = float(pos.leverage)
        mark_price = float(pos.mark_price)
        raw_qty = available * leverage / mark_price

        step = Decimal(str(config["qty_step"]))
        raw_qty_dec = Decimal(str(raw_qty))
        quantized = (raw_qty_dec / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
        qty = float(quantized)

        log_debug(f"📐 {symbol_key} 수량 계산", f"{qty=}, {available=}, {leverage=}, {mark_price=}")
        return max(qty, config["min_qty"])
    except Exception as e:
        log_debug(f"❌ {symbol_key} 최대 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol_key]["min_qty"]

def place_order(symbol_key, side, qty, reduce_only=False):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        size = qty if side == "buy" else -qty
        if reduce_only:
            size = -size
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol_key})", result.to_dict())
        if not reduce_only:
            position_state[symbol_key] = {
                "price": float(result.fill_price or 0),
                "side": side
            }
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol_key})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = [v["symbol"] for v in SYMBOL_CONFIG.values()]
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "time": int(time.time()),
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": symbols
        }))
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            if 'result' in data and isinstance(data['result'], dict):
                symbol = data['result']['contract']
                symbol_key = next((k for k, v in SYMBOL_CONFIG.items() if v["symbol"] == symbol), None)
                if not symbol_key:
                    continue
                price = float(data['result'].get("last", 0))
                update_position_state(symbol_key)
                state = position_state.get(symbol_key, {})
                entry_price = state.get("price")
                entry_side = state.get("side")

                if not price or not entry_price or not entry_side:
                    continue

                sl_hit = (
                    (entry_side == "buy" and price <= entry_price * (1 - STOP_LOSS_PCT)) or
                    (entry_side == "sell" and price >= entry_price * (1 + STOP_LOSS_PCT))
                )
                if sl_hit:
                    log_debug(f"🛑 손절 조건 충족 ({symbol_key})", f"{price=}, {entry_price=}")
                    close_position(symbol_key)

def start_price_listener():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()
        symbol_key = data.get("symbol", "").upper()

        if signal == "buy":
            signal = "long"
        elif signal == "sell":
            signal = "short"

        if signal not in ["long", "short"] or action not in ["entry", "exit"] or symbol_key not in SYMBOL_CONFIG:
            return jsonify({"error": "invalid request"}), 400

        update_position_state(symbol_key)
        state = position_state.get(symbol_key, {})

        if action == "exit":
            close_position(symbol_key)
            return jsonify({"status": "청산 완료"})

        side = "buy" if signal == "long" else "sell"
        entry_side = state.get("side")

        if (signal == "long" and entry_side == "sell") or (signal == "short" and entry_side == "buy"):
            log_debug(f"🔁 반대포지션 감지 ({symbol_key})", f"{entry_side=} → 청산 후 재진입")
            close_position(symbol_key)
            time.sleep(0.5)

        qty = get_max_qty(symbol_key)
        place_order(symbol_key, side, qty)
        return jsonify({"status": "진입 완료", "symbol": symbol_key, "qty": qty})

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
