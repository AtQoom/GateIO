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

# 환경 변수
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

# 거래소 심볼 매핑
BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

# 종목별 설정
SYMBOL_CONFIG = {
    "ADA_USDT": {
        "min_qty": Decimal("10"),
        "qty_step": Decimal("10"),
        "sl_pct": Decimal("0.0075"),
        "tp_pct": Decimal("0.008"),
        "leverage": 3
    },
    "BTC_USDT": {
        "min_qty": Decimal("0.0001"),
        "qty_step": Decimal("0.0001"),
        "sl_pct": Decimal("0.004"),
        "tp_pct": Decimal("0.006"),
        "leverage": 5
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "sl_pct": Decimal("0.0075"),
        "tp_pct": Decimal("0.008"),
        "leverage": 3
    }
}

# Gate.io API 클라이언트 초기화
config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

# 상태 저장 변수
position_state = {}
account_cache = {"time": 0, "data": None}

# 로깅 유틸리티
def log_debug(tag, msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {msg}")

# 계정 정보 조회
def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        avail = Decimal(str(acc.available))
        account_cache.update({"time": now, "data": avail})
        log_debug("💰 계정", f"가용 잔고: {avail}")
        return avail
    except Exception as e:
        log_debug("❌ 계정 조회 실패", str(e))
        return Decimal("0")

# 포지션 상태 업데이트
def update_position_state(symbol):
    try:
        pos = api.get_position(SETTLE, symbol)
        size = Decimal(str(pos.size))
        lev = Decimal(str(pos.leverage or SYMBOL_CONFIG[symbol]["leverage"]))
        if size != 0:
            entry = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark
            margin = value / lev
            position_state[symbol] = {
                "price": entry,
                "side": "buy" if size > 0 else "sell",
                "leverage": lev,
                "size": abs(size),
                "value": value,
                "margin": margin
            }
        else:
            position_state[symbol] = {
                "price": None, "side": None, "leverage": lev,
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
            }
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))

# 가격 조회
def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        price = Decimal(str(ticker[0].last))
        return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
        return Decimal("0")

# 최대 주문 수량 계산
def get_max_qty(symbol, side):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        safe = get_account_info(force=True)
        price = get_price(symbol)
        if price <= 0:
            return float(cfg["min_qty"])
        lev = Decimal(cfg["leverage"])
        order_value = safe * lev
        raw_qty = order_value / price
        step = cfg["qty_step"]
        qty = (raw_qty // step) * step
        qty = max(qty, cfg["min_qty"])
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e))
        return float(cfg["min_qty"])

# 주문 실행
def place_order(symbol, side, qty, reduce_only=False, retry=3):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        order_qty = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        order_qty = max(order_qty, cfg["min_qty"])
        size = float(order_qty) if side == "buy" else -float(order_qty)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 ({symbol})", f"{side.upper()} {order_qty}")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", error_msg)
        if retry > 0 and any(err in error_msg for err in ["INVALID_PARAM", "INSUFFICIENT_AVAILABLE"]):
            retry_qty = max(Decimal(str(qty)) * Decimal("0.5"), cfg["min_qty"])
            log_debug(f"🔄 재시도 ({symbol})", f"수량: {qty} → {retry_qty}")
            time.sleep(1)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False

# 포지션 청산
def close_position(symbol):
    try:
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 청산 완료 ({symbol})", "")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
        return False

# 웹훅 처리
@app.route("/", methods=["POST"])
def webhook():
    symbol = None
    try:
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
            
        data = request.get_json()
        log_debug("📥 웹훅", json.dumps(data))
        
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400
            
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        if side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "Invalid side/action"}), 400

        log_debug("📩 웹훅 수신", f"Symbol: {symbol}, Action: {action}, Side: {side}")
        
        update_position_state(symbol)
        current = position_state.get(symbol, {}).get("side")
        desired = "buy" if side == "long" else "sell"

        if action == "exit":
            if close_position(symbol):
                return jsonify({"status": "success", "message": "Position closed"})
            else:
                return jsonify({"status": "error", "message": "Close failed"}), 500

        if current and current != desired:
            if not close_position(symbol):
                return jsonify({"status": "error", "message": "Reverse close failed"}), 500
            time.sleep(1)
            update_position_state(symbol)

        qty = get_max_qty(symbol, desired)
        success = place_order(symbol, desired, qty)
        return jsonify({"status": "success" if success else "error", "qty": qty})

    except Exception as e:
        error_symbol = symbol or "unknown"
        log_debug(f"❌ 웹훅 오류 ({error_symbol})", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

# 상태 조회
@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
            pos = position_state.get(sym, {})
            positions[sym] = {k: float(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": positions
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

# 웹소켓 리스너 (오류 수정)
async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30) as ws:
                log_debug("📡 웹소켓", f"Connected to {uri}")
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                reconnect_delay = 5
                
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        
                        # 데이터 타입 확인 (핵심 수정)
                        if not isinstance(data, dict):
                            continue
                            
                        # event 필드가 있으면 구독 확인 메시지
                        if "event" in data:
                            if data["event"] == "subscribe":
                                log_debug("✅ 웹소켓 구독", data.get("channel", ""))
                            continue
                        
                        # result 필드 안전하게 처리
                        result = data.get("result")
                        if not isinstance(result, dict):
                            continue
                            
                        contract = result.get("contract")
                        last = result.get("last")
                        
                        if not contract or not last or contract not in SYMBOL_CONFIG:
                            continue
                            
                        price = Decimal(str(last))
                        pos = position_state.get(contract, {})
                        
                        if pos.get("side") and pos.get("price"):
                            cfg = SYMBOL_CONFIG[contract]
                            entry = pos["price"]
                            
                            # SL/TP 조건 계산
                            if pos["side"] == "buy":
                                sl_price = entry * (1 - cfg["sl_pct"])
                                tp_price = entry * (1 + cfg["tp_pct"])
                                if price <= sl_price:
                                    log_debug(f"🛑 손절 ({contract})", f"현재가: {price}, SL: {sl_price}")
                                    close_position(contract)
                                elif price >= tp_price:
                                    log_debug(f"🎯 익절 ({contract})", f"현재가: {price}, TP: {tp_price}")
                                    close_position(contract)
                            elif pos["side"] == "sell":
                                sl_price = entry * (1 + cfg["sl_pct"])
                                tp_price = entry * (1 - cfg["tp_pct"])
                                if price >= sl_price:
                                    log_debug(f"🛑 손절 ({contract})", f"현재가: {price}, SL: {sl_price}")
                                    close_position(contract)
                                elif price <= tp_price:
                                    log_debug(f"🎯 익절 ({contract})", f"현재가: {price}, TP: {tp_price}")
                                    close_position(contract)
                                        
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        log_debug("⚠️ 웹소켓", "연결 재시도 중...")
                        break
                    except Exception as e:
                        log_debug("❌ 웹소켓 메시지 처리 실패", str(e))
                        # 개별 메시지 오류는 연결을 끊지 않음
                        continue
                        
        except Exception as e:
            log_debug("❌ 웹소켓 연결 실패", f"{str(e)} - {reconnect_delay}초 후 재시도")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

# 웹소켓 스레드 시작
def start_ws_listener():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

if __name__ == "__main__":
    # 웹소켓 리스너 시작
    threading.Thread(target=start_ws_listener, daemon=True).start()
    
    # Flask 서버 시작
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
