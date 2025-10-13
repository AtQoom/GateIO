#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ONDO 역방향 그리드 매매 시스템 v16.3-FINAL
- TP 기반 그리드 재생성
- 듀얼 TP (평단가/개별)
- 모든 주문 지정가
- 변화 감지 방식 중복 방지
"""

import os
import time
import asyncio
import threading
import logging
import json
from decimal import Decimal, ROUND_DOWN
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi
import websockets
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# 설정
# =============================================================================

SETTLE = "usdt"
SYMBOL = "ONDO_USDT"
CONTRACT_SIZE = Decimal("1")

GRID_GAP_PCT = Decimal("0.21") / Decimal("100")  # 0.20%
TP_GAP_PCT = Decimal("0.21") / Decimal("100")    # 0.20%
HEDGE_RATIO = Decimal("0.2")  # 헤징 0.2배
THRESHOLD_RATIO = Decimal("5.0")  # 임계값 5배

# API 설정
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
if not API_KEY or not API_SECRET:
    logger.critical("API 키 없음")
    exit(1)

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# 전역 변수
position_lock = threading.RLock()
position_state = {}
latest_prices = {}
entry_history = {}
tp_orders = {}
tp_type = {}
INITIAL_BALANCE = Decimal("0")

app = Flask(__name__)

# =============================================================================
# 유틸리티
# =============================================================================

def log_debug(label, msg="", exc_info=False):
    """로그 출력"""
    if exc_info:
        logger.error(f"[{label}] {msg}", exc_info=True)
    else:
        logger.info(f"[{label}] {msg}")


def get_available_balance(show_log=False):
    """사용 가능 잔고 조회 (Unified/Futures)"""
    try:
        # Unified Account
        try:
            unified_account = unified_api.list_unified_accounts()
            if hasattr(unified_account, 'balances') and unified_account.balances:
                balances = unified_account.balances
                if isinstance(balances, dict) and "USDT" in balances:
                    usdt_data = balances["USDT"]
                    try:
                        if isinstance(usdt_data, dict):
                            available_str = str(usdt_data.get("available", "0"))
                        else:
                            available_str = str(getattr(usdt_data, "available", "0"))
                        usdt_balance = float(available_str)
                        if usdt_balance > 0:
                            if show_log:
                                log_debug("💰 잔고 (Unified)", f"{usdt_balance:.2f} USDT")
                            return usdt_balance
                    except:
                        pass
        except:
            pass
        
        # Futures Account
        try:
            account = api.list_futures_accounts(settle=SETTLE)
            if account:
                available = float(getattr(account, "available", "0"))
                if available > 0:
                    if show_log:
                        log_debug("💰 잔고 (Futures)", f"{available:.2f} USDT")
                    return available
        except:
            pass
        
        return 0.0
    except:
        return 0.0


def get_candles(symbol, interval="10s", limit=600):
    """캔들 데이터 조회"""
    try:
        candles = api.list_futures_candlesticks(SETTLE, contract=symbol, interval=interval, limit=limit)
        if not candles:
            return None
        
        df = pd.DataFrame([{
            'time': int(c.t),
            'open': float(c.o),
            'high': float(c.h),
            'low': float(c.l),
            'close': float(c.c),
            'volume': float(c.v)
        } for c in candles])
        
        return df
    except Exception as e:
        log_debug("❌ 캔들 조회 실패", str(e))
        return None


def calculate_obv_macd(symbol):
    """Shadow OBV MACD 계산"""
    try:
        df = get_candles(symbol, interval="10s", limit=600)
        if df is None or len(df) < 50:
            return Decimal("0")
        
        window_len = 28
        v_len = 14
        ma_len = 9
        slow_length = 26
        
        price_spread = df['high'] - df['low']
        price_spread_std = price_spread.rolling(window=window_len, min_periods=1).std().fillna(0)
        
        price_change = df['close'].diff().fillna(0)
        volume_signed = np.sign(price_change) * df['volume']
        v = volume_signed.cumsum()
        
        smooth = v.rolling(window=v_len, min_periods=1).mean()
        v_diff = v - smooth
        v_spread = v_diff.rolling(window=window_len, min_periods=1).std().fillna(1)
        v_spread = v_spread.replace(0, 1)
        
        shadow = (v_diff / v_spread) * price_spread_std
        
        out = pd.Series(index=df.index, dtype=float)
        for i in range(len(df)):
            if shadow.iloc[i] > 0:
                out.iloc[i] = df['high'].iloc[i] + shadow.iloc[i]
            else:
                out.iloc[i] = df['low'].iloc[i] + shadow.iloc[i]
        
        ma1 = out.ewm(span=ma_len, adjust=False).mean()
        ma2 = ma1.ewm(span=ma_len, adjust=False).mean()
        dema = 2 * ma1 - ma2
        
        slow_ma = df['close'].ewm(span=slow_length, adjust=False).mean()
        macd = dema - slow_ma
        
        final_value = macd.iloc[-1]
        
        if pd.isna(final_value) or np.isinf(final_value):
            return Decimal("0")
        
        # ⭐ 실제 값 그대로 반환 (x1000 안 함)
        return Decimal(str(round(float(final_value), 6)))  # 소수점 6자리
        
    except Exception as e:
        log_debug("❌ OBV MACD 오류", str(e), exc_info=True)
        return Decimal("0")


def calculate_grid_qty(current_price):
    """그리드 수량 계산 (OBV MACD 기반 0.2~0.8배)"""
    try:
        if INITIAL_BALANCE <= 0:
            return 1
        
        obv_macd = calculate_obv_macd(SYMBOL)
        
        # ⭐ x1000 해서 비교 (실제 값이 0.02면 20으로 비교)
        abs_val = abs(float(obv_macd * 1000))
        
        if abs_val < 5:
            leverage = Decimal("0.2")
        elif abs_val < 10:
            leverage = Decimal("0.22")
        elif abs_val < 20:
            leverage = Decimal("0.24")
        elif abs_val < 30:
            leverage = Decimal("0.26")
        elif abs_val < 40:
            leverage = Decimal("0.28")
        elif abs_val < 50:
            leverage = Decimal("0.3")
        elif abs_val < 60:
            leverage = Decimal("0.32")
        elif abs_val < 70:
            leverage = Decimal("0.34")
        elif abs_val < 80:
            leverage = Decimal("0.36")
        elif abs_val < 90:
            leverage = Decimal("0.38")
        elif abs_val < 100:
            leverage = Decimal("0.40")            
        else:
            leverage = Decimal("0.5")
        
        qty = int((INITIAL_BALANCE * leverage) / (current_price * CONTRACT_SIZE))
        return max(1, qty)
    except Exception as e:
        log_debug("❌ 그리드 수량 오류", str(e))
        return 1


def calculate_position_value(qty, price):
    """포지션 가치 계산"""
    return qty * price * CONTRACT_SIZE


def get_primary_direction():
    """주력 방향 판단 (포지션 가치 기반)"""
    try:
        with position_lock:
            pos = position_state.get(SYMBOL, {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            long_price = pos.get("long", {}).get("price", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            short_price = pos.get("short", {}).get("price", Decimal("0"))
            
            long_value = calculate_position_value(long_size, long_price)
            short_value = calculate_position_value(short_size, short_price)
            
            if long_value > short_value:
                return "long"
            elif short_value > long_value:
                return "short"
            else:
                obv_macd = calculate_obv_macd(SYMBOL)
                return "short" if obv_macd >= 0 else "long"
                    
    except Exception as e:
        log_debug("❌ 주력 방향 오류", str(e))
        return None

# =============================================================================
# 포지션 관리
# =============================================================================

def update_position_state(symbol):
    """포지션 상태 업데이트"""
    try:
        positions = api.list_positions(SETTLE)
        
        with position_lock:
            if symbol not in position_state:
                position_state[symbol] = {"long": {}, "short": {}}
            
            long_size = Decimal("0")
            long_price = Decimal("0")
            short_size = Decimal("0")
            short_price = Decimal("0")
            
            for p in positions:
                if p.contract != symbol:
                    continue
                    
                size = abs(Decimal(str(p.size)))
                entry_price = Decimal(str(p.entry_price)) if p.entry_price else Decimal("0")
                
                if p.size > 0:
                    long_size = size
                    long_price = entry_price
                elif p.size < 0:
                    short_size = size
                    short_price = entry_price
            
            position_state[symbol]["long"] = {"size": long_size, "price": long_price}
            position_state[symbol]["short"] = {"size": short_size, "price": short_price}
            
    except Exception as e:
        log_debug("❌ 포지션 업데이트 실패", str(e), exc_info=True)


def record_entry(symbol, side, price, qty):
    """진입 기록 저장"""
    if symbol not in entry_history:
        entry_history[symbol] = {"long": [], "short": []}
    
    entry_history[symbol][side].append({
        "price": Decimal(str(price)),
        "qty": Decimal(str(qty)),
        "timestamp": time.time()
    })
    
    log_debug("📝 진입 기록", f"{symbol}_{side} {qty}계약 @ {price:.4f}")

# =============================================================================
# 주문 관리
# =============================================================================

def cancel_grid_orders(symbol):
    """그리드 주문만 취소 (TP 유지)"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
        for order in orders:
            try:
                if not order.is_reduce_only:
                    api.cancel_futures_order(SETTLE, order.id)
            except:
                pass
    except:
        pass


