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

# Decimal 정밀도
getcontext().prec = 12

# 환경 변수 및 상수
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE_CURRENCY = "usdt"
KST = pytz.timezone("Asia/Seoul")

# 전략 파라미터
COOLDOWN_SECONDS = 15          
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
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"), "min_notional": Decimal("5"), "tp_mult": Decimal("0.55"), "sl_mult": Decimal("0.55"), "qty_mult": Decimal("1.5")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"), "min_notional": Decimal("5"), "tp_mult": Decimal("0.65"), "sl_mult": Decimal("0.65"), "qty_mult": Decimal("1.3")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": Decimal("0.8"), "sl_mult": Decimal("0.8"), "qty_mult": Decimal("1.2")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"), "qty_mult": Decimal("1.0")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"), "qty_mult": Decimal("1.0")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0"), "qty_mult": Decimal("1.0")},
}

ENTRY_RATIOS = [Decimal(x) for x in [0.20, 0.30, 0.70, 1.60, 5.00]]
TP_LEVELS = [Decimal(x) for x in [0.005, 0.004, 0.0035, 0.003, 0.002]]
SL_LEVELS = [Decimal(x) for x in [0.04, 0.038, 0.035, 0.033, 0.03]]

BASE_TP_PCT = Decimal("0.006")
BASE_SL_PCT = Decimal("0.04")
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

TP_DECAY_SEC = 15.0
TP_DECAY_AMT = Decimal("0.002")
TP_MIN_PCT = Decimal("0.0012")
SL_DECAY_AMT = Decimal("0.004")
SL_MIN_PCT = Decimal("0.0009")

