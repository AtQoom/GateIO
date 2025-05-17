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
MARGIN_BUFFER = Decimal("0.55")  # 55%만 사용 (더 보수적으로)

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": Decimal("10"), "qty_step": Decimal("10"), "sl_pct": Decimal("0.0075"), "min_order_usdt": Decimal("1")},
    "BTC_USDT": {"min_qty": Decimal("0.0001"), "qty_step": Decimal("0.0001"), "sl_pct": Decimal("0.004"), "min_order_usdt": Decimal("1")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "sl_pct": Decimal("0.0075"), "min_order_usdt": Decimal("1")}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}
account_cache = {"time": 0, "data": None}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        accounts = api.list_futures_accounts(SETTLE)
        available = Decimal(str(accounts.available))
        safe = available * MARGIN_BUFFER
        account_cache.update({"time": now, "data": safe})
        log_debug("💰 잔고", f"가용: {available}, 안전가용: {safe}")
        return safe
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return Decimal("100")

def update_position_state(symbol):
    try:
        pos = api.get_position(SETTLE, symbol)
        size = Decimal(str(getattr(pos, "size", "0")))
        if size != 0:
            position_state[symbol] = {
                "price": Decimal(str(pos.entry_price)),
                "side": "buy" if size > 0 else "sell",
                "leverage": Decimal(str(pos.leverage)),
                "mark_price": Decimal(str(pos.mark_price)),
                "size": size
            }
            log_debug(f"📊 포지션 ({symbol})", f"{position_state[symbol]}")
        else:
            position_state[symbol] = {"price": None, "side": None, "leverage": Decimal("1"), "mark_price": Decimal("0"), "size": Decimal("0")}
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None, "leverage": Decimal("1"), "mark_price": Decimal("0"), "size": Decimal("0")}

def get_price(symbol):
    try:
        tickers = api.list_futures_tickers(SETTLE, contract=symbol)
        if tickers and hasattr(tickers[0], "last"):
            return Decimal(str(tickers[0].last))
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
    return Decimal("0")

def get_max_qty(symbol, side):
    """전체 가용 증거금 기준 계산 후 33%만 주문"""
    try:
        config = SYMBOL_CONFIG[symbol]
        total_equity = get_account_info()
        price = get_price(symbol)
        if price <= 0:
            return float(config["min_qty"])
        pos = api.get_position(SETTLE, symbol)
        leverage = Decimal(str(getattr(pos, "leverage", "1")))
        # 전체 증거금으로 계산한 최대 수량 (100%)
        raw_max_qty = (total_equity * leverage) / price
        # 33%만 사용 (실제 주문 수량)
        raw_qty = raw_max_qty * Decimal("0.33")
        # 최소 단위 내림 처리
        step = config["qty_step"]
        quantized = (raw_qty // step) * step
        final_qty = max(quantized, config["min_qty"])
        log_debug(f"📊 {symbol} 주문 계획", 
                f"총가용: {total_equity}, 레버리지: {leverage}, "
                f"최대수량: {raw_max_qty}, 주문수량(33%): {final_qty}")
        return float(final_qty)
    except Exception as e:
        log_debug(f"❌ {symbol} 수량 계산 실패", str(e))
        return float(SYMBOL_CONFIG[symbol]["min_qty"])

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    try:
        if qty <= 0:
            log_debug("⛔ 수량 0 이하", symbol)
            return
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 ({symbol})", f"{side.upper()} {qty} @ {result.fill_price}")
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol})", str(e))
        if retry > 0:
            config = SYMBOL_CONFIG[symbol]
            step = config["qty_step"]
            reduced = max(Decimal(str(qty)) * Decimal("0.6"), config["min_qty"])
            reduced = (reduced // step) * step
            time.sleep(1)
            place_order(symbol, side, float(reduced), reduce_only, retry - 1)
        return False

def close_position(symbol):
    try:
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        api.create_futures_order(SETTLE, order)
        position_state[symbol] = {"price": None, "side": None, "leverage": Decimal("1"), "mark_price": Decimal("0"), "size": Decimal("0")}
        log_debug(f"✅ 청산 완료", symbol)
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))

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
                    try:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if 'event' in data:
                            if data['event'] == 'subscribe':
                                log_debug("✅ 구독 완료", data.get('channel', ''))
                            elif data['event'] in ['ping', 'pong']:
                                pass
                            continue
                        if "result" not in data:
                            continue
                        result = data["result"]
                        if not isinstance(result, dict):
                            continue
                        contract = result.get("contract")
                        last = result.get("last")
                        if not contract or not last or contract not in SYMBOL_CONFIG:
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
                            log_debug(f"🛑 손절 발생 ({contract})", f"{last_price} vs {entry_price}")
                            close_position(contract)
                    except Exception as e:
                        log_debug("❌ 메시지 처리 오류", str(e))
                        continue
        except Exception as e:
            log_debug("❌ WS 연결 오류", str(e))
            await asyncio.sleep(5)

def start_price_listener():
    for sym in SYMBOL_CONFIG:
        update_position_state(sym)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw, raw)
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        if side in ["buy"]: side = "long"
        if side in ["sell"]: side = "short"
        desired = "buy" if side == "long" else "sell"
        if symbol not in SYMBOL_CONFIG or side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "잘못된 요청"}), 400
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        current = state.get("side")
        if action == "exit":
            close_position(symbol)
        elif current and current != desired:
            close_position(symbol)
            time.sleep(1)
            qty = get_max_qty(symbol, desired)
            place_order(symbol, desired, qty)
        else:
            qty = get_max_qty(symbol, desired)
            place_order(symbol, desired, qty)
        return jsonify({"status": "처리 완료", "symbol": symbol, "side": desired})
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e))
        return jsonify({"error": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
