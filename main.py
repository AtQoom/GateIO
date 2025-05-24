import os
import json
import time
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {
        "min_qty": Decimal("10"),
        "qty_step": Decimal("10"),
        "sl_pct": Decimal("0.0075"),
        "leverage": 3
    },
    "BTC_USDT": {
        "min_qty": Decimal("0.001"),
        "qty_step": Decimal("0.0001"),
        "sl_pct": Decimal("0.004"),
        "leverage": 5
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "sl_pct": Decimal("0.0075"),
        "leverage": 3
    }
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}
account_cache = {"time": 0, "data": None}

def log_debug(tag, msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {msg}")

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
                "price": entry, "side": "buy" if size > 0 else "sell",
                "leverage": lev, "size": abs(size), "value": value, "margin": margin
            }
        else:
            position_state[symbol] = {"price": None, "side": None, "leverage": lev,
                                      "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")}
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))

def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        price = Decimal(str(ticker[0].last))
        log_debug("💲 가격", f"{symbol} = {price}")
        return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
        return Decimal("0")

def get_max_qty(symbol, side):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        safe = get_account_info(force=True)
        price = get_price(symbol)
        lev = Decimal(cfg["leverage"])
        raw = (safe * lev) / price if price > 0 else Decimal("0")
        qty = (raw // cfg["qty_step"]) * cfg["qty_step"]
        qty = max(qty, cfg["min_qty"])
        log_debug(f"📐 수량 ({symbol})", f"{qty}")
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e))
        return float(SYMBOL_CONFIG[symbol]["min_qty"])

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        order_qty = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        order_qty = max(order_qty, cfg["min_qty"])
        size = float(order_qty) if side == "buy" else -float(order_qty)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 ({symbol})", f"{side.upper()} {order_qty} @ {getattr(result, 'fill_price', 'N/A')}")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        err = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", err)
        if retry > 0 and any(err_type in err for err_type in ["INVALID_PARAM", "INSUFFICIENT_AVAILABLE", "Bad Request"]):
            retry_qty = (Decimal(str(qty)) * Decimal("0.5")).quantize(step, rounding=ROUND_DOWN)
            retry_qty = max(retry_qty, cfg["min_qty"])
            log_debug(f"🔄 재시도 ({symbol})", f"{qty} → {retry_qty}")
            time.sleep(1)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry - 1)
        return False

def close_position(symbol):
    try:
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
        return False

@app.route("/", methods=["POST"])
def webhook():
    symbol = None  # 초기화 추가
    try:
        if not request.is_json:
            log_debug("⚠️ 잘못된 요청", "JSON 형식이 아님")
            return jsonify({"error": "JSON 형식만 허용됩니다"}), 400

        data = request.get_json()
        log_debug("📥 웹훅 원본 데이터", json.dumps(data))
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw, raw)

        if symbol not in SYMBOL_CONFIG:
            log_debug("⚠️ 알 수 없는 심볼", symbol)
            return jsonify({"error": "지원하지 않는 심볼입니다"}), 400

        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        log_debug("📩 웹훅 수신", f"심볼: {symbol}, 신호: {side}, 액션: {action}")

        update_position_state(symbol)
        desired = "buy" if side == "long" else "sell"
        current = position_state.get(symbol, {}).get("side")

        if action == "exit":
            if close_position(symbol):
                return jsonify({"status": "success", "message": "청산 완료"})
            else:
                return jsonify({"status": "error", "message": "청산 실패"}), 500

        if current and current != desired:
            if not close_position(symbol):
                return jsonify({"status": "error", "message": "반대 포지션 청산 실패"}), 500
            time.sleep(1)
            update_position_state(symbol)

        qty = get_max_qty(symbol, desired)
        success = place_order(symbol, desired, qty)
        return jsonify({"status": "success" if success else "error", "qty": qty})

    except Exception as e:
        error_symbol = symbol if symbol else "unknown_symbol"
        log_debug(f"❌ 웹훅 처리 실패 ({error_symbol})", str(e))
        return jsonify({"status": "error", "message": str(e), "symbol": error_symbol}), 500

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
            positions[sym] = position_state.get(sym, {})
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": {k: {sk: (float(sv) if isinstance(sv, Decimal) else sv) for sk, sv in v.items()} 
                        for k, v in positions.items()}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    # 웹소켓 리스너 스레드 시작 (필요시)
    # threading.Thread(target=start_price_listener, daemon=True).start()
    
    # 서버 시작 로그
    log_debug("🚀 서버 시작", f"포트 {os.environ.get('PORT', 8080)}")
    
    # Flask 서버 실행
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
