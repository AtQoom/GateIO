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
MARGIN_BUFFER = Decimal("0.6")  # 안전하게 일부만 사용

# TradingView → Gate 심볼 매핑
BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

# 심볼별 설정
SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": Decimal("10"), "qty_step": Decimal("10"), "sl_pct": Decimal("0.0075")},
    "BTC_USDT": {"min_qty": Decimal("0.0001"), "qty_step": Decimal("0.0001"), "sl_pct": Decimal("0.004")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "sl_pct": Decimal("0.0075")}
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
                "leverage": Decimal(str(pos.leverage))
            }
            log_debug(f"📊 포지션 ({symbol})", f"{position_state[symbol]}")
        else:
            position_state[symbol] = {"price": None, "side": None, "leverage": Decimal("1")}
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None, "leverage": Decimal("1")}

def get_price(symbol):
    try:
        tickers = api.list_futures_tickers(SETTLE, contract=symbol)
        return Decimal(str(tickers[0].last))
    except:
        return Decimal("0")

def get_max_qty(symbol, side):
    """원래 코드 그대로 수량 계산 (하지만 주문시 33%만 사용)"""
    try:
        cfg = SYMBOL_CONFIG[symbol]
        safe = get_account_info()
        update_position_state(symbol)
        price = get_price(symbol)
        leverage = position_state[symbol].get("leverage", Decimal("1"))
        order_value = safe * leverage  # 할당비율 없이 전체 가용 증거금 사용
        raw_qty = order_value / price
        step = cfg["qty_step"]
        qty = (Decimal(str(raw_qty)) // step) * step
        qty = max(qty, cfg["min_qty"])
        log_debug(f"📐 최대수량 ({symbol})", f"{qty} @ {price} (레버리지: {leverage})")
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e))
        return float(SYMBOL_CONFIG[symbol]["min_qty"])

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    """수량의 33%만 실제로 주문"""
    try:
        if qty <= 0:
            log_debug("⛔ 수량 0 이하", symbol)
            return False
            
        # 여기서 수량의 33%만 사용 (핵심 수정 부분)
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        reduced_qty = Decimal(str(qty)) * Decimal("0.33")  # 33%만 사용
        reduced_qty = (reduced_qty // step) * step  # 최소 단위로 내림
        reduced_qty = max(reduced_qty, cfg["min_qty"])  # 최소 수량 보장
        
        size = float(reduced_qty) if side == "buy" else -float(reduced_qty)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api.create_futures_order(SETTLE, order)
        
        log_debug(f"✅ 주문 ({symbol})", f"{side.upper()} {float(reduced_qty)} @ {result.fill_price} (원래: {qty}의 33%)")
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", error_msg)
        
        # 증거금 부족 오류시 수량 더 감소 후 재시도
        if retry > 0 and ("INSUFFICIENT_AVAILABLE" in error_msg or "Bad Request" in error_msg):
            cfg = SYMBOL_CONFIG[symbol]
            step = cfg["qty_step"]
            retry_qty = Decimal(str(qty)) * Decimal("0.2")  # 33%의 60% = 약 20%
            retry_qty = (retry_qty // step) * step
            retry_qty = max(retry_qty, cfg["min_qty"])
            
            log_debug(f"🔄 주문 재시도 ({symbol})", f"수량 감소: {qty} → {float(retry_qty)}")
            time.sleep(1)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
            
        return False

def close_position(symbol):
    try:
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        api.create_futures_order(SETTLE, order)
        position_state[symbol] = {"price": None, "side": None, "leverage": Decimal("1")}
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
                log_debug("📡 WebSocket", "연결 성공")
                
                while True:
                    try:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        
                        # 이벤트 메시지 처리
                        if 'event' in data:
                            continue
                            
                        # 결과 데이터 처리
                        if "result" not in data:
                            continue
                            
                        # 결과가 딕셔너리인지 확인 (핵심 오류 수정)
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
            log_debug("❌ WS 오류", str(e))
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