def cancel_tp_orders(symbol, side):
    """TP 주문 취소"""
    try:
        if symbol in tp_orders and side in tp_orders[symbol]:
            for tp_order in tp_orders[symbol][side][:]:
                try:
                    api.cancel_futures_order(SETTLE, tp_order["order_id"])
                    tp_orders[symbol][side].remove(tp_order)
                except:
                    pass
    except:
        pass

# =============================================================================
# TP 관리
# =============================================================================

def place_average_tp_order(symbol, side, price, qty):
    """평단가 TP 지정가 주문"""
    try:
        if side == "long":
            tp_price = price * (Decimal("1") + TP_GAP_PCT)
            order_size = -int(qty)
        else:
            tp_price = price * (Decimal("1") - TP_GAP_PCT)
            order_size = int(qty)
        
        log_debug("🔍 TP 시도", f"{symbol}_{side} size:{order_size} price:{float(tp_price):.4f}")
        
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price=str(round(float(tp_price), 4)),
            tif="gtc",
            reduce_only=True
        )
        
        result = api.create_futures_order(SETTLE, order)
        
        log_debug("✅ TP 성공", f"주문 ID: {result.id}")
        
        if symbol not in tp_orders:
            tp_orders[symbol] = {"long": [], "short": []}
        
        tp_orders[symbol][side].append({
            "order_id": result.id,
            "tp_price": tp_price,
            "qty": qty,
            "type": "average"
        })
        
        log_debug("📌 평단 TP", f"{symbol}_{side} {qty}계약 TP:{float(tp_price):.4f}")
        
    except Exception as e:
        log_debug("❌ 평단 TP 실패", str(e), exc_info=True)  # ⭐ 상세 오류 출력


