#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import threading
import queue
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
import pytz

from flask import Flask, request, jsonify

# 게이트아이오 API 임포트 (실제 모듈명 및 import 경로는 맞춰주세요)
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from gate_api import exceptions as gate_api_exceptions

# ------------------------------------------------------
# 1. 상수 및 환경설정
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")

SETTLE_CURRENCY = "usdt"
KST = pytz.timezone('Asia/Seoul')

COOLDOWN_SECONDS = 14
MAX_ENTRIES = 5
MAX_SL_RESCUES = 3

SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "BTCUSDT.P": "BTC_USDT", "BTCUSDTPERP": "BTC_USDT",
    "BTC_USDT": "BTC_USDT", "BTC": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", "ETHUSDT.P": "ETH_USDT", "ETHUSDTPERP": "ETH_USDT",
    "ETH_USDT": "ETH_USDT", "ETH": "ETH_USDT",
    "SOLUSDT": "SOL_USDT", "SOLUSDT.P": "SOL_USDT", "SOLUSDTPERP": "SOL_USDT",
    "SOL_USDT": "SOL_USDT", "SOL": "SOL_USDT",
    "ADAUSDT": "ADA_USDT", "ADAUSDT.P": "ADA_USDT", "AD⁴AUSDTPERP": "ADA_USDT",
    "ADA_USDT": "ADA_USDT", "ADA": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "SUIUSDT.P": "SUI_USDT", "SUIUSDTPERP": "SUI_USDT",
    "SUI_USDT": "SUI_USDT", "SUI": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "LINKUSDT.P": "LINK_USDT", "LINKUSDTPERP": "LINK_USDT",
    "LINK_USDT": "LINK_USDT", "LINK": "LINK_USDT",
    "PEPEUSDT": "PEPE_USDT", "PEPEUSDT.P": "PEPE_USDT", "PEPEUSDTPERP": "PEPE_USDT",
    "PEPE_USDT": "PEPE_USDT", "PEPE": "PEPE_USDT",
    "XRPUSDT": "XRP_USDT", "XRPUSDT.P": "XRP_USDT", "XRPUSDTPERP": "XRP_USDT",
    "XRP_USDT": "XRP_USDT", "XRP": "XRP_USDT",
    "DOGEUSDT": "DOGE_USDT", "DOGEUSDT.P": "DOGE_USDT", "DOGEUSDTPERP": "DOGE_USDT",
    "DOGE_USDT": "DOGE_USDT", "DOGE": "DOGE_USDT",
    "ONDOUSDT": "ONDO_USDT", "ONDOUSDT.P": "ONDO_USDT", "ONDOUSDTPERP": "ONDO_USDT",
    "ONDO_USDT": "ONDO_USDT", "ONDO": "ONDO_USDT",
}

SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("0.55"), "sl_mult": Decimal("0.55"),
                 "qty_mult": Decimal("1.5")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("0.65"), "sl_mult": Decimal("0.65"),
                 "qty_mult": Decimal("1.3")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("0.85"), "sl_mult": Decimal("0.85"),
                 "qty_mult": Decimal("1.2")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"),
                 "qty_mult": Decimal("1.0")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"),
                 "qty_mult": Decimal("1.0")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"),
                  "qty_mult": Decimal("1.0")},
    "PEPE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000000"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.2"), "sl_mult": Decimal("1.2"),
                  "qty_mult": Decimal("1.0")},
    "XRP_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"),
                 "qty_mult": Decimal("1.0")},
    "DOGE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.2"), "sl_mult": Decimal("1.2"),
                  "qty_mult": Decimal("1.0")},
    "ONDO_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"),
                  "qty_mult": Decimal("1.0")},
}

