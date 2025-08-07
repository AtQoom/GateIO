#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import threading
import queue
import logging
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
import pytz

import asyncio
import websockets
import pandas as pd
import numpy as np
import ta  # pip install ta

from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from gate_api import exceptions as gate_api_exceptions
from flask import Flask, request, jsonify

getcontext().prec = 12  # Decimal 연산 정밀도

# --- 환경 및 상수 설정 ---
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE_CURRENCY = "usdt"

KST = pytz.timezone('Asia/Seoul')

COOLDOWN_SECONDS = 14
MAX_ENTRIES = 5
MAX_SL_RESCUES = 3
MIN_ENTRY_FOR_SL = 6

SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "BTCUSDT.P": "BTC_USDT", "BTCUSDTPERP": "BTC_USDT",
    "BTC_USDT": "BTC_USDT", "BTC": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", "ETHUSDT.P": "ETH_USDT", "ETHUSDTPERP": "ETH_USDT",
    "ETH_USDT": "ETH_USDT", "ETH": "ETH_USDT",
    "SOLUSDT": "SOL_USDT", "SOLUSDT.P": "SOL_USDT", "SOLUSDTPERP": "SOL_USDT",
    "SOL_USDT": "SOL_USDT", "SOL": "SOL_USDT",
    "ADAUSDT": "ADA_USDT", "ADAUSDT.P": "ADA_USDT", "ADAUSDTPERP": "ADA_USDT",
    "ADA_USDT": "ADA_USDT", "ADA": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "SUIUSDT.P": "SUI_USDT", "SUIUSDTPERP": "SUI_USDT",
    "SUI_USDT": "SUI_USDT", "SUI": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "LINKUSDT.P": "LINK_USDT", "LINKUSDTPERP": "LINK_USDT",
    "LINK_USDT": "LINK_USDT", "LINK": "LINK_USDT",
}

SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("0.55"), "sl_mult": Decimal("0.55"), "qty_mult": Decimal("1.5")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("0.65"), "sl_mult": Decimal("0.65"), "qty_mult": Decimal("1.3")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("0.85"), "sl_mult": Decimal("0.85"), "qty_mult": Decimal("1.2")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"), "qty_mult": Decimal("1.0")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"), "qty_mult": Decimal("1.0")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"), "qty_mult": Decimal("1.0")},
}