def place_individual_tp_orders(symbol, side, entries):
    """개별 진입별 TP 지정가 주문"""
    try:
        for entry in entries:
            entry_price = entry["price"]
            qty = entry["qty"]
            
            if side == "long":
                tp_price = entry_price * (Decimal("1") + TP_GAP_PCT)
                order_size = -int(qty)
            else:
                tp_price = entry_price * (Decimal("1") - TP_GAP_PCT)
                order_size = int(qty)
            
            order = FuturesOrder(
                contract=symbol,
                size=order_size,
                price=str(round(float(tp_price), 4)),
                tif="gtc",
                reduce_only=True
            )
            
            result = api.create_futures_order(SETTLE, order)
            
            if symbol not in tp_orders:
                tp_orders[symbol] = {"long": [], "short": []}
            
            tp_orders[symbol][side].append({
                "order_id": result.id,
                "entry_price": entry_price,
                "tp_price": tp_price,
                "qty": qty,
                "type": "individual"
            })
            
            log_debug("📌 개별 TP", 
                     f"{symbol}_{side} {qty}계약 진입:{float(entry_price):.4f} TP:{float(tp_price):.4f}")
            
            time.sleep(0.1)  # API Rate Limit
            
    except Exception as e:
        log_debug("❌ 개별 TP 실패", str(e))