# TP, SL 기본 맵 (진입별 %값)
TP_BASE_MAP = [Decimal("0.005"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
SL_BASE_MAP = [Decimal("0.04"), Decimal("0.038"), Decimal("0.035"), Decimal("0.033"), Decimal("0.03")]

MIN_ENTRY_FOR_SL = 6  # SL 허용 최소 진입 횟수 (예: 6)

# 진입 비중 (백분율) - 예: [18, 30, 72, 180, 550] 실제 전략에 맞게 조정 가능
ENTRY_RATIOS = [Decimal("18"), Decimal("30"), Decimal("72"), Decimal("180"), Decimal("550")]

# ------------------------------------------------------
# 2. 로깅 설정

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] [%(levelname)s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("AutoTrader")

def log_debug(tag, message):
    logger.info(f"[{tag}] {message}")

# ------------------------------------------------------
# 3. API 클라이언트 및 상태 변수 선언

config = Configuration(key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
futures_api = FuturesApi(api_client)

position_state = {}  # 심볼별 포지션 상태 dict
position_lock = threading.RLock()

tpsl_storage = {}    # 심볼별 TP/SL 저장 dict
tpsl_lock = threading.RLock()

recent_signals = {}  # 중복 신호 관리 dict
signal_lock = threading.RLock()

task_queue = queue.Queue(maxsize=100)

# ------------------------------------------------------
# 4. 유틸리티 함수

def normalize_symbol(raw_symbol: str) -> str:
    result = SYMBOL_MAPPING.get(raw_symbol.strip().upper(), raw_symbol.strip().upper())
    log_debug("SYMBOL_NORMALIZE", f"'{raw_symbol}' -> '{result}'")
    return result

def safe_decimal(value, default=Decimal("0")):
    try:
        return Decimal(str(value))
    except Exception as e:
        log_debug("DECIMAL_ERROR", f"Failed to convert '{value}': {e}")
        return default

def call_api_with_retry(api_func, *args, retries=3, delay=2, **kwargs):
    for i in range(retries):
        try:
            result = api_func(*args, **kwargs)
            return result
        except gate_api_exceptions.ApiException as e:
            log_debug("API_ERROR", f"ApiException: status={e.status}, reason={e.reason}")
        except Exception as e:
            log_debug("API_ERROR", f"Exception: {e}")
        if i < retries - 1:
            time.sleep(delay)
    return None

# ------------------------------------------------------
# 5. 포지션 업데이트

def update_position(symbol: str):
    log_debug("UPDATE_POSITION", f"{symbol} 포지션 업데이트 시작")
    with position_lock:
        pos_info = call_api_with_retry(futures_api.get_position, SETTLE_CURRENCY, symbol)
        if pos_info and pos_info.size:
            size = safe_decimal(pos_info.size)
            if size == 0:
                position_state.pop(symbol, None)
            else:
                entry_price = safe_decimal(pos_info.entry_price)
                side = "buy" if size > 0 else "sell"
                size_abs = abs(size)
                current_pos = position_state.get(symbol, {})
                position_state[symbol] = {
                    "price": entry_price,
                    "side": side,
                    "size": size_abs,
                    "entry_count": current_pos.get("entry_count", 0),
                    "sl_entry_count": current_pos.get("sl_entry_count", 0),
                    "entry_time": current_pos.get("entry_time", time.time()),
                    "time_multiplier": current_pos.get("time_multiplier", Decimal("1.0")),
                    "is_entering": current_pos.get("is_entering", False),
                }
                log_debug("POSITION_UPDATED", f"{symbol}: {side} {size_abs} @ {entry_price}")
        else:
            position_state.pop(symbol, None)
    return position_state.get(symbol, None)

# ------------------------------------------------------
# 6. 현재 가격 조회

def get_current_price(symbol: str) -> Decimal:
    tickers = call_api_with_retry(futures_api.list_futures_tickers, settle=SETTLE_CURRENCY, contract=symbol)
    if tickers and isinstance(tickers, list) and len(tickers) > 0:
        price_str = getattr(tickers[0], "last", None)
        price = safe_decimal(price_str, Decimal("0"))
        return price
    return Decimal("0")

# ------------------------------------------------------
# 7. 잔고 조회

def get_account_equity():
    try:
        acc_info = futures_api.list_futures_accounts(SETTLE_CURRENCY)
        if hasattr(acc_info, 'available'):
            return Decimal(str(acc_info.available))
        if isinstance(acc_info, list) and hasattr(acc_info[0], 'available'):
            return Decimal(str(acc_info[0].available))
    except Exception as e:
        log_debug("BALANCE_ERROR", f"잔고 조회 오류: {e}")
    return Decimal("0")

# ------------------------------------------------------
# 8. TP/SL 저장 및 조회

def store_tp_sl(symbol: str, tp: Decimal, sl: Decimal, entry_num: int):
    with tpsl_lock:
        if symbol not in tpsl_storage:
            tpsl_storage[symbol] = {}
        tpsl_storage[symbol][entry_num] = {
            "tp": tp,
            "sl": sl,
            "entry_time": time.time()
        }

def get_tp_sl(symbol: str, entry_num: int):
    with tpsl_lock:
        symbol_dict = tpsl_storage.get(symbol, {})
        if entry_num in symbol_dict:
            v = symbol_dict[entry_num]
            return v["tp"], v["sl"], v["entry_time"]
    # 기본값 리턴
    cfg = SYMBOL_CONFIG.get(symbol, {})
    tp_mult = cfg.get("tp_mult", Decimal("1.0"))
    sl_mult = cfg.get("sl_mult", Decimal("1.0"))
    return Decimal("0.006") * tp_mult, Decimal("0.04") * sl_mult, time.time()

# ------------------------------------------------------
# 9. 중복 신호 쿨다운 체크

def is_duplicate_signal(data: dict) -> bool:
    with signal_lock:
        now = time.time()
        symbol = normalize_symbol(data.get("symbol", ""))
        side = data.get("side", "").lower()
        signal_id = data.get("id", None)
        key_symbol_side = f"{symbol}_{side}"

        log_debug("COOLDOWN_CHECK", f"검증 중: symbol={symbol}, side={side}, id={signal_id}")

        if signal_id and signal_id in recent_signals:
            last_time = recent_signals[signal_id]
            remaining = COOLDOWN_SECONDS - (now - last_time)
            if remaining > 0:
                log_debug("DUPLICATE_SIGNAL", f"Signal id {signal_id} 쿨다운 중 (남은시간: {remaining:.1f}초)")
                return True

        if key_symbol_side in recent_signals:
            last_time = recent_signals[key_symbol_side]
            remaining = COOLDOWN_SECONDS - (now - last_time)
            if remaining > 0:
                log_debug("DUPLICATE_SIGNAL", f"Symbol-side {key_symbol_side} 쿨다운 중 (남은시간: {remaining:.1f}초)")
                return True

        if signal_id:
            recent_signals[signal_id] = now
        recent_signals[key_symbol_side] = now

        # 오래된 기록 정리 (5분)
        prune_keys = [k for k, t in recent_signals.items() if now - t > 300]
        for k in prune_keys:
            recent_signals.pop(k, None)

        log_debug("SIGNAL_ACCEPTED", f"신호 수용: {key_symbol_side}")
        return False

# ------------------------------------------------------
# 10. 주문 수량 계산

def calculate_qty(symbol: str, signal_type: str, entry_multiplier: Decimal) -> Decimal:
    log_debug("QTY_CALC_START", f"{symbol} 수량 계산 시작 (signal_type: {signal_type})")

    cfg = SYMBOL_CONFIG.get(symbol)
    if not cfg:
        log_debug("QTY_CALC_FAIL", f"{symbol} 설정 없음")
        return Decimal("0")

    qty_mult = cfg.get("qty_mult", Decimal("1.0"))
    equity = get_account_equity()
    price = get_current_price(symbol)

    if equity <= 0 or price <= 0:
        log_debug("QTY_CALC_FAIL", f"Invalid equity({equity}) or price({price}) for {symbol}")
        return Decimal("0")

    current_entry_count = position_state.get(symbol, {}).get("entry_count", 0)

    if current_entry_count >= MAX_ENTRIES:
        log_debug("QTY_CALC_LIMIT", f"Max entries reached for {symbol} - {current_entry_count}/{MAX_ENTRIES}")
        return Decimal("0")

    base_ratio = ENTRY_RATIOS[current_entry_count]
    if signal_type == "sl_rescue":
        base_ratio *= Decimal("1.5")
        log_debug("QTY_CALC_SL_RESCUE", f"SL-Rescue qty multiplier 적용: {float(base_ratio)}%")

    position_value = equity * (base_ratio / Decimal("100")) * entry_multiplier * qty_mult

    contract_size = cfg["contract_size"]
    qty_step = cfg["qty_step"]

    qty_raw = (position_value / (price * contract_size)) / qty_step
    qty_floor = qty_raw.quantize(Decimal('1'), rounding=ROUND_DOWN)
    qty = qty_floor * qty_step

    qty = max(qty, cfg["min_qty"])

    notional = qty * price * contract_size
    if notional < cfg["min_notional"]:
        min_qty = (cfg["min_notional"] / (price * contract_size)) / qty_step
        min_qty_floor = min_qty.quantize(Decimal('1'), rounding=ROUND_DOWN)
        qty = max(qty, min_qty_floor * qty_step)
        log_debug("QTY_CALC_MIN_NOTIONAL", f"최소 명목가 보정 qty: {qty}")

    log_debug("QTY_CALC_RESULT", f"{symbol}: Qty={qty}, Notional={notional:.2f}, Equity={equity:.2f}, Ratio={base_ratio}%")
    return qty

# ------------------------------------------------------
# 11. SL-Rescue 조건 확인 (필요시 사용)

def is_sl_rescue_condition(symbol: str) -> bool:
    with position_lock:
        pos = position_state.get(symbol)
        if not pos or pos.get("size", Decimal("0")) == 0 or pos.get("sl_entry_count", 0) >= MAX_SL_RESCUES:
            return False

        current_price = get_current_price(symbol)
        if current_price <= 0:
            return False

        entry_price = pos.get("price")
        side = pos.get("side")
        entry_count = pos.get("entry_count", 0)
        if not entry_price or side not in ("buy", "sell") or entry_count == 0:
            return False

        if side == "buy":
            sl_pct = SL_BASE_MAP[min(entry_count-1, len(SL_BASE_MAP)-1)] * SYMBOL_CONFIG[symbol]["sl_mult"]
            sl_price = entry_price * (1 - sl_pct)
            return current_price <= sl_price
        else:
            sl_pct = SL_BASE_MAP[min(entry_count-1, len(SL_BASE_MAP)-1)] * SYMBOL_CONFIG[symbol]["sl_mult"]
            sl_price = entry_price * (1 + sl_pct)
            return current_price >= sl_price

# ------------------------------------------------------
# 12. 주문 실행

def place_order(symbol: str, side: str, qty: Decimal, entry_num: int, time_multiplier: Decimal) -> bool:
    log_debug("ORDER_ATTEMPT", f"{symbol} 주문 시도: {side} {qty} (진입#{entry_num})")

    with position_lock:
        cfg = SYMBOL_CONFIG.get(symbol)
        if not cfg:
            log_debug("ORDER_FAIL", f"{symbol}: 설정 없음")
            return False

        qty_adj = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN)
        if qty_adj < cfg["min_qty"]:
            log_debug("ORDER_SKIPPED", f"{symbol}: Qty {qty_adj} < min_qty {cfg['min_qty']}")
            return False

        price = get_current_price(symbol)
        if price <= 0:
            log_debug("ORDER_SKIPPED", f"{symbol}: Invalid price {price}")
            return False

        order_size = float(qty_adj) if side == "buy" else -float(qty_adj)

        max_allowed_value = get_account_equity() * Decimal("10")
        order_value = qty_adj * price * cfg["contract_size"]
        if order_value > max_allowed_value:
            log_debug("ORDER_SKIPPED", f"{symbol}: 주문 명목가 {order_value:.2f} 초과")
            return False

        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price="0",
            tif="ioc",
            reduce_only=False,
        )

        result = call_api_with_retry(futures_api.create_futures_order, SETTLE_CURRENCY, order)
        if not result:
            log_debug("ORDER_FAIL", f"{symbol}: 주문 실패")
            return False

        pos = position_state.setdefault(symbol, {})
        pos["entry_count"] = entry_num
        pos["entry_time"] = time.time()
        pos["time_multiplier"] = time_multiplier
        if "sl_entry_count" not in pos:
            pos["sl_entry_count"] = 0
        log_debug("ORDER_SUCCESS", f"{symbol} {side} {qty_adj} 계약 (진입#{entry_num}/5)")

        time.sleep(2)
        update_position(symbol)

        return True

# ------------------------------------------------------
# 13. 포지션 청산

def close_position(symbol: str, reason: str = "manual") -> bool:
    with position_lock:
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = call_api_with_retry(futures_api.create_futures_order, SETTLE_CURRENCY, order)
        if not result:
            log_debug("CLOSE_FAIL", f"{symbol}: 청산 실패")
            return False

        log_debug("CLOSE_SUCCESS", f"{symbol}: 청산 완료 (이유: {reason})")

        position_state.pop(symbol, None)
        with tpsl_lock:
            tpsl_storage.pop(symbol, None)
        with signal_lock:
            keys_rm = [k for k in recent_signals if k.startswith(symbol + "_") or k == symbol]
            for k in keys_rm:
                recent_signals.pop(k)
        time.sleep(1)
        update_position(symbol)
        return True

# ------------------------------------------------------
# 14. 웹훅 신호 처리 함수

def process_entry_signal(data):
    symbol_raw = data.get("symbol", "")
    symbol = normalize_symbol(symbol_raw)
    side = data.get("side", "").lower()
    entry_num = int(data.get("entry_num", 0))
    tp_pct = Decimal(str(data.get("tp_pct", "0")))
    sl_pct = Decimal(str(data.get("sl_pct", "0")))
    signal_type = data.get("type", "").lower()
    entry_multiplier = Decimal("1.0")

    if is_duplicate_signal(data):
        logger.info(f"[WEBHOOK_DUPLICATE] 중복 신호로 무시: {symbol} {side}")
        return False

    qty = calculate_qty(symbol, signal_type, entry_multiplier)
    if qty <= 0:
        logger.info(f"[ORDER_SKIPPED] {symbol}: 수량 0 이하 또는 제한 초과")
        return False
    
    success = place_order(symbol, side, qty, entry_num, entry_multiplier)
    if success:
        store_tp_sl(symbol, tp_pct, sl_pct, entry_num)
    return success

def process_exit_signal(data):
    symbol_raw = data.get("symbol", "")
    symbol = normalize_symbol(symbol_raw)
    side = data.get("side", "").lower()
    reason = data.get("reason", "manual")

    # 필요한 경우 중복 체크도 가능

    return close_position(symbol, reason)

# ------------------------------------------------------
# 15. Flask 웹서버 및 웹훅 핸들러

app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data received"}), 400
        
        action = data.get("action", "").lower()
        
        if action == "entry":
            result = process_entry_signal(data)
            if result:
                return jsonify({"status": "success", "message": "Entry order placed"}), 200
            else:
                return jsonify({"status": "fail", "message": "Entry order failed or skipped"}), 200

        elif action == "exit":
            result = process_exit_signal(data)
            if result:
                return jsonify({"status": "success", "message": "Exit order placed"}), 200
            else:
                return jsonify({"status": "fail", "message": "Exit order failed or skipped"}), 200

        else:
            return jsonify({"status": "fail", "message": "Unknown action"}), 400

    except Exception as e:
        logger.exception(f"Webhook 처리 중 오류: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