TP_BASE_MAP = [Decimal("0.005"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
SL_BASE_MAP = [Decimal("0.04"), Decimal("0.038"), Decimal("0.035"), Decimal("0.033"), Decimal("0.03")]

# --- 로깅 설정 ---

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] [%(levelname)s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

logger = logging.getLogger("AutoTrader")

def log_debug(tag, message):
    logger.info(f"[{tag}] {message}")

# --- API 클라이언트 초기화 ---

config = Configuration(key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
futures_api = FuturesApi(api_client)

# --- 상태 변수, 잠금, 작업 큐 ---
position_state = {}
position_lock = threading.RLock()

tpsl_storage = {}
tpsl_lock = threading.RLock()

recent_signals = {}
signal_lock = threading.RLock()

task_queue = queue.Queue(maxsize=100)

app = Flask(__name__)

# --- 심볼별 가격 데이터 및 지표 관리용 클래스 ---

class SymbolData:
    def __init__(self, symbol):
        self.symbol = symbol
        self.lock = threading.RLock()
        self.df = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        self.df.set_index('timestamp', inplace=True)
        self.last_bar_time = None

    def add_tick(self, price, volume, tstamp=None):
        with self.lock:
            timestamp = int(tstamp or time.time())
            bar_time = timestamp - (timestamp % 15)  # 15초 봉 기준
            if self.last_bar_time != bar_time:
                self.df.loc[bar_time] = [price, price, price, price, volume]
                self.last_bar_time = bar_time
            else:
                row = self.df.loc[bar_time]
                row['high'] = max(row['high'], price)
                row['low'] = min(row['low'], price)
                row['close'] = price
                row['volume'] += volume
                self.df.loc[bar_time] = row

            if len(self.df) > 100:
                self.df = self.df.iloc[-100:]

    def compute_indicators(self):
        with self.lock:
            if len(self.df) < 15:
                return None
            df = self.df.copy()
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
            df['bb_high'] = bb.bollinger_hband()
            df['bb_low'] = bb.bollinger_lband()
            df['bb_mid'] = bb.bollinger_mavg()
            df['bb_pos'] = (df['close'] - df['bb_low']) / (df['bb_high'] - df['bb_low']).replace(0, np.nan)
            df['vol_sma20'] = df['volume'].rolling(window=20).mean()
            return df

symbol_data_map = {sym: SymbolData(sym) for sym in SYMBOL_CONFIG.keys()}

# --- 유틸 함수: 심볼 정규화, Decimal 변환, API 재시도 등 ---

def normalize_symbol(raw_symbol: str) -> str:
    return SYMBOL_MAPPING.get(raw_symbol.strip().upper(), raw_symbol.strip().upper())

def safe_decimal(value, default=Decimal("0")):
    try:
        return Decimal(str(value))
    except Exception as e:
        log_debug("DECIMAL_ERROR", f"Failed to convert '{value}': {e}")
        return default

def call_api_with_retry(api_func, *args, retries=3, delay=2, **kwargs):
    for attempt in range(retries):
        try:
            return api_func(*args, **kwargs)
        except gate_api_exceptions.ApiException as e:
            log_debug("API_ERROR", f"ApiException: status={e.status}, reason={e.reason}")
        except Exception as e:
            log_debug("API_ERROR", f"Exception: {e}")
        if attempt < retries-1:
            time.sleep(delay)
    return None

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

def get_current_price(symbol: str) -> Decimal:
    tickers = call_api_with_retry(futures_api.list_futures_tickers, settle=SETTLE_CURRENCY, contract=symbol)
    if tickers and isinstance(tickers, list) and len(tickers) > 0:
        price_str = getattr(tickers[0], "last", None)
        return safe_decimal(price_str, Decimal("0"))
    return Decimal("0")

# --- TP/SL 저장 및 조회 함수 ---

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

# --- 주문 수량 계산 ---

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

# --- SL-Rescue 조건 확인 ---

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
    
        _, sl_orig, _ = get_tp_sl(symbol, entry_count)
        sl_mult = SYMBOL_CONFIG.get(symbol, {}).get("sl_mult", Decimal("1.0"))
        sl_pct = SL_BASE_MAP[min(entry_count-1, len(SL_BASE_MAP)-1)] * sl_mult

        if side == "buy":
            sl_price = entry_price * (1 - sl_pct)
            return current_price <= sl_price
        else:
            sl_price = entry_price * (1 + sl_pct)
            return current_price >= sl_price

# --- 주문 실행 및 청산 함수 ---

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
        now = time.time()
        pos = position_state.get(symbol, {})
        last_close = pos.get("last_close_time", 0)

        if now - last_close < 1.0:
            log_debug("CLOSE_SKIPPED", f"{symbol}: 최근 청산({now-last_close:.2f}s 내) 중복 방지")
            return False

        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = call_api_with_retry(futures_api.create_futures_order, SETTLE_CURRENCY, order)
        if not result:
            log_debug("CLOSE_FAIL", f"{symbol}: 청산 실패")
            return False

        pos["last_close_time"] = now
        log_debug("CLOSE_SUCCESS", f"{symbol}: 청산 완료 (이유: {reason})")

        position_state.pop(symbol, None)
        with tpsl_lock:
            tpsl_storage.pop(symbol, None)
        with signal_lock:
            keys_rm = [k for k, _ in recent_signals.items() if k.startswith(symbol + "_") or k == symbol]
            for k in keys_rm:
                recent_signals.pop(k)
        time.sleep(1)
        update_position(symbol)
        return True

# --- 진입 처리 함수 (웹훅 및 내부 호출 모두 사용) ---

def handle_entry(data: dict):
    symbol_raw = data.get("symbol", "")
    side_raw = data.get("side", "").lower()
    signal_type = data.get("signal", "none").lower()
    entry_type = data.get("type", "").lower()

    symbol = normalize_symbol(symbol_raw)
    if symbol not in SYMBOL_CONFIG:
        log_debug("HANDLE_ENTRY", f"알 수 없는 심볼: {symbol_raw}")
        return

    update_position(symbol)

    with position_lock:
        pos = position_state.get(symbol, {})
        current_side = pos.get("side")
        entry_count = pos.get("entry_count", 0)
        sl_entry_count = pos.get("sl_entry_count", 0)
        time_mult = pos.get("time_multiplier", Decimal("1.0"))

    desired_side = "buy" if side_raw == "long" else "sell" if side_raw == "short" else None
    if not desired_side:
        log_debug("HANDLE_ENTRY", f"잘못된 side: {side_raw}")
        return

    if current_side and current_side != desired_side and entry_count > 0:
        log_debug("HANDLE_ENTRY", f"{symbol}: 반대 포지션 감지. 기존 포지션 청산 시도")
        if not close_position(symbol, reason="reverse_entry"):
            log_debug("HANDLE_ENTRY", f"{symbol}: 반대 포지션 청산 실패, 진입 중단")
            return
        time.sleep(1)
        update_position(symbol)
        entry_count = 0
        time_mult = Decimal("1.0")

    if entry_count >= MAX_ENTRIES:
        log_debug("HANDLE_ENTRY", f"{symbol}: 최대 진입 도달 (#{entry_count}/{MAX_ENTRIES})")
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
                log_debug("HANDLE_ENTRY", f"{symbol}: 가격 조건 미충족 (현재가 {price_current} >= 평단가 {avg_price})")
                return
            if avg_price and current_side == "sell" and price_current <= avg_price:
                log_debug("HANDLE_ENTRY", f"{symbol}: 가격 조건 미충족 (현재가 {price_current} <= 평단가 {avg_price})")
                return
        actual_entry_num = entry_count + 1

    if actual_entry_num <= MAX_ENTRIES:
        tp_val = TP_BASE_MAP[actual_entry_num-1] * SYMBOL_CONFIG[symbol]["tp_mult"]
        sl_val = SL_BASE_MAP[actual_entry_num-1] * SYMBOL_CONFIG[symbol]["sl_mult"]
        store_tp_sl(symbol, tp_val, sl_val, actual_entry_num)
        log_debug("HANDLE_ENTRY", f"{symbol} 진입#{actual_entry_num} TP {tp_val*100:.3f}% / SL {sl_val*100:.3f}% 저장")
    else:
        log_debug("HANDLE_ENTRY", f"{symbol} 진입#{actual_entry_num} TP/SL 맵 범위 초과")

    qty = calculate_qty(symbol, signal_type, time_mult)

    if qty <= 0:
        log_debug("HANDLE_ENTRY", f"{symbol}: 유효 수량 계산 실패")
        return

    with position_lock:
        pos = position_state.setdefault(symbol, {})
        pos["is_entering"] = True
        pos["entry_time"] = time.time()
        pos["entry_count"] = actual_entry_num
        pos["time_multiplier"] = time_mult
        pos["stored_tp_pct"] = tp_val
        pos["stored_sl_pct"] = sl_val
        if "sl_entry_count" not in pos:
            pos["sl_entry_count"] = 0

    success = place_order(symbol, desired_side, qty, actual_entry_num, time_mult)

    if success:
        log_debug("HANDLE_ENTRY", f"{symbol} {desired_side.upper()} 진입 성공, 수량: {qty}")
    else:
        log_debug("HANDLE_ENTRY", f"{symbol} {desired_side.upper()} 진입 실패")

    time.sleep(0.25)  # 진입중 동기화 시간
    with position_lock:
        pos["entry_time"] = time.time()
        pos["is_entering"] = False

    update_position(symbol)

# --- 실시간 가격 감시 및 TP/SL 평가 ---

async def price_monitor(symbols):
    ws_uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    subscribe_msg = {
        "time": int(time.time()),
        "channel": "futures.tickers",
        "event": "subscribe",
        "payload": symbols,
    }
    while True:
        try:
            async with websockets.connect(ws_uri) as ws:
                await ws.send(json.dumps(subscribe_msg))
                log_debug("WS", "가격 모니터링 시작")
                while True:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    data = json.loads(raw_msg)
                    if data.get("event") in ("error", "subscribe"):
                        continue
                    results = data.get("result", None)
                    if isinstance(results, list):
                        for ticker in results:
                            symbol = ticker.get("contract", "")
                            price = safe_decimal(ticker.get("last", "0"))
                            volume = safe_decimal(ticker.get("base_volume", "0"))
                            if symbol in symbol_data_map:
                                symbol_data_map[symbol].add_tick(float(price), float(volume), int(time.time()))
                            process_ticker(ticker)
                    elif isinstance(results, dict):
                        symbol = results.get("contract", "")
                        price = safe_decimal(results.get("last", "0"))
                        volume = safe_decimal(results.get("base_volume", "0"))
                        if symbol in symbol_data_map:
                            symbol_data_map[symbol].add_tick(float(price), float(volume), int(time.time()))
                        process_ticker(results)
        except Exception as e:
            log_debug("WS_ERROR", f"WebSocket 오류: {e} 재접속 시도 중...")
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
        if not pos:
            return
        last_close = pos.get("last_close_time", 0)
        if time.time() - last_close < 1.0:
            return
        if pos.get("is_entering", False):
            return

        now = time.time()
        entry_time = pos.get("entry_time", now)
        entry_price = pos["price"]
        side = pos["side"]
        entry_count = pos.get("entry_count", 0)

        if entry_count == 0:
            return

        decay_steps = int((now - entry_time) // 15)

        if now - entry_time < 5.0:
            tp_adj = Decimal("0.01")
            tp_orig, sl_orig, _ = get_tp_sl(symbol, entry_count)
        else:
            tp_decay_per_15s = Decimal("0.00002")
            tp_min = Decimal("0.0012")
            tp_orig, sl_orig, _ = get_tp_sl(symbol, entry_count)
            tp_mult = SYMBOL_CONFIG[symbol].get("tp_mult", Decimal("1.0"))
            tp_adj = max(tp_min, tp_orig - decay_steps * tp_decay_per_15s * tp_mult)

        sl_decay_per_15s = Decimal("0.00004")
        sl_min = Decimal("0.0009")
        sl_mult = SYMBOL_CONFIG[symbol].get("sl_mult", Decimal("1.0"))
        sl_adj = max(sl_min, sl_orig - decay_steps * sl_decay_per_15s * sl_mult)

        tp_price = entry_price * (1 + tp_adj) if side == "buy" else entry_price * (1 - tp_adj)
        sl_price = entry_price * (1 - sl_adj) if side == "buy" else entry_price * (1 + sl_adj)

        if (side == "buy" and price >= tp_price) or (side == "sell" and price <= tp_price):
            log_debug("TP_TRIGGER", f"{symbol} TP 도달 현재가={price}, TP가격={tp_price} (즉시 청산)")
            close_position(symbol, reason="TP")
            log_debug("TP_EXEC", f"{symbol} TP 청산 실행")

        if (side == "buy" and price <= sl_price) and entry_count >= MIN_ENTRY_FOR_SL:
            log_debug("SL_TRIGGER", f"{symbol} SL 발동 현재가={price}, SL가격={sl_price}, 진입횟수={entry_count}")
            close_position(symbol, reason="SL")
        elif (side == "sell" and price >= sl_price) and entry_count >= MIN_ENTRY_FOR_SL:
            log_debug("SL_TRIGGER", f"{symbol} SL 발동 현재가={price}, SL가격={sl_price}, 진입횟수={entry_count}")
            close_position(symbol, reason="SL")


# --- 워커 스레드 ---

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

# --- 진입 모니터 및 WebSocket 스레드 시작 ---

def entry_monitor_loop():
    while True:
        for symbol in SYMBOL_CONFIG.keys():
            side = check_entry_conditions(symbol)
            if side:
                pos = position_state.get(symbol, {})
                with position_lock:
                    if pos.get('entry_count', 0) >= MAX_ENTRIES:
                        continue
                    last_entry = pos.get('entry_time', 0)
                    if time.time() - last_entry < COOLDOWN_SECONDS:
                        continue

                    data = {
                        'symbol': symbol,
                        'side': side,
                        'signal': 'main',
                        'type': 'auto_entry'
                    }
                    try:
                        task_queue.put_nowait(data)
                        log_debug("ENTRY_MONITOR", f"{symbol} 자동진입 신호: {side}")
                    except queue.Full:
                        log_debug("ENTRY_MONITOR", "작업 큐 가득참 - 자동 진입 신호 누락")
        time.sleep(1)

def start_threads_and_ws():
    for i in range(8):
        threading.Thread(target=worker_thread, args=(i,), daemon=True, name=f"worker-{i}").start()
    threading.Thread(target=entry_monitor_loop, daemon=True, name="EntryMonitor").start()
    threading.Thread(target=lambda: asyncio.run(price_monitor(list(SYMBOL_CONFIG.keys()))), daemon=True, name="WebSocketMonitor").start()

# --- Flask 웹훅 서버, ping/status 엔드포인트 ---

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
        except Exception:
            pass

        if not data and request.form:
            data = dict(request.form)

        if not data:
            raw_data = request.get_data(as_text=True)
            if raw_data:
                try:
                    data = json.loads(raw_data)
                except:
                    try:
                        import urllib.parse
                        data = dict(urllib.parse.parse_qsl(raw_data))
                    except:
                        data = None

        if not data:
            return jsonify({"error": "Empty or invalid data"}), 400

        symbol_raw = data.get("symbol", "")
        action = str(data.get("action", "entry")).lower()

        if not symbol_raw:
            return jsonify({"error": "Symbol required"}), 400

        symbol = normalize_symbol(symbol_raw)
        if symbol not in SYMBOL_CONFIG:
            return jsonify({"error": f"Invalid symbol: {symbol_raw}"}), 400

        if is_duplicate_signal(data):
            return jsonify({"status": "duplicate"}), 200

        if action == "entry":
            try:
                task_queue.put_nowait(data)
                return jsonify({"status": "queued", "queue_size": task_queue.qsize()}), 200
            except queue.Full:
                return jsonify({"error": "Queue full"}), 429

        elif action == "exit":
            reason = str(data.get("reason", "")).strip().lower()
            if reason in ("tp", "sl"):
                return jsonify({"status": f"{action}_{reason}_ignored"}), 200
            update_position(symbol)
            with position_lock:
                pos = position_state.get(symbol)
                if pos and pos.get("size", Decimal("0")) > 0:
                    close_position(symbol, reason=action.upper())
                    return jsonify({"status": f"{action}_closed"}), 200
                else:
                    return jsonify({"status": "no_position"}), 200

        elif action in ("tp", "sl"):
            return jsonify({"status": f"{action}_ignored"}), 200

        else:
            return jsonify({"error": f"Unknown action: {action}"}), 400

    except Exception as e:
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
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 메인 함수 ---

def main():
    log_debug("STARTUP", "자동매매 서버 시작")
    for sym in SYMBOL_CONFIG.keys():
        update_position(sym)
    start_threads_and_ws()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