def check_and_update_tp_mode(symbol, side):
    """임계값 체크 및 TP 모드 전환"""
    try:
        pos = position_state.get(symbol, {}).get(side, {})
        size = pos.get("size", Decimal("0"))
        price = pos.get("price", Decimal("0"))
        
        if size == 0:
            return
        
        position_value = calculate_position_value(size, price)
        threshold_value = INITIAL_BALANCE * THRESHOLD_RATIO
        
        current_type = tp_type.get(symbol, {}).get(side, "average")
        
        if position_value > threshold_value:
            # 임계값 초과 → 개별 TP
            if current_type != "individual":
                log_debug("⚠️ 임계값 초과", 
                         f"{symbol}_{side} {float(position_value):.2f} > {float(threshold_value):.2f}")
                
                cancel_tp_orders(symbol, side)
                
                entries = entry_history.get(symbol, {}).get(side, [])
                if entries:
                    place_individual_tp_orders(symbol, side, entries)
                
                if symbol not in tp_type:
                    tp_type[symbol] = {"long": "average", "short": "average"}
                tp_type[symbol][side] = "individual"
                
        else:
            # 임계값 미만 → 평단가 TP
            # ⭐⭐⭐ 수정: 조건 제거하고 무조건 등록!
            log_debug("✅ 평단가 TP", f"{symbol}_{side} {float(position_value):.2f} < {float(threshold_value):.2f}")
            
            cancel_tp_orders(symbol, side)
            place_average_tp_order(symbol, side, price, size)
            
            if symbol not in tp_type:
                tp_type[symbol] = {"long": "average", "short": "average"}
            tp_type[symbol][side] = "average"
                
    except Exception as e:
        log_debug("❌ TP 모드 체크 오류", str(e))


def refresh_tp_orders(symbol):
    """TP 주문 새로고침"""
    try:
        log_debug("🔄 TP 새로고침 시작", symbol)
        
        for side in ["long", "short"]:
            pos = position_state.get(symbol, {}).get(side, {})
            size = pos.get("size", Decimal("0"))
            price = pos.get("price", Decimal("0"))
            
            log_debug(f"🔍 포지션 체크", f"{side} size:{size} price:{price}")
            
            if size > 0:
                cancel_tp_orders(symbol, side)
                check_and_update_tp_mode(symbol, side)
            else:
                log_debug(f"⚠️ 포지션 없음", f"{side} size=0")
                
    except Exception as e:
        log_debug("❌ TP 새로고침 오류", str(e), exc_info=True)


# =============================================================================
# 그리드 관리
# =============================================================================

def initialize_grid(base_price=None):
    """그리드 초기화 (지정가 주문)"""
    try:
        if base_price is None:
            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
            if not ticker:
                return
            base_price = Decimal(str(ticker[0].last))
        
        obv_macd = calculate_obv_macd(SYMBOL)
        
        upper_price = float(base_price * (Decimal("1") + GRID_GAP_PCT))
        lower_price = float(base_price * (Decimal("1") - GRID_GAP_PCT))
        
        # OBV 기반 주력 방향 결정
        if obv_macd >= 0:
            short_qty = calculate_grid_qty(base_price)
            long_qty = max(1, int((INITIAL_BALANCE * HEDGE_RATIO) / (base_price * CONTRACT_SIZE)))
        else:
            long_qty = calculate_grid_qty(base_price)
            short_qty = max(1, int((INITIAL_BALANCE * HEDGE_RATIO) / (base_price * CONTRACT_SIZE)))
        
        # 위쪽 숏 주문
        try:
            order = FuturesOrder(
                contract=SYMBOL,
                size=-short_qty,
                price=str(round(upper_price, 4)),
                tif="gtc"
            )
            api.create_futures_order(SETTLE, order)
        except Exception as e:
            log_debug("❌ 숏 주문 실패", str(e))
        
        # 아래쪽 롱 주문
        try:
            order = FuturesOrder(
                contract=SYMBOL,
                size=long_qty,
                price=str(round(lower_price, 4)),
                tif="gtc"
            )
            api.create_futures_order(SETTLE, order)
        except Exception as e:
            log_debug("❌ 롱 주문 실패", str(e))
        
        # ⭐ 로그 출력 시 x1000 표시
        log_debug("🎯 그리드 생성", 
                 f"기준:{base_price:.4f} 위:{upper_price:.4f} 아래:{lower_price:.4f} | OBV:{float(obv_macd * 1000):.2f}")
        
    except Exception as e:
        log_debug("❌ 그리드 생성 실패", str(e), exc_info=True)


