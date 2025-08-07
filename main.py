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
from flask import Flask, jsonify

# Decimal 정밀도 설정
getcontext().prec = 12

# --- 설정 (환경 변수 등) ---
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE_CURRENCY = "usdt"

KST = pytz.timezone("Asia/Seoul")

COOLDOWN_SECONDS = 15     # tp_decay_sec 기반 쿨다운 (초)
MAX_ENTRIES = 5          # 피라미딩 최대 횟수
MAX_SL_RESCUES = 3       # 최대 SL Rescue 횟수
MIN_ENTRY_FOR_SL = 6     # SL 활성화 최소 진입 수

# 심볼별 매핑 및 설정 (가중치 포함)
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
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1")},
}

# 사용자 파인스크립트 내 배열 상수 그대로 (float → Decimal 으로 변환)
ENTRY_RATIOS = [Decimal(x) for x in [0.20, 0.30, 0.70, 1.60, 5.00]]
TP_LEVELS = [Decimal(x) for x in [0.005, 0.004, 0.0035, 0.003, 0.002]]
SL_LEVELS = [Decimal(x) for x in [0.04, 0.038, 0.035, 0.033, 0.03]]

# 기본 TP/SL (strategy input값 반영)
BASE_TP_PCT = Decimal("0.006")  # 0.6% default
BASE_SL_PCT = Decimal("0.04")   # 4.0% default

# RSI 설정 기본값 (입력값 예시)
RSI_LENGTH = 14
RSI_LONG_MAIN = 28
RSI_SHORT_MAIN = 72
RSI_15S_LONG = 21
RSI_15S_SHORT = 79
RSI_15S_LONG_EXIT = 78
RSI_15S_SHORT_EXIT = 22

ENGULF_RSI_OB = 79
ENGULF_RSI_OS = 21
ENGULF_EXIT_OB = 76
ENGULF_EXIT_OS = 24

USE_BB_FILTER = True
BB_LENGTH = 20
BB_MULT = 2.0
VOLUME_MULT = 1.5

# TP, SL 감소 관련 설정 (time decay)
TP_DECAY_SEC = 15.0      # 15초후 TP 감소 시작 (초단위)
TP_DECAY_AMT = 0.002     # 0.2% 감소
TP_MIN_PCT = 0.0012      # 0.12%

SL_DECAY_AMT = 0.004     # 0.4% 감소
SL_MIN_PCT = 0.0009      # 0.09%

ENABLE_ALERTS = True