ENABLE_ALERTS = True

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] [%(levelname)s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("AutoTrader")

def log_debug(tag, msg):
    logger.info(f"[{tag}] {msg}")

config = Configuration(key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
futures_api = FuturesApi(api_client)

position_state = {}
position_lock = threading.RLock()
tpsl_storage = {}
tpsl_lock = threading.RLock()
task_queue = queue.Queue(maxsize=100)

app = Flask(__name__)

# --- SymbolData 클래스 ---
class SymbolData:
    def __init__(self, symbol):
        self.symbol = symbol
        self.lock = threading.RLock()
        self.df_15s = pd.DataFrame(columns=['open','high','low','close','volume'])
        self.df_15s.index.name='timestamp'
        self.last_bar_time=None
        self.df_1m=None
        self.df_3m=None

    def add_tick(self, price, volume, timestamp=None):
        with self.lock:
            try:
                ts = int(timestamp or time.time())
                bar_time = ts - (ts % 15)
                bar_dt = pd.to_datetime(bar_time, unit='s')
                if bar_dt not in self.df_15s.index:
                    self.df_15s.loc[bar_dt]=[price, price, price, price, volume]
                    self.last_bar_time=bar_dt
                else:
                    self.df_15s.at[bar_dt, 'high']=max(self.df_15s.at[bar_dt, 'high'], price)
                    self.df_15s.at[bar_dt, 'low']=min(self.df_15s.at[bar_dt, 'low'], price)
                    self.df_15s.at[bar_dt, 'close']=price
                    self.df_15s.at[bar_dt, 'volume']+=volume
                self.df_15s.index=pd.to_datetime(self.df_15s.index)
                self.df_15s.sort_index(inplace=True)
                if len(self.df_15s)>200:
                    self.df_15s=self.df_15s.iloc[-200:]
                if len(self.df_15s)>0:
                    self.df_1m=self.df_15s.resample('1min').agg({
                        'open': 'first',
                        'high': 'max',
                        'low': 'min',
                        'close': 'last',
                        'volume': 'sum'
                    }).dropna()
                    if len(self.df_1m)>200:
                        self.df_1m=self.df_1m.iloc[-200:]
                    self.df_3m=self.df_15s.resample('3min').agg({
                        'open': 'first',
                        'high': 'max',
                        'low': 'min',
                        'close': 'last',
                        'volume': 'sum'
                    }).dropna()
                    if len(self.df_3m)>200:
                        self.df_3m=self.df_3m.iloc[-200:]
                else:
                    self.df_1m=None
                    self.df_3m=None
            except Exception as e:
                logger.error(f"[SymbolData.add_tick] Exception: {e}")
                raise

    def compute_indicators(self):
        with self.lock:
            if (self.df_15s is None or len(self.df_15s)<20 or
                self.df_1m is None or len(self.df_1m)<20 or
                self.df_3m is None or len(self.df_3m)<20):
                return None
            try:
                df15=self.df_15s.copy()
                close15=df15['close'].astype(float)
                df15['rsi_15s']=ta.momentum.RSIIndicator(close15, window=RSI_LENGTH).rsi()
                bb=ta.volatility.BollingerBands(close15, window=BB_LENGTH, window_dev=BB_MULT)
                df15['bb_high']=bb.bollinger_hband()
                df15['bb_low']=bb.bollinger_lband()
                denom=df15['bb_high']-df15['bb_low']
                df15['bb_pos']=(close15 - df15['bb_low']) / denom.replace({0: np.nan})
                df15['vol_sma_20'] = df15['volume'].rolling(20).mean()

                last15=df15.iloc[-1]
                if last15[['rsi_15s','bb_high','bb_low','bb_pos']].isnull().any():
                    return None

                df1m=self.df_1m.copy()
                close1m=df1m['close'].astype(float)
                df1m['rsi_1m']=ta.momentum.RSIIndicator(close1m, window=RSI_LENGTH).rsi()
                df1m['vol_sma_20']=df1m['volume'].rolling(20).mean()
                last1m=df1m.iloc[-1]
                if pd.isna(last1m['rsi_1m']):
                    return None

                df3m=self.df_3m.copy()
                close3m=df3m['close'].astype(float)
                df3m['rsi_3m']=ta.momentum.RSIIndicator(close3m, window=RSI_LENGTH).rsi()
                df3m['vol_sma_20']=df3m['volume'].rolling(20).mean()
                last3m=df3m.iloc[-1]
                if pd.isna(last3m['rsi_3m']):
                    return None

                return {'15s': last15, '1m': last1m, '3m': last3m}
            except Exception as e:
                logger.error(f"[SymbolData.compute_indicators] Exception: {e}")
                return None

symbol_data_map={sym:SymbolData(sym) for sym in SYMBOL_CONFIG.keys()}

def get_symbol_multiplier(symbol: str) -> Decimal:
    sym = symbol.upper()
    if "BTC" in sym:
        return Decimal("0.55")
    elif "ETH" in sym:
        return Decimal("0.65")
    elif "SOL" in sym:
        return Decimal("0.80")
    elif "PEPE" in sym or "DOGE" in sym:
        return Decimal("1.2")
    else:
        return Decimal("1.0")

def calculate_qty(symbol: str, signal_type: str, entry_multiplier: Decimal) -> Decimal:
    cfg=SYMBOL_CONFIG[symbol]
    symbol_mult=get_symbol_multiplier(symbol)
    equity=get_account_equity()
    price=get_current_price(symbol)
    if equity<=0 or price<=0:
        return Decimal("0")
    cnt=position_state.get(symbol, {}).get("entry_count", 0)
    if cnt>=MAX_ENTRIES:
        return Decimal("0")
    base_ratio=ENTRY_RATIOS[cnt]
    if signal_type=="sl_rescue":
        base_ratio*=Decimal("1.5")
    position_val=equity*base_ratio*symbol_mult/Decimal("100")
    qty_raw=(position_val/(price*cfg["contract_size"])) / cfg["qty_step"]
    qty_floor=qty_raw.quantize(Decimal("1"), rounding=ROUND_DOWN)
    qty=qty_floor*cfg["qty_step"]
    qty=max(qty, cfg["min_qty"])
    notional=qty*price*cfg["contract_size"]
    if notional<cfg.get("min_notional", Decimal("0")):
        min_qty=(cfg["min_notional"]/(price*cfg["contract_size"])) / cfg["qty_step"]
        min_qty_floor=min_qty.quantize(Decimal("1"), rounding=ROUND_DOWN)
        qty=max(qty, min_qty_floor*cfg["qty_step"])
    return qty

def is_sl_rescue_condition(symbol: str) -> bool:
    with position_lock:
        pos=position_state.get(symbol)
        if not pos or pos.get("size", Decimal("0"))==0 or pos.get("sl_entry_count", 0)>=MAX_SL_RESCUES:
            return False
        current_price=get_current_price(symbol)
        if current_price<=0:
            return False
        entry_price=pos.get("price")
        side=pos.get("side")
        entry_count=pos.get("entry_count", 0)
        if not entry_price or side not in ("buy", "sell") or entry_count==0:
            return False
        tp_val, sl_val, _=get_tp_sl(symbol, entry_count)
        sl_mult=SYMBOL_CONFIG.get(symbol, {}).get("sl_mult", Decimal("1.0"))
        sl_pct=SL_LEVELS[min(entry_count-1, 4)]*sl_mult
        if side=="buy":
            sl_price=entry_price*(1-sl_pct)
            return current_price<=sl_price
        else:
            sl_price=entry_price*(1+sl_pct)
            return current_price>=sl_price

def get_tp_sl(symbol:str, entry_count:int):
    symbol_mult=get_symbol_multiplier(symbol)
    tp=TP_LEVELS[min(entry_count-1, len(TP_LEVELS)-1)]*symbol_mult
    sl=SL_LEVELS[min(entry_count-1, len(SL_LEVELS)-1)]*symbol_mult
    return tp, sl, None

def place_order(symbol: str, side: str, qty: Decimal, entry_num: int, time_multiplier: Decimal) -> bool:
    with position_lock:
        cfg=SYMBOL_CONFIG[symbol]
        qty_adj=qty.quantize(cfg.get("qty_step", Decimal("1")), rounding=ROUND_DOWN)
        if qty_adj<cfg["min_qty"]:
            return False
        price=get_current_price(symbol)
        if price<=0:
            return False
        order_size=float(qty_adj) if side=="buy" else -float(qty_adj)
        max_allowed_value=get_account_equity()*Decimal("10")
        order_value=qty_adj*price*cfg["contract_size"]
        if order_value>max_allowed_value:
            return False
        order=FuturesOrder(
            contract=symbol,
            size=order_size,
            price="0",
            tif="ioc",
            reduce_only=False,
        )
        result=call_api_with_retry(futures_api.create_futures_order, SETTLE_CURRENCY, order)
        if not result:
            return False
        pos=position_state.setdefault(symbol, {})
        pos["entry_count"]=entry_num
        pos["entry_time"]=time.time()
        pos["time_multiplier"]=time_multiplier
        if "sl_entry_count" not in pos:
            pos["sl_entry_count"]=0
        time.sleep(2)
        update_position(symbol)
    return True

def close_position(symbol: str, reason: str = "manual") -> bool:
    with position_lock:
        pos=position_state.get(symbol, {})
        now=time.time()
        last_close=pos.get("last_close_time", 0)
        if now-last_close<1.0:
            return False
        order=FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result=call_api_with_retry(futures_api.create_futures_order, SETTLE_CURRENCY, order)
        if not result:
            return False
        pos["last_close_time"]=now
        position_state.pop(symbol, None)
        with tpsl_lock:
            tpsl_storage.pop(symbol, None)
        time.sleep(1)
        update_position(symbol)
    return True

def update_position(symbol: str):
    with position_lock:
        try:
            pos_info=call_api_with_retry(futures_api.get_position, SETTLE_CURRENCY, symbol)
            if pos_info and pos_info.size:
                size=safe_decimal(pos_info.size)
                if size==0:
                    position_state.pop(symbol, None)
                else:
                    entry_price=safe_decimal(pos_info.entry_price)
                    side="buy" if size>0 else "sell"
                    size_abs=abs(size)
                    current_pos=position_state.get(symbol, {})
                    position_state[symbol]={
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

def handle_entry(data: dict):
    symbol_raw=data.get("symbol", "")
    side_raw=data.get("side", "").lower()
    signal_type=data.get("signal", "none").lower()
    entry_type=data.get("type", "").lower()

    symbol=normalize_symbol(symbol_raw)
    if symbol not in SYMBOL_CONFIG:
        return

    update_position(symbol)

    with position_lock:
        pos=position_state.get(symbol, {})
        current_side=pos.get("side")
        entry_count=pos.get("entry_count", 0)
        sl_entry_count=pos.get("sl_entry_count", 0)
        time_mult=pos.get("time_multiplier", Decimal("1.0"))

    desired_side="buy" if side_raw=="long" else "sell" if side_raw=="short" else None
    if not desired_side:
        return

    if current_side and current_side!=desired_side and entry_count>0:
        if not close_position(symbol, reason="reverse_entry"):
            return
        time.sleep(1)
        update_position(symbol)
        entry_count=0
        time_mult=Decimal("1.0")

    if entry_count>=MAX_ENTRIES:
        return

    is_sl_rescue=(signal_type=="sl_rescue")
    if is_sl_rescue:
        if sl_entry_count>=MAX_SL_RESCUES:
            return
        if not is_sl_rescue_condition(symbol):
            return
        with position_lock:
            position_state[symbol]["sl_entry_count"]=sl_entry_count+1
        actual_entry_num=entry_count+1
    else:
        if entry_count>0:
            current_price=get_current_price(symbol)
            avg_price=position_state[symbol].get("price")
            if avg_price and current_side=="buy" and current_price>=avg_price:
                return
            if avg_price and current_side=="sell" and current_price<=avg_price:
                return
        actual_entry_num=entry_count+1

    if actual_entry_num<=MAX_ENTRIES:
        tp_val=TP_LEVELS[actual_entry_num-1]*get_symbol_multiplier(symbol)
        sl_val=SL_LEVELS[actual_entry_num-1]*get_symbol_multiplier(symbol)
        store_tp_sl(symbol, tp_val, sl_val, actual_entry_num)
    else:
        return

    qty=calculate_qty(symbol, signal_type, time_mult)
    if qty<=0:
        return

    with position_lock:
        pos=position_state.setdefault(symbol, {})
        pos["is_entering"]=True
        pos["entry_time"]=time.time()
        pos["entry_count"]=actual_entry_num
        pos["time_multiplier"]=time_mult
        pos["stored_tp_pct"]=tp_val
        pos["stored_sl_pct"]=sl_val
        if "sl_entry_count" not in pos:
            pos["sl_entry_count"]=0

    place_order(symbol, desired_side, qty, actual_entry_num, time_mult)

    time.sleep(0.25)
    with position_lock:
        pos=position_state.get(symbol, {})
        pos["entry_time"]=time.time()
        pos["is_entering"]=False

    update_position(symbol)

def worker_thread(worker_id: int):
    while True:
        try:
            data=task_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            handle_entry(data)
        except Exception as e:
            log_debug(f"Worker-{worker_id} ERROR", f"입력 처리 실패: {e}")
        finally:
            task_queue.task_done()

def entry_monitor_loop():
    while True:
        for symbol in SYMBOL_CONFIG.keys():
            side=check_entry_conditions(symbol)
            if side:
                with position_lock:
                    pos=position_state.get(symbol, {})
                    if pos.get("entry_count", 0)>=MAX_ENTRIES:
                        continue
                    last_entry=pos.get("entry_time", 0)
                    if time.time()-last_entry<COOLDOWN_SECONDS:
                        continue
                try:
                    task_queue.put_nowait({
                        "symbol": symbol,
                        "side": side,
                        "signal": "main",
                        "type": "auto_entry",
                    })
                    log_debug("ENTRY_MONITOR", f"{symbol} 자동진입 신호: {side}")
                except queue.Full:
                    log_debug("ENTRY_MONITOR", "큐 가득참, 신호 누락")
        time.sleep(1)

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
                    raw_msg=await asyncio.wait_for(ws.recv(), timeout=45)
                    data=json.loads(raw_msg)
                    if data.get("event") in ("error","subscribe"):
                        continue
                    results=data.get("result", None)
                    if isinstance(results, list):
                        for ticker in results:
                            symbol=ticker.get("contract", "")
                            price=safe_decimal(ticker.get("last", "0"))
                            volume=safe_decimal(ticker.get("base_volume", "0"))
                            if symbol in symbol_data_map:
                                symbol_data_map[symbol].add_tick(float(price), float(volume))
                            process_ticker(ticker)
                    elif isinstance(results, dict):
                        symbol=results.get("contract", "")
                        price=safe_decimal(results.get("last", "0"))
                        volume=safe_decimal(results.get("base_volume", "0"))
                        if symbol in symbol_data_map:
                            symbol_data_map[symbol].add_tick(float(price), float(volume))
                        process_ticker(results)
        except Exception as e:
            log_debug("WS_ERROR", f"WebSocket 오류: {e} 재접속 대기중...")
            await asyncio.sleep(5)

def process_ticker(ticker: dict):
    symbol=ticker.get("contract", "")
    if symbol not in SYMBOL_CONFIG:
        return

    price=safe_decimal(ticker.get("last", "0"))
    if price <= 0:
        return

    with position_lock:
        pos=position_state.get(symbol)
        if not pos or pos.get("is_entering", False):
            return
        now=time.time()
        entry_time=pos.get("entry_time", now)
        entry_price=pos.get("price")
        side=pos.get("side")
        entry_count=pos.get("entry_count", 0)
        if entry_count==0 or entry_price is None or side is None:
            return

        # TP/SL 감소(Decay) 로직 (시간경과에 따라서 TP, SL이 줄어듦)
        sec_passed=now-entry_time
        periods=int(sec_passed//TP_DECAY_SEC)
        symbol_mult=get_symbol_multiplier(symbol)

        base_tp=TP_LEVELS[min(entry_count-1, len(TP_LEVELS)-1)]*symbol_mult
        base_sl=SL_LEVELS[min(entry_count-1, len(SL_LEVELS)-1)]*symbol_mult

        tp_decay=TP_DECAY_AMT * symbol_mult
        sl_decay=SL_DECAY_AMT * symbol_mult

        tp_reduction=periods*tp_decay
        sl_reduction=periods*sl_decay

        decayed_tp=max(TP_MIN_PCT, base_tp-tp_reduction)
        decayed_sl=max(SL_MIN_PCT, base_sl-sl_reduction)

        if side=="buy":
            tp_price=entry_price*(1+decayed_tp)
            sl_price=entry_price*(1-decayed_sl)
            if price>=tp_price:
                close_position(symbol, reason="TP")
            if entry_count>=MIN_ENTRY_FOR_SL and price<=sl_price:
                close_position(symbol, reason="SL")
        else:  # sell
            tp_price=entry_price*(1-decayed_tp)
            sl_price=entry_price*(1+decayed_sl)
            if price<=tp_price:
                close_position(symbol, reason="TP")
            if entry_count>=MIN_ENTRY_FOR_SL and price>=sl_price:
                close_position(symbol, reason="SL")

        # 엔걸핑 진입 및 청산 신호(간단 예시)
        inds=symbol_data_map[symbol].compute_indicators()
        if inds is None:
            return
        rsi_3m=inds["3m"]["rsi_3m"]

        df15 = symbol_data_map[symbol].df_15s
        if df15 is None or len(df15)<2:
            return
        last_candle=df15.iloc[-1]
        prev_candle=df15.iloc[-2]

        bullish_engulf = last_candle['close'] >= prev_candle['open'] and prev_candle['close'] < prev_candle['open']
        bearish_engulf = last_candle['close'] <= prev_candle['open'] and prev_candle['close'] > prev_candle['open']

        engulf_long_exit = (side=="buy" and USE_BB_FILTER and bearish_engulf and (rsi_3m >= ENGULF_EXIT_OB))
        engulf_short_exit = (side=="sell" and USE_BB_FILTER and bullish_engulf and (rsi_3m <= ENGULF_EXIT_OS))

        if engulf_long_exit:
            close_position(symbol, reason="ENGULF")
        if engulf_short_exit:
            close_position(symbol, reason="ENGULF")

def normalize_symbol(raw_symbol: str):
    return SYMBOL_MAPPING.get(raw_symbol.strip().upper(), raw_symbol.strip().upper())

def safe_decimal(val, default=Decimal("0")):
    try:
        return Decimal(str(val))
    except Exception:
        return default

def call_api_with_retry(func, *args, retries=3, delay=2, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except gate_api_exceptions.ApiException as e:
            log_debug("API_ERROR", f"ApiException: status={e.status}, reason={e.reason}")
        except Exception as e:
            log_debug("API_ERROR", f"Exception: {e}")
        if attempt < retries - 1:
            time.sleep(delay)
    return None

def get_account_equity():
    try:
        acc_info = futures_api.list_futures_accounts(SETTLE_CURRENCY)
        if hasattr(acc_info, "available"):
            return Decimal(str(acc_info.available))
        if isinstance(acc_info, list) and acc_info and hasattr(acc_info[0], "available"):
            return Decimal(str(acc_info[0].available))
    except Exception as e:
        log_debug("BALANCE_ERROR", f"잔고 조회 실패: {e}")
    return Decimal("0")

def get_current_price(symbol):
    tickers = call_api_with_retry(futures_api.list_futures_tickers, settle=SETTLE_CURRENCY, contract=symbol)
    if tickers and isinstance(tickers, list) and tickers:
        return safe_decimal(getattr(tickers[0], "last", None), Decimal("0"))
    return Decimal("0")

def start_threads_and_ws():
    for i in range(8):
        threading.Thread(target=worker_thread, args=(i,), daemon=True, name=f"worker-{i}").start()
    threading.Thread(target=entry_monitor_loop, daemon=True, name="EntryMonitor").start()
    threading.Thread(target=lambda: asyncio.run(price_monitor(list(SYMBOL_CONFIG.keys()))), daemon=True, name="WSMonitor").start()

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

def main():
    log_debug("STARTUP", "자동매매 서버 시작 중...")
    for sym in SYMBOL_CONFIG.keys():
        update_position(sym)
    start_threads_and_ws()
    port=int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__=="__main__":
    main()
