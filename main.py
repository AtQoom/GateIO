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
MARGIN_BUFFER = 0.7
ALLOCATION_RATIO = 0.33

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": 10, "qty_step": 10, "sl_pct": 0.0075},
    "BTC_USDT": {"min_qty": 0.0001, "qty_step": 0.0001, "sl_pct": 0.004},
    "SUI_USDT": {"min_qty": 1, "qty_step": 1, "sl_pct": 0.0075}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

position_state = {}
account_cache = {"time": 0, "data": None}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_account_info(force_refresh=False):
    current_time = time.time()
    if not force_refresh and account_cache["time"] > current_time - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        available = float(getattr(accounts, 'available', 0))
        safe_available = available * MARGIN_BUFFER
        log_debug("💰 계정 정보", f"가용: {available:.2f}, 안전가용: {safe_available:.2f} USDT")
        account_cache["time"] = current_time
        account_cache["data"] = safe_available
        return safe_available
    except Exception as e:
        log_debug("❌ 증거금 조회 실패", str(e))
        return 100 * MARGIN_BUFFER

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(getattr(pos, 'size', 0))
        mark_price = float(getattr(pos, 'mark_price', 0))
        leverage = float(getattr(pos, 'leverage', 1))
        if size != 0:
            position_state[symbol] = {
                "price": float(getattr(pos, 'entry_price', 0)),
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "leverage": leverage,
                "value": abs(size) * mark_price,
                "margin": (abs(size) * mark_price) / leverage
            }
            log_debug(f"📊 포지션 상태 ({symbol})", 
                    f"수량: {abs(size)}, 가격: {position_state[symbol]['price']:.4f}, "
                    f"가치: {position_state[symbol]['value']:.2f}, 증거금: {position_state[symbol]['margin']:.2f}, "
                    f"레버리지: {leverage}x")
        else:
            position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1, "value": 0, "margin": 0}
            log_debug(f"📊 포지션 상태 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1, "value": 0, "margin": 0}

def get_current_price(symbol):
    try:
        tickers = api_instance.list_futures_tickers(settle=SETTLE, contract=symbol)
        if tickers and len(tickers) > 0:
            price = float(tickers[0].last)
            return price
        pos = api_instance.get_position(SETTLE, symbol)
        if hasattr(pos, 'mark_price') and pos.mark_price:
            price = float(pos.mark_price)
            return price
    except Exception as e:
        log_debug(f"⚠️ {symbol} 가격 조회 실패", str(e))
    return 0

def get_max_qty(symbol, desired_side):
    try:
        config = SYMBOL_CONFIG[symbol]
        safe_available = get_account_info()
        current_price = get_current_price(symbol)
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        leverage = state.get("leverage", 1)
        target_margin = safe_available * ALLOCATION_RATIO
        current_side = state.get("side")
        current_margin = state.get("margin", 0)
        if current_side == desired_side and current_margin > 0:
            additional_margin = max(target_margin - current_margin, 0)
        else:
            additional_margin = target_margin
        order_value = additional_margin * leverage
        raw_qty = order_value / current_price

        # 수량을 반드시 int로, 최소단위의 정수배로 내림
        step = config["qty_step"]
        min_qty = config["min_qty"]
        qty = int(raw_qty // step * step)
        if qty < min_qty:
            qty = min_qty
        log_debug(f"📊 {symbol} 주문 계획", 
                f"가격: {current_price:.4f}, 주문수량: {qty}")
        return qty
    except Exception as e:
        log_debug(f"❌ {symbol} 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol]["min_qty"]

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    if qty <= 0:
        log_debug(f"⚠️ 주문 무시 ({symbol})", "수량이 0 이하")
        return False
    try:
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        fill_price = float(getattr(result, 'fill_price', 0))
        fill_size = float(getattr(result, 'size', 0))
        log_debug(f"✅ 주문 성공 ({symbol})", f"수량: {fill_size}, 체결가: {fill_price}")
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", error_msg)
        # 주문 단위 오류/증거금 부족 시 수량 10의 배수로 줄여 재시도
        if retry > 0 and ("INSUFFICIENT_AVAILABLE" in error_msg or 
                          "INVALID_PARAM_VALUE" in error_msg or 
                          "Bad Request" in error_msg):
            config = SYMBOL_CONFIG[symbol]
            min_qty = config["min_qty"]
            step = config["qty_step"]
            reduced_qty = int((qty * 0.6) // step * step)
            if reduced_qty >= min_qty:
                log_debug(f"🔄 주문 재시도 ({symbol})", f"수량 감소: {qty} → {reduced_qty}")
                time.sleep(1)
                return place_order(symbol, side, reduced_qty, reduce_only, retry-1)
        return False

# ... (price_listener, start_price_listener, webhook 등 기존 코드 동일)

# 아래는 기존과 동일하게 유지
# - price_listener
# - start_price_listener
# - webhook
# - /ping, /status 라우트
# - __main__ 블록
