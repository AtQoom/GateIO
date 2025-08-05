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

from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from gate_api import exceptions as gate_api_exceptions

import asyncio
import websockets
from flask import Flask, request, jsonify

# 1. 설정 및 상수

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")

SETTLE_CURRENCY = "usdt"
KST = pytz.timezone('Asia/Seoul')

COOLDOWN_SECONDS = 14
SL_RESCUE_PROXIMITY = Decimal("0.0001")
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

TP_BASE_MAP = [Decimal("0.005"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
SL_BASE_MAP = [Decimal("0.04"), Decimal("0.038"), Decimal("0.035"), Decimal("0.033"), Decimal("0.03")]
MIN_ENTRY_FOR_SL = 6  # 예: 3회 추가진입 후부터 SL 청산 허용

# 2. 강화된 로깅 설정

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] [%(levelname)s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("AutoTrader")

def log_debug(tag, message):
    logger.info(f"[{tag}] {message}")

# 3. API 클라이언트 및 상태 변수

config = Configuration(key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
futures_api = FuturesApi(api_client)

position_state = {}
position_lock = threading.RLock()

tpsl_storage = {}
tpsl_lock = threading.RLock()

recent_signals = {}
signal_lock = threading.RLock()

task_queue = queue.Queue(maxsize=100)

# 4. 유틸리티 및 API 호출 재시도

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

# 5. 포지션 및 가격 조회

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
                    "time_multiplier": current_pos.get("time_multiplier", Decimal("1.0"))
                }
                log_debug("POSITION_UPDATED", f"{symbol}: {side} {size_abs} @ {entry_price}")
        else:
            position_state.pop(symbol, None)
    return position_state.get(symbol, None)

def get_current_price(symbol: str) -> Decimal:
    tickers = call_api_with_retry(futures_api.list_futures_tickers, settle=SETTLE_CURRENCY, contract=symbol)
    if tickers and isinstance(tickers, list) and len(tickers) > 0:
        price_str = getattr(tickers[0], "last", None)
        price = safe_decimal(price_str, Decimal("0"))
        return price
    return Decimal("0")

def get_account_equity():
    try:
        acc_info = futures_api.list_futures_accounts(SETTLE_CURRENCY)
        if hasattr(acc_info, 'available'):
            equity = Decimal(str(acc_info.available))
            return equity
        if isinstance(acc_info, list) and hasattr(acc_info[0], 'available'):
            equity = Decimal(str(acc_info[0].available))
            return equity
    except Exception as e:
        log_debug("BALANCE_ERROR", f"잔고 조회 오류: {e}")
    return Decimal("0")

# 6. TP/SL 저장 및 조회

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
    cfg = SYMBOL_CONFIG.get(symbol, {})
    tp_mult = cfg.get("tp_mult", Decimal("1.0"))
    sl_mult = cfg.get("sl_mult", Decimal("1.0"))
    return Decimal("0.006") * tp_mult, Decimal("0.04") * sl_mult, time.time()

# 7. 중복 신호 방지 쿨다운

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
        
        log_debug("SIGNAL_ACCEPTED", f"신호 수용: {key_symbol_side}")

        prune_keys = [k for k, t in recent_signals.items() if now - t > 300]
        for k in prune_keys:
            recent_signals.pop(k, None)

        return False

# 8. 주문 수량 계산 (수정된 진입 비율 적용)

def calculate_qty(symbol: str, signal_type: str, entry_multiplier: Decimal) -> Decimal:
    log_debug("QTY_CALC_START", f"{symbol} 수량 계산 시작 (signal_type: {signal_type})")
    
    cfg = SYMBOL_CONFIG[symbol]
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

    # 수정된 진입 비율: 10-20-50-120-400
    entry_ratios = [Decimal("10"), Decimal("20"), Decimal("50"), Decimal("120"), Decimal("400")]

    base_ratio = entry_ratios[current_entry_count]
    if signal_type == "sl_rescue":
        base_ratio *= Decimal("1.5")
        log_debug("QTY_CALC_SL_RESCUE", f"SL-Rescue qty multiplier applied: {float(base_ratio)}%")

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
        log_debug("QTY_CALC_MIN_NOTIONAL", f"Adjusted qty for min notional: {qty}")

    log_debug("QTY_CALC_RESULT", f"{symbol}: Qty={qty}, Notional={notional:.2f}, Equity={equity:.2f}, Ratio={base_ratio}%")
    return qty

# 9. SL-Rescue 조건 확인

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
        entry_count = pos.get("entry_count", 1)

        tp_orig, sl_orig, entry_start_time = get_tp_sl(symbol, entry_count)
        sl_mult = SYMBOL_CONFIG.get(symbol, {}).get("sl_mult", Decimal("1.0"))
        sl_pct = SL_BASE_MAP[min(entry_count-1, len(SL_BASE_MAP)-1)] * sl_mult

        if side == "buy":
            sl_price = entry_price * (1 - sl_pct)
            return current_price <= sl_price
        else:
            sl_price = entry_price * (1 + sl_pct)
            return current_price >= sl_price

# 10. 주문 실행

def place_order(symbol: str, side: str, qty: Decimal, entry_num: int, time_multiplier: Decimal) -> bool:
    log_debug("ORDER_ATTEMPT", f"{symbol} 주문 시도: {side} {qty} (진입#{entry_num})")
    
    with position_lock:
        cfg = SYMBOL_CONFIG[symbol]
        qty_adj = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN)
        if qty_adj < cfg["min_qty"]:
            log_debug("ORDER_SKIPPED", f"{symbol}: Qty {qty_adj} below min_qty {cfg['min_qty']}")
            return False
        
        price = get_current_price(symbol)
        if price <= 0:
            log_debug("ORDER_SKIPPED", f"{symbol}: Invalid price {price}")
            return False

        order_size = float(qty_adj) if side == "buy" else -float(qty_adj)

        max_allowed_value = get_account_equity() * Decimal("10")
        order_value = qty_adj * price * cfg["contract_size"]
        if order_value > max_allowed_value:
            log_debug("ORDER_SKIPPED", f"{symbol}: 주문 명목가 {order_value:.2f} USDT가 최대허용값 초과")
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
            keys_rm = [k for k in recent_signals.keys() if k.startswith(symbol + "_") or (k == symbol)]
            for k in keys_rm:
                recent_signals.pop(k)
        time.sleep(1)
        update_position(symbol)
        return True

# 11. TP/SL 모니터링 (WebSocket)

async def price_monitor(symbols):
    ws_uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    subscribe_message = {
        "time": int(time.time()),
        "channel": "futures.tickers",
        "event": "subscribe",
        "payload": symbols,
    }
    while True:
        try:
            async with websockets.connect(ws_uri) as ws:
                await ws.send(json.dumps(subscribe_message))
                log_debug("WS", "가격 모니터링 시작")
                while True:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    data = json.loads(raw_msg)
                    if data.get("event") in ("error", "subscribe"):
                        continue
                    result = data.get("result", None)
                    if isinstance(result, list):
                        for ticker in result:
                            process_ticker(ticker)
                    elif isinstance(result, dict):
                        process_ticker(result)
        except Exception as e:
            log_debug("WS_ERROR", f"WebSocket 오류: {str(e)} 재접속 시도...")
            await asyncio.sleep(5)

def process_ticker(ticker: dict):
    symbol = ticker.get("contract", "")
    if symbol not in SYMBOL_CONFIG:
        return
    price = safe_decimal(ticker.get("last", "0"))
    if price <= 0:
        return
    with position_lock:
        pos = position_state.get(symbol)
        if not pos or not pos.get("side") or not pos.get("price") or pos.get("size", 0) == 0:
            return
        entry_price = pos["price"]
        side = pos["side"]
        entry_count = pos.get("entry_count", 0)
        if entry_count == 0:
            return

        # TP/SL 초기값과 진입시각 불러오기
        tp_orig, sl_orig, entry_time_stored = get_tp_sl(symbol, entry_count)

        elapsed_seconds = time.time() - entry_time_stored
        # 15초 간격 감쇠 횟수 산정
        decay_steps = int(elapsed_seconds // 15)

        # TP 감쇠량: 0.00002 (0.002%) per 15 seconds, 곱하기 코인별 가중치
        tp_decay_per_15s = Decimal("0.00002")
        tp_min = Decimal("0.0012")   # 최소 TP 0.12%
        tp_mult = SYMBOL_CONFIG[symbol].get("tp_mult", Decimal("1.0"))
        # 감쇠 적용 후 TP값
        tp_adj = max(tp_min, tp_orig - decay_steps * tp_decay_per_15s * tp_mult)

        # SL 감쇠량: 0.00004 (0.004%) per 15 seconds, 곱하기 코인별 가중치
        sl_decay_per_15s = Decimal("0.00004")
        sl_min = Decimal("0.0009")
        sl_mult = SYMBOL_CONFIG[symbol].get("sl_mult", Decimal("1.0"))
        sl_adj = max(sl_min, sl_orig - decay_steps * sl_decay_per_15s * sl_mult)

        # TP 및 SL 목표가격 계산 (롱/숏 구분)
        tp_price = entry_price * (1 + tp_adj) if side == "buy" else entry_price * (1 - tp_adj)
        sl_price = entry_price * (1 - sl_adj) if side == "buy" else entry_price * (1 + sl_adj)

        # --- TP 부분 버퍼 추가 시작 ---
        if (side == "buy" and price >= tp_price) or (side == "sell" and price <= tp_price):
            log_debug("TP_TRIGGER", f"{symbol} TP 발동 현재가={price}, TP가격={tp_price}")
            # TP 발동 순간부터 최대 0.4초 내(실시간)로 가격 변동성 다시 체크
            buffer_time = 0.4  # 단위: 초, 필요시 0.2~1.0 사이에서 조정
            tp_valid = True
            start_time = time.time()
            while time.time() - start_time < buffer_time:
                real_time_price = get_current_price(symbol)
                if (side == "buy" and real_time_price < tp_price) or (side == "sell" and real_time_price > tp_price):
                    tp_valid = False  # 가격이 TP 미만으로 떨어질 경우 TP 청산 취소
                    log_debug("TP_BUFFER_FAIL", f"{symbol} TP 구간 이탈: {real_time_price} / {tp_price}")
                    break
                time.sleep(0.05)  # 50ms 단위로 재확인
            if tp_valid:
                close_position(symbol, reason="TP")
            else:
                log_debug("TP_HOLD", f"{symbol} TP 조건 단기 미충족! 청산 미진행")
        # --- TP 부분 버퍼 추가 끝 ---

        # 아래 기존 SL 조건 등 기존대로 유지
        if (side == "buy" and price <= sl_price) and entry_count >= MIN_ENTRY_FOR_SL:
            log_debug("SL_TRIGGER", f"{symbol} SL 발동 현재가={price}, SL가격={sl_price}, 진입횟수={entry_count} 조건 만족")
            close_position(symbol, reason="SL")
        elif (side == "sell" and price >= sl_price) and entry_count >= MIN_ENTRY_FOR_SL:
            log_debug("SL_TRIGGER", f"{symbol} SL 발동 현재가={price}, SL가격={sl_price}, 진입횟수={entry_count} 조건 만족")
            close_position(symbol, reason="SL")

# 12. 워커 스레드 및 진입 처리

def worker_thread(worker_id: int):
    log_debug(f"Worker-{worker_id}", "워커 시작")
    while True:
        try:
            data = task_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            handle_entry(data)
        except Exception as e:
            log_debug(f"Worker-{worker_id} ERROR", f"진입 처리 실패: {e}")
        finally:
            task_queue.task_done()

def handle_entry(data: dict):
    symbol_raw = data.get("symbol", "")
    side_raw = data.get("side", "").lower()
    signal_type = data.get("signal", "none").lower()
    entry_type = data.get("type", "").lower()

    symbol = normalize_symbol(symbol_raw)
    if symbol not in SYMBOL_CONFIG:
        log_debug("HANDLE_ENTRY", f"알 수 없는 심볼: {symbol_raw}")
        return

    log_debug("HANDLE_ENTRY", f"진입 신호 접수: {symbol} {side_raw} signal:{signal_type} type:{entry_type}")

    update_position(symbol)

    desired_side = "buy" if side_raw == "long" else "sell" if side_raw == "short" else None
    if not desired_side:
        log_debug("HANDLE_ENTRY", f"잘못된 side: {side_raw}")
        return

    with position_lock:
        pos = position_state.get(symbol, {})
        current_side = pos.get("side")
        entry_count = pos.get("entry_count", 0)
        sl_entry_count = pos.get("sl_entry_count", 0)
        time_mult = pos.get("time_multiplier", Decimal("1.0"))

    # 반대포지션 청산
    if current_side and current_side != desired_side and entry_count > 0:
        log_debug("HANDLE_ENTRY", f"{symbol}: 반대 포지션 감지. 기존 포지션 청산 시도")
        if not close_position(symbol, reason="reverse_entry"):
            log_debug("HANDLE_ENTRY", f"{symbol}: 반대 포지션 청산 실패, 진입 중단")
            return
        time.sleep(1)
        update_position(symbol)
        entry_count = 0
        time_mult = get_time_multiplier()

    if entry_count >= MAX_ENTRIES:
        log_debug("HANDLE_ENTRY", f"{symbol}: 최대 진입 도달 (#{entry_count}/{MAX_ENTRIES}), 추가 진입 거부")
        return

    is_sl_rescue = (signal_type == "sl_rescue")
    if is_sl_rescue:
        if sl_entry_count >= MAX_SL_RESCUES:
            log_debug("HANDLE_ENTRY", f"{symbol}: SL-Rescue 최대 진입 도달 (#{sl_entry_count}/{MAX_SL_RESCUES})")
            return
        if not is_sl_rescue_condition(symbol):
            log_debug("HANDLE_ENTRY", f"{symbol}: SL-Rescue 조건 미충족, 진입 건너뜀")
            return
        with position_lock:
            position_state[symbol]["sl_entry_count"] = sl_entry_count + 1
        actual_entry_num = entry_count + 1
        log_debug("HANDLE_ENTRY", f"{symbol}: SL-Rescue 진입 시도 (#{sl_entry_count + 1}/{MAX_SL_RESCUES})")
    else:
        if entry_count > 0:
            price_current = get_current_price(symbol)
            avg_price = position_state[symbol].get("price")
            if avg_price and current_side == "buy" and price_current >= avg_price:
                log_debug("HANDLE_ENTRY", f"{symbol}: 가격 조건 미충족(현재가 {price_current} >= 평단가 {avg_price})")
                return
            if avg_price and current_side == "sell" and price_current <= avg_price:
                log_debug("HANDLE_ENTRY", f"{symbol}: 가격 조건 미충족(현재가 {price_current} <= 평단가 {avg_price})")
                return
        actual_entry_num = entry_count + 1

    if actual_entry_num <= MAX_ENTRIES:
        tp_val = TP_BASE_MAP[actual_entry_num - 1] * SYMBOL_CONFIG[symbol]["tp_mult"]
        sl_val = SL_BASE_MAP[actual_entry_num - 1] * SYMBOL_CONFIG[symbol]["sl_mult"]
        store_tp_sl(symbol, tp_val, sl_val, actual_entry_num)
        log_debug("HANDLE_ENTRY", f"{symbol} 진입#{actual_entry_num} TP {tp_val*100:.3f}% / SL {sl_val*100:.3f}% 저장")
    else:
        log_debug("HANDLE_ENTRY", f"{symbol} 진입#{actual_entry_num} TP/SL 맵 범위 초과")

    qty = calculate_qty(symbol, signal_type, time_mult)

    if qty <= 0:
        log_debug("HANDLE_ENTRY", f"{symbol}: 유효 수량 계산 실패")
        return

    if place_order(symbol, desired_side, qty, actual_entry_num, time_mult):
        log_debug("HANDLE_ENTRY", f"{symbol} {desired_side.upper()} 진입 성공, 수량: {qty}")
    else:
        log_debug("HANDLE_ENTRY", f"{symbol} {desired_side.upper()} 진입 실패")

def get_time_multiplier() -> Decimal:
    now_hour = datetime.now(KST).hour
    if 22 <= now_hour or now_hour < 9:
        return Decimal("1.0")
    return Decimal("1.0")

# 13. HTTP 웹훅 서버 (404 오류 해결)

app = Flask(__name__)

@app.route("/webhook", methods=["POST", "GET", "PUT", "PATCH"])
@app.route("/", methods=["POST", "GET", "PUT", "PATCH"])
def webhook_handler():
    log_debug("WEBHOOK_REQUEST", f"웹훅 요청 수신: {request.method} {request.path}")
    
    if request.method == "GET":
        return jsonify({"status": "OK", "message": "Webhook endpoint is active"}), 200
    
    try:
        data = None
        try:
            data = request.get_json(force=True)
            log_debug("WEBHOOK_PARSE", f"JSON 파싱 성공: {data}")
        except Exception as e:
            log_debug("WEBHOOK_PARSE", f"JSON 파싱 실패: {e}")
        
        if not data and request.form:
            data = dict(request.form)
            log_debug("WEBHOOK_PARSE", f"Form 데이터 파싱: {data}")

        if not data:
            raw_data = request.get_data(as_text=True)
            log_debug("WEBHOOK_RAW", f"Raw data: {raw_data[:200]}...")
            if raw_data:
                try:
                    data = json.loads(raw_data)
                    log_debug("WEBHOOK_PARSE", f"Raw JSON 파싱 성공: {data}")
                except:
                    try:
                        import urllib.parse
                        data = dict(urllib.parse.parse_qsl(raw_data))
                        log_debug("WEBHOOK_PARSE", f"URL encoded 파싱: {data}")
                    except:
                        pass

        if not data:
            log_debug("WEBHOOK_ERROR", "데이터 파싱 실패 - 빈 데이터")
            return jsonify({"error": "Empty or invalid data"}), 400
        
        symbol_raw = data.get("symbol", "")
        action = str(data.get("action", "entry")).lower()
        
        if not symbol_raw:
            log_debug("WEBHOOK_ERROR", "심볼 누락")
            return jsonify({"error": "Symbol required"}), 400
        
        symbol = normalize_symbol(symbol_raw)
        if symbol not in SYMBOL_CONFIG:
            log_debug("WEBHOOK_ERROR", f"유효하지 않은 심볼: {symbol_raw} -> {symbol}")
            return jsonify({"error": f"Invalid symbol: {symbol_raw}"}), 400
        
        log_debug("WEBHOOK_RECEIVED", f"유효한 신호: 심볼={symbol}, action={action}, side={data.get('side')}, signal={data.get('signal')}")

        # 중복 신호 체크
        if is_duplicate_signal(data):
            log_debug("WEBHOOK_DUPLICATE", "중복 신호로 무시")
            return jsonify({"status": "duplicate"}), 200

        if action == "entry":
            # 작업 큐에 진입 처리만
            try:
                task_queue.put_nowait(data)
                log_debug("WEBHOOK_QUEUED", f"작업 큐에 추가 완료 (큐 크기: {task_queue.qsize()})")
                return jsonify({"status": "queued", "queue_size": task_queue.qsize()}), 200
            except queue.Full:
                log_debug("WEBHOOK_ERROR", "작업 큐 가득참")
                return jsonify({"error": "Queue full"}), 429

        elif action == "exit":
            reason = str(data.get("reason", "")).strip().lower()
            # 1) TP/SL 청산 알림(reason 정확히 'tp' 또는 'sl')은 무조건 무시!
            if reason in ("tp", "sl"):
                log_debug("WEBHOOK_IGNORED", f"{symbol} EXIT({reason.upper()}) 알림 무시 (자동 TP/SL 청산 차단)")
                return jsonify({"status": f"{action}_{reason}_ignored"}), 200
            # 2) 나머지(수동, 전략, 기타 alert)는 정상적으로 청산 실행
            update_position(symbol)
            with position_lock:
                pos = position_state.get(symbol)
                if pos and pos.get("size", Decimal("0")) > 0:
                    close_position(symbol, reason=action.upper())
                    log_debug("FORCE_CLOSE", f"{symbol} {action.upper()}({reason}) 알림으로 포지션 청산")
                    return jsonify({"status": f"{action}_closed"}), 200
                else:
                    log_debug("NO_POSITION_EXIT", f"{symbol} {action.upper()}({reason}) 알림 - 포지션 없음")
                    return jsonify({"status": "no_position"}), 200

        elif action in ("tp", "sl"):
           # TP/SL 청산 단독 알림(아예 action이 'tp','sl'인 경우도 필요시 방어)
            log_debug("WEBHOOK_IGNORED", f"{symbol} {action.upper()} 알림 무시 (TP/SL 자동청산 룰)")
            return jsonify({"status": f"{action}_ignored"}), 200

        else:
            log_debug("WEBHOOK_ERROR", f"알 수 없는 action: {action}")
            return jsonify({"error": f"Unknown action: {action}"}), 400

    except Exception as e:
        log_debug("WEBHOOK_ERROR", f"웹훅 처리 중 예외: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_equity()
        positions = {}
        for sym in SYMBOL_CONFIG:
            pos = position_state.get(sym, {})
            if pos.get("side"):
                positions[sym] = {
                    "side": pos["side"],
                    "size": float(pos.get("size", 0)),
                    "price": float(pos.get("price", 0)),
                    "entry_count": pos.get("entry_count", 0),
                    "sl_entry_count": pos.get("sl_entry_count", 0)
                }
        
        return jsonify({
            "status": "running",
            "current_time": datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST'),
            "balance_usdt": float(equity),
            "active_positions": positions,
            "queue_size": task_queue.qsize(),
            "entry_ratios": [20, 30, 70, 160, 500]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 14. 초기 상태 출력 및 실행

def log_initial_state():
    equity = get_account_equity()
    log_debug("INIT", f"초기 자산 조회: {equity:.2f} USDT")
    active_positions = []
    for sym in SYMBOL_CONFIG:
        update_position(sym)
        pos = position_state.get(sym, {})
        if pos and pos.get("side") and pos.get("size", 0) > 0:
            active_positions.append(f"{sym} 포지션: {pos['side']} {pos['size']} @ {pos['price']} (진입#{pos.get('entry_count', 0)}/5, SL-Rescue#{pos.get('sl_entry_count', 0)}/3)")
    if active_positions:
        log_debug("INIT", "현재 활성화된 포지션:")
        for info in active_positions:
            log_debug("INIT", f"  - {info}")
    else:
        log_debug("INIT", "활성 포지션 없음")

def run_ws_monitor():
    asyncio.run(price_monitor(list(SYMBOL_CONFIG.keys())))

def worker_launcher(num_workers: int = 8):
    for i in range(num_workers):
        threading.Thread(target=worker_thread, args=(i,), daemon=True, name=f"Worker-{i}").start()
    log_debug("WORKER", f"{num_workers} 워커 스레드 실행")

def main():
    log_debug("STARTUP", "자동매매 서버 시작")
    log_debug("ENTRY_RATIOS", "진입 비율: 10%-20%-50%-120%-100%")
    log_initial_state()
    worker_launcher(4)
    threading.Thread(target=run_ws_monitor, daemon=True, name="WS-Monitor").start()
    port = int(os.environ.get("PORT", 8080))
    log_debug("SERVER", f"HTTP 서버 시작 포트 {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
