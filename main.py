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

# API 설정
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
DEFAULT_STOP_LOSS = 0.0075  # 기본 손절

# 심볼 설정
SYMBOL_CONFIG = {
    "ADAUSDT": {"symbol": "ADA_USDT", "min_qty": 10, "qty_step": 10},
    "BTCUSDT": {"symbol": "BTC_USDT", "min_qty": 0.0001, "qty_step": 0.0001},
    "SUIUSDT": {"symbol": "SUI_USDT", "min_qty": 1, "qty_step": 1},
}

# API 클라이언트
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
        return max(total_equity - used_margin, 0) / len(SYMBOL_CONFIG)
    except Exception as e:
        log_debug("❌ 사용 가능 증거금 계산 실패", str(e))
        return 0

def get_stop_loss_pct(symbol_key):
    return 0.004 if symbol_key == "BTCUSDT" else DEFAULT_STOP_LOSS

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
        position_state[symbol_key] = {"price": None, "side": None}

def close_position(symbol_key):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        pos = api_instance.get_position(SETTLE, symbol)
        if float(pos.size) == 0:
            log_debug(f"📭 포지션 없음 ({symbol_key})", "청산할 포지션 없음")
            return
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 청산 성공 ({symbol_key})", result.to_dict())
        attempts = 0
        while attempts < 10:
            pos = api_instance.get_position(SETTLE, symbol)
            if float(pos.size) == 0:
                break
            time.sleep(0.5)
            attempts += 1
        position_state[symbol_key] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol_key})", str(e))

def get_max_qty(symbol_key):
    try:
        config = SYMBOL_CONFIG[symbol_key]
        available = get_available_equity()
        pos = api_instance.get_position(SETTLE, config["symbol"])
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
        log_debug(f"❌ {symbol_key} 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol_key]["min_qty"]

def place_order(symbol_key, side, qty, reduce_only=False):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol_key})", result.to_dict())
        if not reduce_only:
            fill_price = float(result.fill_price) if result.fill_price else 0
            position_state[symbol_key] = {"price": fill_price, "side": side}
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol_key})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = [v["symbol"] for v in SYMBOL_CONFIG.values()]
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                log_debug("📡 WebSocket", f"연결 성공 - 구독 중: {symbols}")
                while True:
                    try:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if 'event' in data and data['event'] == 'subscribe':
                            log_debug("✅ 구독 완료", f"{data.get('channel')} - {data.get('payload')}")
                            continue
                        if 'result' not in data or not isinstance(data['result'], dict):
                            continue
                        symbol = data['result'].get("contract")
                        price = float(data['result'].get("last", 0))
                        if not symbol or price <= 0:
                            continue
                        symbol_key = next((k for k, v in SYMBOL_CONFIG.items() if v["symbol"] == symbol), None)
                        if not symbol_key:
                            continue
                        update_position_state(symbol_key)
                        state = position_state.get(symbol_key, {})
                        entry_price = state.get("price")
                        entry_side = state.get("side")
                        if not entry_price or not entry_side:
                            continue
                        sl_pct = get_stop_loss_pct(symbol_key)
                        sl_hit = (
                            (entry_side == "buy" and price <= entry_price * (1 - sl_pct)) or
                            (entry_side == "sell" and price >= entry_price * (1 + sl_pct))
                        )
                        if sl_hit:
                            log_debug(f"🛑 손절 조건 충족 ({symbol_key})", f"{price=}, {entry_price=}, {sl_pct=}")
                            close_position(symbol_key)
                    except Exception as e:
                        log_debug("❌ WebSocket 메시지 오류", str(e))
        except Exception as e:
            log_debug("❌ WebSocket 연결 실패", str(e))
            await asyncio.sleep(5)

def start_price_listener():
    for symbol_key in SYMBOL_CONFIG:
        update_position_state(symbol_key)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        symbol_key = data.get("symbol", "").upper()
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()

        if signal == "buy":
            signal = "long"
        elif signal == "sell":
            signal = "short"

        if signal not in ["long", "short"] or action not in ["entry", "exit"] or symbol_key not in SYMBOL_CONFIG:
            return jsonify({"error": "유효하지 않은 요청"}), 400

        update_position_state(symbol_key)
        state = position_state.get(symbol_key, {})
        if action == "exit":
            close_position(symbol_key)
            return jsonify({"status": "청산 완료"})

        entry_side = state.get("side")
        if entry_side and (
            (signal == "long" and entry_side == "sell") or
            (signal == "short" and entry_side == "buy")
        ):
            log_debug(f"🔁 반대포지션 감지 ({symbol_key})", f"{entry_side=} → 청산 후 재진입")
            close_position(symbol_key)
            time.sleep(0.5)

        qty = get_max_qty(symbol_key)
        place_order(symbol_key, "buy" if signal == "long" else "sell", qty)
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