# Logging
logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] [%(levelname)s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("AutoTrader")

def log_debug(tag, msg):
    logger.info(f"[{tag}] {msg}")

# API 클라이언트 초기화
config = Configuration(key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
futures_api = FuturesApi(api_client)

# 글로벌 상태 및 락
position_state = {}  # 심볼별 포지션 상태 딕셔너리
position_lock = threading.RLock()
tpsl_storage = {}
tpsl_lock = threading.RLock()
task_queue = queue.Queue(maxsize=100)

app = Flask(__name__)

# === SymbolData ===
class SymbolData:
    def __init__(self, symbol):
        self.symbol = symbol
        self.lock = threading.RLock()

        self.df_15s = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
        self.df_15s.index.name = 'timestamp'
        self.last_bar_time = None

        self.df_1m = None
        self.df_3m = None

    def add_tick(self, price, volume, timestamp=None):
        with self.lock:
            try:
                ts = int(timestamp or time.time())
                bar_time = ts - (ts % 15)
                bar_dt = pd.to_datetime(bar_time, unit='s')
                if bar_dt not in self.df_15s.index:
                    self.df_15s.loc[bar_dt] = [price, price, price, price, volume]
                    self.last_bar_time = bar_dt
                else:
                    self.df_15s.at[bar_dt, 'high'] = max(self.df_15s.at[bar_dt, 'high'], price)
                    self.df_15s.at[bar_dt, 'low'] = min(self.df_15s.at[bar_dt, 'low'], price)
                    self.df_15s.at[bar_dt, 'close'] = price
                    self.df_15s.at[bar_dt, 'volume'] += volume

                # 반드시 DatetimeIndex로 유지하기
                self.df_15s.index = pd.to_datetime(self.df_15s.index)
                self.df_15s.sort_index(inplace=True)

                if len(self.df_15s) > 200:
                    self.df_15s = self.df_15s.iloc[-200:]

                if len(self.df_15s) > 0:
                    self.df_1m = self.df_15s.resample('1min').agg({
                        'open': 'first',
                        'high': 'max',
                        'low': 'min',
                        'close': 'last',
                        'volume': 'sum'
                    }).dropna()
                    if len(self.df_1m) > 200:
                        self.df_1m = self.df_1m.iloc[-200:]

                    self.df_3m = self.df_15s.resample('3min').agg({
                        'open': 'first',
                        'high': 'max',
                        'low': 'min',
                        'close': 'last',
                        'volume': 'sum'
                    }).dropna()
                    if len(self.df_3m) > 200:
                        self.df_3m = self.df_3m.iloc[-200:]
                else:
                    self.df_1m = None
                    self.df_3m = None

            except Exception as e:
                logger.error(f"[SymbolData.add_tick] Exception: {e}")
                raise

    def compute_indicators(self):
        with self.lock:
            if (self.df_15s is None or len(self.df_15s) < 20 or
                self.df_1m is None or len(self.df_1m) < 20 or
                self.df_3m is None or len(self.df_3m) < 20):
                return None

            try:
                df15 = self.df_15s.copy()
                close15 = df15['close'].astype(float)

                df15['rsi_15s'] = ta.momentum.RSIIndicator(close15, window=RSI_LENGTH).rsi()
                bb = ta.volatility.BollingerBands(close15, window=BB_LENGTH, window_dev=BB_MULT)
                df15['bb_high'] = bb.bollinger_hband()
                df15['bb_low'] = bb.bollinger_lband()

                denom = df15['bb_high'] - df15['bb_low']
                df15['bb_pos'] = (close15 - df15['bb_low']) / denom.replace(0, np.nan)
                df15['vol_sma_20'] = df15['volume'].rolling(window=20).mean()

                last15 = df15.iloc[-1]
                if last15[['rsi_15s', 'bb_high', 'bb_low', 'bb_pos']].isnull().any():
                    return None

                df1m = self.df_1m.copy()
                close1m = df1m['close'].astype(float)
                df1m['rsi_1m'] = ta.momentum.RSIIndicator(close1m, window=RSI_LENGTH).rsi()
                df1m['vol_sma_20'] = df1m['volume'].rolling(window=20).mean()
                last1m = df1m.iloc[-1]
                if pd.isna(last1m['rsi_1m']):
                    return None

                df3m = self.df_3m.copy()
                close3m = df3m['close'].astype(float)
                df3m['rsi_3m'] = ta.momentum.RSIIndicator(close3m, window=RSI_LENGTH).rsi()
                df3m['vol_sma_20'] = df3m['volume'].rolling(window=20).mean()
                last3m = df3m.iloc[-1]
                if pd.isna(last3m['rsi_3m']):
                    return None

                return {
                    "15s": last15,
                    "1m": last1m,
                    "3m": last3m,
                }
            except Exception as e:
                logger.error(f"[SymbolData.compute_indicators] Exception: {e}")
                return None


# 함수: 심볼별 가중치 (사용자 전략 반영)
def get_symbol_multiplier(symbol: str) -> Decimal:
    sym = symbol.upper()
    if "BTC" in sym:
        return Decimal("0.55")
    elif "ETH" in sym:
        return Decimal("0.65")
    elif "SOL" in sym:
        return Decimal("0.8")
    elif "PEPE" in sym or "DOGE" in sym:
        return Decimal("1.2")
    else:
        return Decimal("1.0")


# 진입 조건 체크 함수
def check_entry_conditions(symbol: str):
    inds = symbol_data_map[symbol].compute_indicators()
    if inds is None:
        return None

    last15 = inds["15s"]
    last1m = inds["1m"]
    last3m = inds["3m"]

    keys_15s = ["rsi_15s", "bb_pos", "volume", "vol_sma_20"]
    keys_1m = ["rsi_1m", "volume", "vol_sma_20"]
    keys_3m = ["rsi_3m", "volume", "vol_sma_20"]

    for k in keys_15s:
        if k not in last15 or pd.isna(last15[k]):
            return None
    for k in keys_1m:
        if k not in last1m or pd.isna(last1m[k]):
            return None
    for k in keys_3m:
        if k not in last3m or pd.isna(last3m[k]):
            return None

    bb_pos = last15["bb_pos"]

    adj_factor = Decimal("1.0")
    # 심볼별 가중치 반영
    adj_factor = get_symbol_multiplier(symbol)

    cond_long = (
        last3m["rsi_3m"] <= Decimal(RSI_LONG_MAIN)
        and last1m["rsi_1m"] <= Decimal(RSI_LONG_MAIN)
        and last15["rsi_15s"] <= Decimal(RSI_15S_LONG)
        and last3m["volume"] >= last3m["vol_sma_20"] * Decimal(VOLUME_MULT)
        and last1m["volume"] >= last1m["vol_sma_20"] * Decimal(VOLUME_MULT)
        and last15["volume"] >= last15["vol_sma_20"] * Decimal(VOLUME_MULT)
        and (not USE_BB_FILTER or bb_pos <= Decimal("0.10"))
    )

    cond_short = (
        last3m["rsi_3m"] >= Decimal(RSI_SHORT_MAIN)
        and last1m["rsi_1m"] >= Decimal(RSI_SHORT_MAIN)
        and last15["rsi_15s"] >= Decimal(RSI_15S_SHORT)
        and last3m["volume"] >= last3m["vol_sma_20"] * Decimal(VOLUME_MULT)
        and last1m["volume"] >= last1m["vol_sma_20"] * Decimal(VOLUME_MULT)
        and last15["volume"] >= last15["vol_sma_20"] * Decimal(VOLUME_MULT)
        and (not USE_BB_FILTER or bb_pos >= Decimal("0.90"))
    )

    if cond_long:
        return "long"
    elif cond_short:
        return "short"
    else:
        return None


# TP/SL 저장 (전략용)
def store_tp_sl(symbol, tp_pct, sl_pct, entry_num):
    with tpsl_lock:
        tpsl_storage[(symbol, entry_num)] = {"tp": tp_pct, "sl": sl_pct}


# 주문 수량 계산
def calculate_qty(symbol: str, signal_type: str, entry_multiplier: Decimal) -> Decimal:
    cfg = SYMBOL_CONFIG[symbol]
    qty_mult = Decimal("1.0")  # 심볼 가중치는 진입 조건에 반영하여 취한다
    equity = get_account_equity()
    price = get_current_price(symbol)

    if equity <= 0 or price <= 0:
        return Decimal("0")

    current_entry_count = position_state.get(symbol, {}).get("entry_count", 0)

    if current_entry_count >= MAX_ENTRIES:
        return Decimal("0")

    entry_ratios = [Decimal("10"), Decimal("20"), Decimal("50"), Decimal("120"), Decimal("400")]

    base_ratio = entry_ratios[current_entry_count]

    if signal_type == "sl_rescue":
        base_ratio *= Decimal("1.5")

    position_value = equity * (base_ratio / Decimal("100")) * entry_multiplier * qty_mult

    contract_size = cfg["contract_size"]
    qty_step = cfg["qty_step"]

    qty_raw = (position_value / (price * contract_size)) / qty_step
    qty_floor = qty_raw.quantize(Decimal("1"), rounding=ROUND_DOWN)
    qty = qty_floor * qty_step

    qty = max(qty, cfg["min_qty"])

    notional = qty * price * contract_size
    if notional < cfg["min_notional"]:
        min_qty = (cfg["min_notional"] / (price * contract_size)) / qty_step
        min_qty_floor = min_qty.quantize(Decimal("1"), rounding=ROUND_DOWN)
        qty = max(qty, min_qty_floor * qty_step)

    return qty


# SL Rescue 조건 체크
def is_sl_rescue_condition(symbol: str) -> bool:
    with position_lock:
        pos = position_state.get(symbol)
        if (
            not pos or pos.get("size", Decimal("0")) == 0
            or pos.get("sl_entry_count", 0) >= MAX_SL_RESCUES
        ):
            return False
        current_price = get_current_price(symbol)
        if current_price <= 0:
            return False
        entry_price = pos.get("price")
        side = pos.get("side")
        entry_count = pos.get("entry_count", 0)
        if not entry_price or side not in ("buy", "sell") or entry_count == 0:
            return False

        tp_val, sl_val, _ = get_tp_sl(symbol, entry_count)
        sl_mult = SYMBOL_CONFIG.get(symbol, {}).get("sl_mult", Decimal("1.0"))
        sl_pct = SL_BASE_MAP[min(entry_count - 1, len(SL_BASE_MAP) - 1)] * sl_mult

        if side == "buy":
            sl_price = entry_price * (1 - sl_pct)
            return current_price <= sl_price
        else:
            sl_price = entry_price * (1 + sl_pct)
            return current_price >= sl_price


# 주문 실행
def place_order(symbol: str, side: str, qty: Decimal, entry_num: int, time_multiplier: Decimal) -> bool:
    with position_lock:
        cfg = SYMBOL_CONFIG[symbol]
        qty_adj = qty.quantize(cfg.get("qty_step", Decimal("1")), rounding=ROUND_DOWN)
        if qty_adj < cfg["min_qty"]:
            return False
        price = get_current_price(symbol)
        if price <= 0:
            return False
        order_size = float(qty_adj) if side == "buy" else -float(qty_adj)
        max_allowed_value = get_account_equity() * Decimal("10")
        order_value = qty_adj * price * cfg["contract_size"]
        if order_value > max_allowed_value:
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
            return False

        pos = position_state.setdefault(symbol, {})
        pos["entry_count"] = entry_num
        pos["entry_time"] = time.time()
        pos["time_multiplier"] = time_multiplier
        if "sl_entry_count" not in pos:
            pos["sl_entry_count"] = 0

        time.sleep(2)
        update_position(symbol)
    return True


# 청산 실행
def close_position(symbol: str, reason: str = "manual") -> bool:
    with position_lock:
        pos = position_state.get(symbol, {})
        now = time.time()
        last_close = pos.get("last_close_time", 0)
        if now - last_close < 1.0:
            return False

        order = FuturesOrder(
            contract=symbol,
            size=0,
            price="0",
            tif="ioc",
            close=True,
        )
        result = call_api_with_retry(futures_api.create_futures_order, SETTLE_CURRENCY, order)
        if not result:
            return False

        pos["last_close_time"] = now
        position_state.pop(symbol, None)

        with tpsl_lock:
            tpsl_storage.pop(symbol, None)

        time.sleep(1)
        update_position(symbol)
    return True


# 포지션 업데이트
def update_position(symbol: str):
    with position_lock:
        try:
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
                        "stored_tp_pct": current_pos.get("stored_tp_pct", Decimal("0")),
                        "stored_sl_pct": current_pos.get("stored_sl_pct", Decimal("0")),
                        "entry_time": current_pos.get("entry_time", time.time()),
                        "time_multiplier": current_pos.get("time_multiplier", Decimal("1.0")),
                        "is_entering": current_pos.get("is_entering", False),
                        "last_close_time": current_pos.get("last_close_time", 0),
                    }
            else:
                position_state.pop(symbol, None)
        except Exception as e:
            log_debug("UPDATE_POSITION_ERROR", f"{symbol} 포지션 업데이트 중 오류: {e}")


# 진입 처리 함수
def handle_entry(data: dict):
    symbol_raw = data.get("symbol", "")
    side_raw = data.get("side", "").lower()
    signal_type = data.get("signal", "none").lower()
    entry_type = data.get("type", "").lower()

    symbol = normalize_symbol(symbol_raw)
    if symbol not in SYMBOL_CONFIG:
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
        return

    if current_side and current_side != desired_side and entry_count > 0:
        if not close_position(symbol, reason="reverse_entry"):
            return
        time.sleep(1)
        update_position(symbol)
        entry_count = 0
        time_mult = Decimal("1.0")

    if entry_count >= MAX_ENTRIES:
        return

    is_sl_rescue = (signal_type == "sl_rescue")
    if is_sl_rescue:
        if sl_entry_count >= MAX_SL_RESCUES:
            return
        if not is_sl_rescue_condition(symbol):
            return
        with position_lock:
            position_state[symbol]["sl_entry_count"] = sl_entry_count + 1
        actual_entry_num = entry_count + 1
    else:
        if entry_count > 0:
            current_price = get_current_price(symbol)
            avg_price = position_state[symbol].get("price")
            if avg_price and current_side == "buy" and current_price >= avg_price:
                return
            if avg_price and current_side == "sell" and current_price <= avg_price:
                return
        actual_entry_num = entry_count + 1

    if actual_entry_num <= MAX_ENTRIES:
        tp_val = TP_BASE_MAP[actual_entry_num - 1] * get_symbol_multiplier(symbol)
        sl_val = SL_BASE_MAP[actual_entry_num - 1] * get_symbol_multiplier(symbol)
        store_tp_sl(symbol, tp_val, sl_val, actual_entry_num)
    else:
        return

    qty = calculate_qty(symbol, signal_type, time_mult)
    if qty <= 0:
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

    place_order(symbol, desired_side, qty, actual_entry_num, time_mult)

    time.sleep(0.25)
    with position_lock:
        pos = position_state.get(symbol, {})
        pos["entry_time"] = time.time()
        pos["is_entering"] = False

    update_position(symbol)


# 워커 스레드 함수
def worker_thread(worker_id: int):
    while True:
        try:
            data = task_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            handle_entry(data)
        except Exception as e:
            log_debug(f"Worker-{worker_id} ERROR", f"입력 처리 실패: {e}")
        finally:
            task_queue.task_done()


# 자동 진입 감시 루프
def entry_monitor_loop():
    while True:
        for symbol in SYMBOL_CONFIG.keys():
            side = check_entry_conditions(symbol)
            if side:
                with position_lock:
                    pos = position_state.get(symbol, {})
                    if pos.get("entry_count", 0) >= MAX_ENTRIES:
                        continue
                    last_entry = pos.get("entry_time", 0)
                    if time.time() - last_entry < COOLDOWN_SECONDS:
                        continue
                try:
                    task_queue.put_nowait({
                        "symbol": symbol,
                        "side": side,
                        "signal": "main",
                        "type": "auto_entry"
                    })
                    log_debug("ENTRY_MONITOR", f"{symbol} 자동진입 신호: {side}")
                except queue.Full:
                    log_debug("ENTRY_MONITOR", "큐 가득참, 신호 누락")
        time.sleep(1)


# WebSocket 실시간 가격 모니터
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
                                symbol_data_map[symbol].add_tick(float(price), float(volume))
                            process_ticker(ticker)
                    elif isinstance(results, dict):
                        symbol = results.get("contract", "")
                        price = safe_decimal(results.get("last", "0"))
                        volume = safe_decimal(results.get("base_volume", "0"))
                        if symbol in symbol_data_map:
                            symbol_data_map[symbol].add_tick(float(price), float(volume))
                        process_ticker(results)
        except Exception as e:
            log_debug("WS_ERROR", f"WebSocket 오류: {e} 재접속 대기중...")
            await asyncio.sleep(5)


# 가격정보 매 처리 함수
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
        entry_price = pos.get("price")
        side = pos.get("side")
        entry_count = pos.get("entry_count", 0)

        if entry_count == 0 or entry_price is None or side is None:
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
            close_position(symbol, reason="TP")

        if (side == "buy" and price <= sl_price) and entry_count >= MIN_ENTRY_FOR_SL:
            close_position(symbol, reason="SL")
        elif (side == "sell" and price >= sl_price) and entry_count >= MIN_ENTRY_FOR_SL:
            close_position(symbol, reason="SL")


# 반드시 구현 필요: get_tp_sl() 함수 예시
def get_tp_sl(symbol, entry_count):
    """TP/SL 값을 SYMBOL_CONFIG 기반 및 entry_count 로 결정"""
    tp = TP_BASE_MAP[min(entry_count - 1, len(TP_BASE_MAP) - 1)] * get_symbol_multiplier(symbol)
    sl = SL_BASE_MAP[min(entry_count - 1, len(SL_BASE_MAP) - 1)] * get_symbol_multiplier(symbol)
    # 필요시 종료 포인트(===) 등을 포함하여 반환, 여기서는 임시로 None 추가
    return tp, sl, None


# 서버 스레드 시작 및 Flask 실행
def start_threads_and_ws():
    for i in range(8):
        threading.Thread(target=worker_thread, args=(i,), daemon=True, name=f"worker-{i}").start()
    threading.Thread(target=entry_monitor_loop, daemon=True, name="EntryMonitor").start()
    threading.Thread(target=lambda: asyncio.run(price_monitor(list(SYMBOL_CONFIG.keys()))), daemon=True, name="WSMonitor").start()


# /ping 엔드포인트 (업타임 모니터용)
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


# 메인 함수
def main():
    log_debug("STARTUP", "자동매매 서버 시작 중...")
    for sym in SYMBOL_CONFIG.keys():
        update_position(sym)
    start_threads_and_ws()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