# =============================================================================
# 헤징 관리
# =============================================================================

def place_hedge_order(symbol, side, current_price):
    """헤징 시장가 주문 (즉시 체결)"""
    try:
        hedge_qty = max(1, int((INITIAL_BALANCE * HEDGE_RATIO) / (current_price * CONTRACT_SIZE)))
        
        if side == "short":
            order_size = -hedge_qty
        else:
            order_size = hedge_qty
        
        # ⭐ 시장가 주문
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price="0",  # 시장가
            tif="ioc"
        )
        
        result = api.create_futures_order(SETTLE, order)
        
        log_debug("📌 헤징 주문 (시장가)", f"{symbol} {side} {hedge_qty}계약")
        
        return result.id
        
    except Exception as e:
        log_debug("❌ 헤징 주문 실패", str(e))
        return None


# =============================================================================
# 체결 모니터링
# =============================================================================

def fill_monitor():
    """체결 감지 (그리드 재생성 없음!)"""
    prev_long_size = Decimal("0")
    prev_short_size = Decimal("0")
    last_action_time = 0
    
    while True:
        time.sleep(2)
        update_position_state(SYMBOL)
        
        with position_lock:
            pos = position_state.get(SYMBOL, {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            long_price = pos.get("long", {}).get("price", Decimal("0"))
            short_price = pos.get("short", {}).get("price", Decimal("0"))
            
            now = time.time()
            
            # 현재가 조회
            try:
                ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                current_price = Decimal(str(ticker[0].last)) if ticker else Decimal("0")
            except:
                current_price = Decimal("0")
            
            # 롱 체결 감지
            if long_size > prev_long_size and now - last_action_time >= 5:
                added_long = long_size - prev_long_size
                
                log_debug("📊 롱 체결", f"{added_long}계약 @ {long_price:.4f} (총 {long_size}계약)")
                
                # 진입 기록
                record_entry(SYMBOL, "long", long_price, added_long)
                
                # 그리드 취소
                cancel_grid_orders(SYMBOL)
                
                # TP 새로고침
                refresh_tp_orders(SYMBOL)
                
                # 숏 헤징
                if current_price > 0:
                    place_hedge_order(SYMBOL, "short", current_price)
                
                # 헤징 체결 대기
                time.sleep(1)
                update_position_state(SYMBOL)
                pos = position_state.get(SYMBOL, {})
                prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                
                # TP 다시 새로고침 (헤징 포함)
                refresh_tp_orders(SYMBOL)
                
                last_action_time = now
            
            # 숏 체결 감지
            elif short_size > prev_short_size and now - last_action_time >= 5:
                added_short = short_size - prev_short_size
                
                log_debug("📊 숏 체결", f"{added_short}계약 @ {short_price:.4f} (총 {short_size}계약)")
                
                # 진입 기록
                record_entry(SYMBOL, "short", short_price, added_short)
                
                # 그리드 취소
                cancel_grid_orders(SYMBOL)
                
                # TP 새로고침
                refresh_tp_orders(SYMBOL)
                
                # 롱 헤징
                if current_price > 0:
                    place_hedge_order(SYMBOL, "long", current_price)
                
                # 헤징 체결 대기
                time.sleep(1)
                update_position_state(SYMBOL)
                pos = position_state.get(SYMBOL, {})
                prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                
                # TP 다시 새로고침 (헤징 포함)
                refresh_tp_orders(SYMBOL)
                
                last_action_time = now


# =============================================================================
# TP 체결 모니터링
# =============================================================================

def tp_monitor():
    """TP 체결 감지 및 그리드 재생성 (변화 감지 방식)"""
    prev_long_size = Decimal("-1")  # 초기값 -1
    prev_short_size = Decimal("-1")
    
    while True:
        time.sleep(3)
        
        try:
            update_position_state(SYMBOL)
            
            with position_lock:
                pos = position_state.get(SYMBOL, {})
                long_size = pos.get("long", {}).get("size", Decimal("0"))
                short_size = pos.get("short", {}).get("size", Decimal("0"))
                
                # 롱 포지션이 0이 "되었을 때"만
                if long_size == 0 and prev_long_size > 0:
                    long_type = tp_type.get(SYMBOL, {}).get("long", "average")
                    
                    # 개별 TP는 그리드 재생성 안 함
                    if long_type == "individual":
                        prev_long_size = long_size
                        continue
                    
                    log_debug("✅ 롱 TP 청산", "그리드 재생성!")
                    
                    # 진입 기록 초기화
                    if SYMBOL in entry_history:
                        entry_history[SYMBOL]["long"] = []
                    if SYMBOL in tp_type:
                        tp_type[SYMBOL]["long"] = "average"
                    
                    # ⭐⭐⭐ 기존 그리드 취소
                    cancel_grid_orders(SYMBOL)
                    time.sleep(1)
                    
                    # 그리드 재생성
                    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                    if ticker:
                        current_price = Decimal(str(ticker[0].last))
                        initialize_grid(current_price)
                        
                        # ⭐⭐⭐ TP 새로고침 추가
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                
                # 숏 포지션이 0이 "되었을 때"만
                elif short_size == 0 and prev_short_size > 0:
                    short_type = tp_type.get(SYMBOL, {}).get("short", "average")
                    
                    # 개별 TP는 그리드 재생성 안 함
                    if short_type == "individual":
                        prev_short_size = short_size
                        continue
                    
                    log_debug("✅ 숏 TP 청산", "그리드 재생성!")
                    
                    # 진입 기록 초기화
                    if SYMBOL in entry_history:
                        entry_history[SYMBOL]["short"] = []
                    if SYMBOL in tp_type:
                        tp_type[SYMBOL]["short"] = "average"
                    
                    # ⭐⭐⭐ 기존 그리드 취소
                    cancel_grid_orders(SYMBOL)
                    time.sleep(1)
                    
                    # 그리드 재생성
                    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                    if ticker:
                        current_price = Decimal(str(ticker[0].last))
                        initialize_grid(current_price)
                        
                        # ⭐⭐⭐ TP 새로고침 추가
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        # ⭐⭐⭐ 한 번 더 확인!
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                
                # 상태 저장
                prev_long_size = long_size
                prev_short_size = short_size
                
        except Exception as e:
            log_debug("❌ TP 모니터 오류", str(e), exc_info=True)


# =============================================================================
# WebSocket 가격 모니터링
# =============================================================================

async def price_monitor():
    """가격 모니터링 (WebSocket)"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                subscribe_msg = {
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": [SYMBOL]
                }
                await ws.send(json.dumps(subscribe_msg))
                log_debug("🔗 WebSocket 연결", SYMBOL)
                
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    
                    if data.get("event") == "update" and data.get("channel") == "futures.tickers":
                        result = data.get("result")
                        if result and isinstance(result, dict):
                            price = Decimal(str(result.get("last", "0")))
                            if price > 0:
                                latest_prices[SYMBOL] = price
                    
        except Exception as e:
            log_debug("❌ WebSocket 오류", str(e))
            await asyncio.sleep(5)

# =============================================================================
# 웹 서버
# =============================================================================

@app.route("/ping", methods=["GET", "POST"])
def ping():
    """Health Check"""
    return jsonify({"status": "ok", "time": time.time()})

# =============================================================================
# 메인
# =============================================================================

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "v16.3-FINAL")
    
    # 초기 자본금 설정
    INITIAL_BALANCE = Decimal(str(get_available_balance(show_log=True)))
    log_debug("💰 초기 잔고", f"{INITIAL_BALANCE:.2f} USDT")
    log_debug("🎯 임계값", f"{float(INITIAL_BALANCE * THRESHOLD_RATIO):.2f} USDT ({int(THRESHOLD_RATIO)}배)")
    
    # 초기화
    entry_history[SYMBOL] = {"long": [], "short": []}
    tp_orders[SYMBOL] = {"long": [], "short": []}
    tp_type[SYMBOL] = {"long": "average", "short": "average"}
    
    # ⭐ OBV MACD 확인 (x1000 표시)
    obv_macd_val = calculate_obv_macd(SYMBOL)
    log_debug("📊 Shadow OBV MACD", f"{SYMBOL}: {float(obv_macd_val * 1000):.2f}")
    
    # 초기 그리드 생성
    initialize_grid()
    
    # 스레드 시작
    threading.Thread(target=fill_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    # 웹 서버 시작
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
