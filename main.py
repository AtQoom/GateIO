import os
import json
import time
import logging
import asyncio
import threading
from decimal import Decimal
from datetime import datetime

import websockets
from flask import Flask, request, jsonify
from gate_api import FuturesApi, FuturesOrder, ApiClient, Configuration

app = Flask(__name__)

# ================== 심볼 및 변환 테이블 ==================
SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("0.0001"),
        "qty_step": Decimal("0.0001"),
        "min_order_usdt": Decimal("5"),
        "sl_pct": Decimal("0.03")
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "min_order_usdt": Decimal("5"),
        "sl_pct": Decimal("0.1")
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "min_order_usdt": Decimal("5"),
        "sl_pct": Decimal("0.1")
    }
}

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

# ================== 환경 변수 및 API ==================
SETTLE = "usdt"
MARGIN_BUFFER = Decimal("0.90")
API_HOST = "https://api.gateio.ws/api/v4"
API_KEY = os.environ.get("GATE_API_KEY", "")
API_SECRET = os.environ.get("GATE_API_SECRET", "")

config = Configuration(host=API_HOST, key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
api = FuturesApi(api_client)

position_state = {}
account_cache = {
    "time": 0,
    "equity": Decimal("0")
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# ================== 유틸리티 ==================
def log_debug(title, message=""):
    logging.info(f"{title} [{message}]")


def get_account_info(force=False):
    current = int(time.time())
    if force or current - account_cache["time"] > 60:
        try:
            account = api.get_futures_account(SETTLE)
            equity = Decimal(str(account.total_equity))
            available = Decimal(str(account.available))
            margin = Decimal(str(account.total_initial_margin))
            unrealized = Decimal(str(account.unrealized_pnl))
            account_cache["time"] = current
            account_cache["equity"] = equity
            log_debug("💰 계정 정보", f"가용: {available}, 총액: {equity}, 미실현손익: {unrealized}, 안전가용: {available * MARGIN_BUFFER}")
            return available * MARGIN_BUFFER
        except Exception as e:
            log_debug(f"❌ 계정정보 조회 실패", str(e))
            if account_cache["equity"] > 0:
                return account_cache["equity"] * MARGIN_BUFFER
            return Decimal("10")
    else:
        return account_cache["equity"] * MARGIN_BUFFER


def get_price(symbol):
    try:
        tickers = api.list_futures_tickers(SETTLE, contract=symbol)
        if tickers and hasattr(tickers[0], "last"):
            price = Decimal(str(tickers[0].last))
            log_debug(f"💲 가격 조회 ({symbol})", f"{price}")
            return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
    return Decimal("0")


def get_current_leverage(symbol):
    try:
        pos = api.get_position(SETTLE, symbol)
        raw_leverage = getattr(pos, "leverage", None)
        if raw_leverage is None or str(raw_leverage).strip() in ["", "0"]:
            leverage = Decimal("5")
            log_debug(f"⚠️ 레버리지 보정 ({symbol})", f"잘못된 값({raw_leverage}) 대신 {leverage}로 처리")
        else:
            leverage = Decimal(str(raw_leverage))
            if leverage <= 0:
                leverage = Decimal("5")
                log_debug(f"⚠️ 레버리지 보정 ({symbol})", f"0 이하 값({raw_leverage}) 대신 {leverage} 사용")
        log_debug(f"⚙️ 레버리지 조회 ({symbol})", f"{leverage}x")
        return leverage
    except Exception as e:
        log_debug(f"❌ 레버리지 조회 실패 ({symbol})", str(e))
        return Decimal("5")


def update_position_state(symbol):
    try:
        pos = api.get_position(SETTLE, symbol)
        size = Decimal(str(getattr(pos, "size", "0")))
        if size != 0:
            entry_price = Decimal(str(getattr(pos, "entry_price", "0")))
            raw_leverage = getattr(pos, "leverage", None)
            if raw_leverage is None or str(raw_leverage).strip() in ["", "0"]:
                leverage = Decimal("5")
                log_debug(f"⚠️ 레버리지 보정 ({symbol})", f"잘못된 값({raw_leverage}) 대신 {leverage}로 처리")
            else:
                leverage = Decimal(str(raw_leverage))
                if leverage <= 0:
                    leverage = Decimal("5")
                    log_debug(f"⚠️ 레버리지 보정 ({symbol})", f"0 이하 값({raw_leverage}) 대신 {leverage} 사용")
            value = Decimal(str(getattr(pos, "value", "0")))
            margin = Decimal(str(getattr(pos, "initial_margin", "0")))
            position_state[symbol] = {
                "price": entry_price,
                "side": "buy" if size > 0 else "sell",
                "leverage": leverage,
                "size": abs(size),
                "value": abs(value),
                "margin": margin
            }
            log_debug(f"📊 포지션 ({symbol})", 
                      f"{position_state[symbol]['side'].upper()} @ {entry_price}, 수량: {abs(size)}, 레버리지: {leverage}x")
        else:
            position_state[symbol] = {
                "price": None, "side": None, "leverage": Decimal("5"),
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
            }
            log_debug(f"📊 포지션 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 정보 조회 실패 ({symbol})", str(e))
        position_state[symbol] = {
            "price": None, "side": None, "leverage": Decimal("5"), 
            "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
        }


def get_max_qty(symbol, side):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        safe = get_account_info()
        price = get_price(symbol)
        if price <= 0:
            log_debug(f"❌ 가격 0 이하 ({symbol})", "최소 수량만 반환")
            return float(cfg["min_qty"])
        leverage = get_current_leverage(symbol)
        order_value = safe * leverage
        raw_qty = order_value / price
        step = cfg["qty_step"]
        qty_decimal = (raw_qty // step) * step
        qty = max(qty_decimal, cfg["min_qty"])
        min_order_usdt = cfg.get("min_order_usdt", Decimal("5"))
        if qty * price < min_order_usdt:
            qty = cfg["min_qty"]
            log_debug(f"⚠️ 최소 주문 금액 미달 ({symbol})", f"{qty * price} USDT < {min_order_usdt} USDT, 최소 수량 사용")
        log_debug(f"📐 최대수량 계산 ({symbol})", 
                f"가용증거금: {safe}, 레버리지: {leverage}x, 가격: {price}, 주문가치: {order_value}, 최대수량: {qty}")
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e))
        return float(SYMBOL_CONFIG[symbol]["min_qty"])


def place_order(symbol, side, qty, reduce_only=False, retry=3):
    try:
        if qty <= 0:
            log_debug("⛔ 수량 0 이하", symbol)
            return False
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        order_qty = Decimal(str(qty))
        if symbol == "BTC_USDT":
            order_qty = (order_qty // Decimal("0.0001")) * Decimal("0.0001")
            if order_qty < Decimal("0.0001"):
                order_qty = Decimal("0.0001")
            str_qty = f"{float(order_qty):.4f}"
            log_debug(f"🔍 BTC 수량 변환", f"원래: {order_qty} → 변환: {str_qty}")
            order_qty = Decimal(str_qty)
        else:
            order_qty = (order_qty // step) * step
        order_qty = max(order_qty, cfg["min_qty"])
        size = float(order_qty) if side == "buy" else -float(order_qty)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api.create_futures_order(SETTLE, order)
        fill_price = result.fill_price if hasattr(result, 'fill_price') else "알 수 없음"
        log_debug(f"✅ 주문 ({symbol})", f"{side.upper()} {float(order_qty)} @ {fill_price} (주문수량: {float(order_qty)})")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", error_msg)
        if retry > 0 and symbol == "BTC_USDT" and "INVALID_PARAM_VALUE" in error_msg:
            retry_qty = Decimal("0.0001")
            log_debug(f"🔄 BTC 최소단위 재시도", f"수량: {retry_qty}")
            time.sleep(1)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry - 1)
        if retry > 0 and ("INSUFFICIENT_AVAILABLE" in error_msg or "Bad Request" in error_msg):
            cfg = SYMBOL_CONFIG[symbol]
            step = cfg["qty_step"]
            retry_qty = Decimal(str(qty)) * Decimal("0.5")
            retry_qty = (retry_qty // step) * step
            retry_qty = max(retry_qty, cfg["min_qty"])
            log_debug(f"🔄 주문 재시도 ({symbol})", f"수량 감소: {qty} → {float(retry_qty)}")
            time.sleep(1)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry - 1)
        return False

def close_position(symbol):
    try:
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 청산 완료", symbol)
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
        return False

# ================== Webhook 및 상태 API ==================
def start_price_listener():
    for sym in SYMBOL_CONFIG:
        update_position_state(sym)
    # price_listener 등 실시간 WS 부분은 필요시 구현

@app.route("/", methods=["POST"])
def webhook():
    try:
        if not request.is_json:
            log_debug("⚠️ 잘못된 요청", "JSON 형식이 아님")
            return jsonify({"error": "JSON 형식만 허용됩니다"}), 400
        data = request.get_json()
        log_debug("📥 웹훅 원본 데이터", json.dumps(data))
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw, raw)
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        strategy = data.get("strategy", "unknown")
        log_debug("📩 웹훅 수신", f"심볼: {symbol}, 신호: {side}, 액션: {action}, 전략: {strategy}")
        if side in ["buy"]: side = "long"
        if side in ["sell"]: side = "short"
        desired = "buy" if side == "long" else "sell"
        if symbol not in SYMBOL_CONFIG:
            log_debug("⚠️ 알 수 없는 심볼", symbol)
            return jsonify({"error": "지원하지 않는 심볼입니다"}), 400
        if side not in ["long", "short"]:
            log_debug("⚠️ 잘못된 방향", side)
            return jsonify({"error": "long 또는 short만 지원합니다"}), 400
        if action not in ["entry", "exit"]:
            log_debug("⚠️ 잘못된 액션", action)
            return jsonify({"error": "entry 또는 exit만 지원합니다"}), 400
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        current = state.get("side")
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "success", "message": "청산 완료", "symbol": symbol})
        if current and current != desired:
            log_debug(f"🔄 반대 포지션 감지 ({symbol})", f"{current} → {desired}로 전환")
            close_position(symbol)
            time.sleep(1)
        get_account_info(force=True)
        qty = get_max_qty(symbol, desired)
        place_order(symbol, desired, qty)
        return jsonify({
            "status": "success", 
            "message": "진입 완료", 
            "symbol": symbol,
            "side": desired
        })
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"status": "error", "message": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        positions = {}
        for symbol in SYMBOL_CONFIG:
            update_position_state(symbol)
            positions[symbol] = position_state.get(symbol, {})
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": {k: {sk: (float(sv) if isinstance(sv, Decimal) else sv) 
                            for sk, sv in v.items()} 
                        for k, v in positions.items()}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", f"WebSocket 리스너 실행됨 - 버전: 1.0.7")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
