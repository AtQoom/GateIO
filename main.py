#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ONDO 역방향 그리드 매매 시스템 v19.0-ADVANCED
- 복리 자동화: 1시간마다 실제 잔고 업데이트
- 환경변수 기반 설정 (속도/안정성 극대화)
- 수량 계산: 레버리지 1배 기준
- OBV MACD 가중 수량 (0.10~0.35)
- 그리드/TP 간격 0.12%
- 헤징 0.1배
- 임계값 1배
- ⭐ 임계값 초과 후: 역방향 주력 30%, 주력 개별 TP 시 역방향 20% 동반 청산
- ⭐ 로그 최적화: 중요한 이벤트만 출력
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

# ✅ 버전 호환 Exception import
try:
    from gate_api.exceptions import ApiException as GateApiException
except ImportError:
    from gate_api import ApiException as GateApiException
import websockets
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# 환경변수 기반 설정
# =============================================================================

SETTLE = "usdt"
SYMBOL = "ONDO_USDT"
CONTRACT_SIZE = Decimal("1")
BASE_QTY = Decimal("0.2")

# ⭐ 환경변수로 모든 설정 관리
GRID_GAP_PCT = Decimal(os.environ.get("GRID_GAP_PCT", "0.12")) / Decimal("100")
TP_GAP_PCT = Decimal(os.environ.get("TP_GAP_PCT", "0.12")) / Decimal("100")
HEDGE_RATIO = Decimal(os.environ.get("HEDGE_RATIO", "0.1"))
THRESHOLD_RATIO = Decimal(os.environ.get("THRESHOLD_RATIO", "0.8"))
BALANCE_UPDATE_INTERVAL = int(os.environ.get("BALANCE_UPDATE_INTERVAL", "3600"))

# ⭐⭐⭐ 새로운 설정
COUNTER_POSITION_RATIO = Decimal("0.30")
COUNTER_CLOSE_RATIO = Decimal("0.20")

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

# =============================================================================
# 전역 변수
# =============================================================================

# ⚡⚡⚡ 중복 그리드 생성 방지
grid_generation_lock = threading.RLock()
last_grid_generation_time = 0

# ⭐ 복리를 위한 전역 변수
INITIAL_BALANCE = Decimal("0")
last_balance_update = 0
balance_lock = threading.RLock()

# 전역 변수
position_lock = threading.RLock()
position_state = {}
latest_prices = {}
entry_history = {}
tp_orders = {"long": [], "short": []}  # TP 주문 추적
tp_type = {}  # "average" 또는 "individual"
threshold_exceeded_time = 0  # 임계값 초과 시점 기록
post_threshold_entries = {"long": [], "short": []}  # 초과 후 진입 내역

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


def get_total_balance_from_api():
    """API에서 실제 총 자산 조회"""
    try:
        # Unified Account
        try:
            unified_account = unified_api.list_unified_accounts()
            if hasattr(unified_account, 'balances') and unified_account.balances:
                balances = unified_account.balances
                if isinstance(balances, dict) and "USDT" in balances:
                    usdt_data = balances["USDT"]
                    
                    if isinstance(usdt_data, dict):
                        available = float(usdt_data.get("available", "0"))
                        freeze = float(usdt_data.get("freeze", "0"))
                        borrowed = float(usdt_data.get("borrowed", "0"))
                        total = available + freeze - borrowed
                    else:
                        available = float(getattr(usdt_data, "available", "0"))
                        freeze = float(getattr(usdt_data, "freeze", "0"))
                        borrowed = float(getattr(usdt_data, "borrowed", "0"))
                        total = available + freeze - borrowed
                    
                    if total > 0:
                        return total
        except Exception as e:
            log_debug("⚠️ Unified Account 조회 실패", str(e))
        
        # Futures Account (백업)
        try:
            account = api.list_futures_accounts(settle=SETTLE)
            if account:
                available = float(getattr(account, "available", "0"))
                unrealized_pnl = 0
                if hasattr(account, "unrealized_pnl"):
                    unrealized_pnl = float(getattr(account, "unrealized_pnl", "0"))
                
                total = available + unrealized_pnl
                if total > 0:
                    return total
        except Exception as e:
            log_debug("⚠️ Futures Account 조회 실패", str(e))
        
        return 0.0
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return 0.0


def update_initial_balance(force=False):
    """복리를 위한 자본금 업데이트 (주기적)"""
    global INITIAL_BALANCE, last_balance_update
    
    now = time.time()
    
    # 강제 업데이트 또는 주기 도래 시
    if force or (now - last_balance_update >= BALANCE_UPDATE_INTERVAL):
        with balance_lock:
            try:
                new_balance = get_total_balance_from_api()
                
                if new_balance > 0:
                    old_balance = INITIAL_BALANCE
                    INITIAL_BALANCE = Decimal(str(new_balance))
                    last_balance_update = now
                    
                    if old_balance > 0:
                        change_pct = ((new_balance - float(old_balance)) / float(old_balance)) * 100
                        log_debug("💰 복리 자본금 업데이트", 
                                 f"{float(old_balance):.2f} → {new_balance:.2f} USDT ({change_pct:+.2f}%)")
                    else:
                        log_debug("💰 초기 자본금 설정", f"{new_balance:.2f} USDT")
                    
                    return True
            except Exception as e:
                log_debug("❌ 자본금 업데이트 실패", str(e))
                return False
    
    return False


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
        
        return Decimal(str(round(float(final_value), 6)))
        
    except Exception as e:
        log_debug("❌ OBV MACD 오류", str(e), exc_info=True)
        return Decimal("0")


def calculate_grid_qty(current_price):
    """그리드 수량 계산 (OBV MACD 가중 0.10~0.35, 레버리지 1배)"""
    try:
        if current_price is None or current_price <= 0:
            log_debug("❌ 수량 계산 오류", "가격 정보 없음")
            return int(Decimal("10"))
        
        with balance_lock:
            current_balance = INITIAL_BALANCE
        
        obv_macd_value = calculate_obv_macd(SYMBOL)
        abs_val = abs(obv_macd_value * 1000)
        
        if abs_val < 5:
            weight = Decimal("0.10")
        elif abs_val < 10:
            weight = Decimal("0.11")
        elif abs_val < 15:
            weight = Decimal("0.12")
        elif abs_val < 20:
            weight = Decimal("0.13")
        elif abs_val < 30:
            weight = Decimal("0.15")
        elif abs_val < 40:
            weight = Decimal("0.17")
        elif abs_val < 50:
            weight = Decimal("0.20")
        elif abs_val < 70:
            weight = Decimal("0.23")
        elif abs_val < 100:
            weight = Decimal("0.27")
        elif abs_val < 150:
            weight = Decimal("0.30")
        else:
            weight = Decimal("0.35")
        
        target_value = current_balance * weight
        quantity = target_value / current_price
        qty = int(quantity / CONTRACT_SIZE) * CONTRACT_SIZE
        
        # ✅ 로그 간소화: 주요 값만 출력
        log_debug("🔢 수량 계산", f"OBV:{obv_macd_value:.5f} → {qty}계약")
        return max(qty, CONTRACT_SIZE)
    except Exception as e:
        log_debug("❌ 수량 계산 오류", str(e), exc_info=True)
        return int(Decimal("10"))


def place_limit_order(symbol, side, price, size, reduce_only=False):
    """지정가 주문 - Gate.io API 완벽 호환"""
    try:
        # ⚡⚡⚡ reduce_only일 때 포지션 체크!
        if reduce_only:
            with position_lock:
                pos = position_state.get(symbol, {})
                
                if side == "long":  # 숏 청산
                    current_short = pos.get("short", {}).get("size", Decimal("0"))
                    if current_short == 0:
                        log_debug("⚠️ TP 스킵", "숏 포지션 없음")
                        return None
                    size = min(int(size), int(current_short))  # 최대 수량 제한!
                
                else:  # side == "short" → 롱 청산
                    current_long = pos.get("long", {}).get("size", Decimal("0"))
                    if current_long == 0:
                        log_debug("⚠️ TP 스킵", "롱 포지션 없음")
                        return None
                    size = min(int(size), int(current_long))  # 최대 수량 제한!
        
        # size 계산
        base_size = max(int(size), int(CONTRACT_SIZE))
        
        if side == "long":
            order_size = base_size  # 양수
        else:  # short
            order_size = -base_size  # 음수
        
        # 주문 생성
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price=str(round(float(price), 4)),
            tif='gtc',
            reduce_only=reduce_only
        )
        
        result = api.create_futures_order(SETTLE, order)
        return result
        
    except GateApiException as e:
        log_debug(f"❌ {side.upper()} 주문 실패", str(e))
        return None
    except Exception as e:
        log_debug(f"❌ {side.upper()} 오류", str(e), exc_info=True)
        return None


# =============================================================================
# 헤징 로직 수정 (시장가 주문 - price 추가)
# =============================================================================

def place_hedge_order(symbol, side, price):
    """일반 헤징 (0.1배) - 시장가 주문"""
    try:
        with position_lock:
            pos = position_state.get(symbol, {})
            
            # 진입한 포지션 수량 확인
            if side == "long":
                filled_size = pos.get("long", {}).get("size", Decimal("0"))
            else:
                filled_size = pos.get("short", {}).get("size", Decimal("0"))
        
        if filled_size == 0:
            return None
        
        # 헤징 수량: 0.1배
        hedge_qty = int(filled_size * HEDGE_RATIO)
        
        if hedge_qty < 1:
            hedge_qty = 1
        
        # 역방향 헤징
        hedge_side = "short" if side == "long" else "long"
        
        # ⚡⚡⚡ 현재가 조회
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker:
            log_debug("❌ 헤징 실패", "현재가 조회 실패")
            return None
        
        current_price = Decimal(str(ticker[0].last))
        
        # ⚡⚡⚡ 시장가 주문 (IOC) - price 필수!
        if hedge_side == "long":
            order_size = hedge_qty  # 양수
        else:
            order_size = -hedge_qty  # 음수
        
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price=str(round(float(current_price), 4)),  # ⚡ price 필수!
            tif='ioc',  # 시장가 주문
            reduce_only=False
        )
        
        result = api.create_futures_order(SETTLE, order)
        
        if result:
            log_debug(f"✅ 헤징", f"{hedge_side.upper()} {hedge_qty}개 (0.1배 시장가)")
        
        return result
        
    except Exception as e:
        log_debug("❌ 헤징 오류", str(e), exc_info=True)
        return None


def place_hedge_order_with_counter_check(symbol, side, price):
    """
    ⚡ 비주력 그리드 체결 시: 주력의 10%와 기본 헤지 중 큰 수량 (시장가)
    ⚡ 일반 그리드 체결 시: 0.1배 헤징 (시장가)
    """
    try:
        with balance_lock:
            current_balance = INITIAL_BALANCE
        
        threshold = current_balance * THRESHOLD_RATIO
        
        with position_lock:
            pos = position_state.get(symbol, {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            long_price = pos.get("long", {}).get("price", Decimal("0"))
            short_price = pos.get("short", {}).get("price", Decimal("0"))
        
        long_value = long_size * long_price if long_price > 0 else Decimal("0")
        short_value = short_size * short_price if short_price > 0 else Decimal("0")
        
        # ⚡ 주력 판단
        if side == "short":  # 숏 진입 = 롱이 주력일 가능성
            main_size = long_size
            main_value = long_value
            main_side = "long"
            is_counter_entry = main_value >= threshold
        else:  # 롱 진입 = 숏이 주력일 가능성
            main_size = short_size
            main_value = short_value
            main_side = "short"
            is_counter_entry = main_value >= threshold
        
        # ⚡ 비주력 그리드 체결인 경우
        if is_counter_entry and main_size > 0:
            # max(주력 10%, 기본 헤지 0.1배)
            counter_hedge_qty = int(main_size * Decimal("0.10"))
            base_hedge_qty = int(main_size * HEDGE_RATIO)
            hedge_qty = max(counter_hedge_qty, base_hedge_qty)
            
            if hedge_qty < 1:
                hedge_qty = 1
            
            # ⚡⚡⚡ 현재가 조회
            ticker = api.list_futures_tickers(SETTLE, contract=symbol)
            if not ticker:
                log_debug("❌ 비주력 헤징 실패", "현재가 조회 실패")
                return None
            
            current_price = Decimal(str(ticker[0].last))
            
            # ⚡⚡⚡ 같은 방향으로 헤징 (주력과 같은 방향 추가 진입) - 시장가
            if main_side == "long":
                order_size = hedge_qty  # 양수
            else:
                order_size = -hedge_qty  # 음수
            
            order = FuturesOrder(
                contract=symbol,
                size=order_size,
                price=str(round(float(current_price), 4)),  # ⚡ price 필수!
                tif='ioc',  # 시장가 주문
                reduce_only=False
            )
            
            result = api.create_futures_order(SETTLE, order)
            
            if result:
                log_debug(f"✅ 비주력 헤징", 
                         f"{main_side.upper()} {hedge_qty}개 (max(10%, 헤지) 시장가)")
            return result
        
        # ⚡ 일반 헤징 (0.1배 시장가)
        return place_hedge_order(symbol, side, price)
        
    except Exception as e:
        log_debug("❌ 헤징 오류", str(e), exc_info=True)
        return None
        

# =============================================================================
# 포지션 관리
# =============================================================================

def update_position_state(symbol, retry=5, show_log=False):
    """포지션 상태 업데이트"""
    for attempt in range(retry):
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
                
                # ✅ show_log=True일 때만 출력
                if show_log:
                    log_debug("🔍 포지션 최종", f"롱:{long_size}@{long_price:.4f} 숏:{short_size}@{short_price:.4f}")
                
                return True
                
        except Exception as e:
            if attempt < retry - 1:
                time.sleep(0.5)
            else:
                log_debug("❌ 포지션 업데이트 실패", str(e), exc_info=True)
                return False


def record_entry(symbol, side, price, qty):
    """진입 기록 - 임계값 초과 후 추적"""
    global threshold_exceeded_time, post_threshold_entries
    
    if symbol not in entry_history:
        entry_history[symbol] = {"long": [], "short": []}
    
    entry_history[symbol][side].append({
        "price": Decimal(str(price)),
        "qty": Decimal(str(qty)),
        "timestamp": time.time()
    })
    
    # 임계값 확인
    with balance_lock:
        current_balance = INITIAL_BALANCE
    
    threshold = current_balance * THRESHOLD_RATIO
    
    with position_lock:
        pos = position_state.get(symbol, {})
        long_size = pos.get("long", {}).get("size", Decimal("0"))
        short_size = pos.get("short", {}).get("size", Decimal("0"))
        long_price = pos.get("long", {}).get("price", Decimal("0"))
        short_price = pos.get("short", {}).get("price", Decimal("0"))
    
    long_value = long_size * long_price if long_price > 0 else Decimal("0")
    short_value = short_size * short_price if short_price > 0 else Decimal("0")
    
    # 임계값 초과 시점 기록
    if (long_value >= threshold or short_value >= threshold) and threshold_exceeded_time == 0:
        threshold_exceeded_time = time.time()
        log_debug("⚡ 임계값 초과", f"시각: {threshold_exceeded_time}")
    
    # 임계값 초과 후 진입 기록
    if threshold_exceeded_time > 0:
        if symbol not in post_threshold_entries:
            post_threshold_entries[symbol] = {"long": [], "short": []}
        
        post_threshold_entries[symbol][side].append({
            "price": Decimal(str(price)),
            "qty": Decimal(str(qty)),
            "timestamp": time.time()
        })
        log_debug("📝 초과 후 진입", f"{side.upper()} {qty}개 @{price:.4f}")


# =============================================================================
# 주문 관리
# =============================================================================

def cancel_grid_orders(symbol):
    """그리드 주문만 취소 (TP 유지)"""
    for retry in range(2):
        try:
            orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
            cancelled_count = 0
            
            for order in orders:
                try:
                    if not order.is_reduce_only:
                        api.cancel_futures_order(SETTLE, order.id)
                        cancelled_count += 1
                        time.sleep(0.1)
                except:
                    pass
            
            # ✅ 로그 간소화
            if cancelled_count > 0:
                log_debug("✅ 그리드 취소", f"{cancelled_count}개")
            break
            
        except Exception as e:
            if retry < 1:
                time.sleep(0.3)


def cancel_tp_orders(symbol):  # ← side 파라미터 제거!
    """TP 주문 취소"""
    try:
        cancelled_count = 0
        orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
        
        for order in orders:
            if not order.is_reduce_only:
                continue
            
            # 롱 TP (숏 주문)
            if order.size < 0:
                for retry in range(3):
                    try:
                        api.cancel_futures_order(SETTLE, order.id)
                        cancelled_count += 1
                        break
                    except:
                        if retry < 2:
                            time.sleep(0.3)
            
            # 숏 TP (롱 주문)
            elif order.size > 0:
                for retry in range(3):
                    try:
                        api.cancel_futures_order(SETTLE, order.id)
                        cancelled_count += 1
                        break
                    except:
                        if retry < 2:
                            time.sleep(0.3)
        
        if cancelled_count > 0:
            log_debug("✅ TP 취소", f"{cancelled_count}개")
        
        if symbol in tp_orders:
            tp_orders[symbol]["long"] = []
            tp_orders[symbol]["short"] = []
            
    except Exception as e:
        log_debug("❌ TP 취소 오류", str(e), exc_info=True)


def cancel_all_orders(symbol):
    """모든 주문 취소"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
        for order in orders:
            try:
                api.cancel_futures_order(SETTLE, order.id)
                time.sleep(0.1)
            except:
                pass
    except:
        pass


# =============================================================================
# TP 관리
# =============================================================================

def place_average_tp_order(symbol, side, price, qty, retry=3):
    """평단가 TP 지정가 주문 - 중복 방지"""
    for attempt in range(retry):
        try:
            # ⚡⚡⚡ 기존 TP 주문 확인
            try:
                orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
                
                for order in orders:
                    if not order.is_reduce_only:
                        continue
                    
                    # 같은 방향의 TP가 이미 있으면 스킵
                    if side == "long" and order.size < 0:  # 롱 TP = 숏 주문
                        log_debug("⚠️ TP 스킵", f"롱 TP 이미 존재 (order_id: {order.id})")
                        return False
                    elif side == "short" and order.size > 0:  # 숏 TP = 롱 주문
                        log_debug("⚠️ TP 스킵", f"숏 TP 이미 존재 (order_id: {order.id})")
                        return False
            except:
                pass
            
            # ⚡⚡⚡ 포지션 수량 재확인
            with position_lock:
                pos = position_state.get(symbol, {})
                
                if side == "long":
                    current_size = pos.get("long", {}).get("size", Decimal("0"))
                else:
                    current_size = pos.get("short", {}).get("size", Decimal("0"))
            
            if current_size == 0:
                log_debug("⚠️ TP 스킵", f"{side} 포지션 없음")
                return False
            
            # 최대 수량 제한
            qty = min(int(qty), int(current_size))
            
            if side == "long":
                tp_price = price * (Decimal("1") + TP_GAP_PCT)
                order_size = -int(qty)
            else:
                tp_price = price * (Decimal("1") - TP_GAP_PCT)
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
                "tp_price": tp_price,
                "qty": Decimal(str(qty)),
                "type": "average"
            })
            
            # ✅ 로그 간소화
            log_debug("✅ TP 생성", f"{side} {qty}개 @{float(tp_price):.4f}")
            return True
            
        except GateApiException as e:
            error_msg = str(e)
            if "REDUCE_ONLY_FAIL" in error_msg:
                log_debug("⚠️ TP 중복", f"{side} TP 이미 존재")
                return False
            elif attempt < retry - 1:
                time.sleep(0.5)
            else:
                log_debug("❌ TP 실패", str(e), exc_info=True)
                return False
        except Exception as e:
            if attempt < retry - 1:
                time.sleep(0.5)
            else:
                log_debug("❌ TP 실패", str(e), exc_info=True)
                return False

def close_counter_position_on_main_tp(symbol, main_side, main_tp_qty):
    """
    ⭐ 주력 TP 체결 시 역방향 20% 동반 청산
    ⭐ 조건: 양쪽 포지션 모두 임계값 초과 상태여야 함!
    """
    try:
        counter_side = "long" if main_side == "short" else "short"
        
        # ⚡⚡⚡ 포지션 & 임계값 확인!
        with balance_lock:
            current_balance = INITIAL_BALANCE
        
        threshold = current_balance * THRESHOLD_RATIO
        
        with position_lock:
            pos = position_state.get(symbol, {})
            
            # 주력 포지션 확인
            main_size = pos.get(main_side, {}).get("size", Decimal("0"))
            main_entry = pos.get(main_side, {}).get("price", Decimal("0"))
            main_value = main_size * main_entry if main_entry > 0 else Decimal("0")
            
            # 역방향 포지션 확인
            counter_size = pos.get(counter_side, {}).get("size", Decimal("0"))
            counter_entry = pos.get(counter_side, {}).get("price", Decimal("0"))
            counter_value = counter_size * counter_entry if counter_entry > 0 else Decimal("0")
        
        # ⚡⚡⚡ 양쪽 모두 임계값 초과 체크!
        if main_value < threshold:
            log_debug("⚠️ 동반 청산 스킵", 
                     f"주력 미달 ({float(main_value):.1f} < {float(threshold):.1f})")
            return
        
        if counter_value < threshold:
            log_debug("⚠️ 동반 청산 스킵", 
                     f"역방향 미달 ({float(counter_value):.1f} < {float(threshold):.1f})")
            return
        
        # ⚡⚡⚡ 청산 수량 계산 (20%)
        counter_close_qty = int(main_tp_qty * COUNTER_CLOSE_RATIO)
        
        if counter_close_qty < 1:
            return
        
        if counter_size == 0:
            return
        
        # 최대 수량 제한
        counter_close_qty = min(counter_close_qty, int(counter_size))
        
        # ⚡⚡⚡ IOC 주문으로 즉시 청산!
        if counter_side == "long":
            order_size = -counter_close_qty  # 롱 청산 = 숏 주문
        else:
            order_size = counter_close_qty   # 숏 청산 = 롱 주문
        
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            tif='ioc',
            reduce_only=True
        )
        
        result = api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 동반 청산 20%", 
                 f"{counter_side.upper()} {counter_close_qty}개 (주력 TP {main_tp_qty}개)")
        
    except Exception as e:
        log_debug("❌ 동반 청산 오류", str(e), exc_info=True)


def refresh_tp_orders(symbol):
    """TP 재생성 - 임계값 확인하여 평단가 TP 또는 개별 TP"""
    global threshold_exceeded_time, tp_type
    
    try:
        cancel_tp_orders(symbol)
        time.sleep(0.5)
        
        with balance_lock:
            current_balance = INITIAL_BALANCE
        
        threshold = current_balance * THRESHOLD_RATIO
        
        with position_lock:
            pos = position_state.get(symbol, {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            long_entry = pos.get("long", {}).get("price", Decimal("0"))
            short_entry = pos.get("short", {}).get("price", Decimal("0"))
        
        long_value = long_size * long_entry if long_entry > 0 else Decimal("0")
        short_value = short_size * short_entry if short_entry > 0 else Decimal("0")
        
        log_debug("TP 재생성", f"롱{long_size}@{long_entry:.4f} 숏{short_size}@{short_entry:.4f}")
        
        # ============================================================
        # 롱 주력 + 임계값 초과
        # ============================================================
        if long_value >= threshold and short_value < threshold:
            log_debug("🔵 롱 주력 TP", "개별 TP 생성")
            tp_type[symbol] = "individual"
            
            # 비주력 숏: 평단가 TP
            if short_size > 0 and short_entry > 0:
                tp_price = short_entry * (Decimal("1") - TP_GAP_PCT)
                tp_price = round(tp_price, 4)
                place_average_tp_order(symbol, "short", short_entry, int(short_size))
            
            # 주력 롱: 개별 진입가 TP
            # ⚡⚡⚡ 수정: post_threshold_entries.get("long", []) 
            #        → post_threshold_entries.get(symbol, {}).get("long", [])
            if long_size > 0 and len(post_threshold_entries.get(symbol, {}).get("long", [])) > 0:
                for entry_info in post_threshold_entries[symbol]["long"]:
                    entry_price = entry_info["price"]
                    entry_qty = int(entry_info["qty"])
                    
                    tp_price = entry_price * (Decimal("1") + TP_GAP_PCT)
                    tp_price = round(tp_price, 4)
                    
                    try:
                        order_size = -entry_qty
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
                        tp_orders[symbol]["long"].append({
                            "order_id": result.id,
                            "tp_price": tp_price,
                            "qty": Decimal(str(entry_qty)),
                            "type": "individual"
                        })
                        log_debug("✅ 개별 TP", f"롱 {entry_qty}개 @{tp_price:.4f}")
                    except:
                        pass
            
            # 평단가 TP도 추가 (안전장치)
            elif long_size > 0 and long_entry > 0:
                place_average_tp_order(symbol, "long", long_entry, int(long_size))
            
            return
        
        # ============================================================
        # 숏 주력 + 임계값 초과
        # ============================================================
        elif short_value >= threshold and long_value < threshold:
            log_debug("🔴 숏 주력 TP", "개별 TP 생성")
            tp_type[symbol] = "individual"
            
            # 비주력 롱: 평단가 TP
            if long_size > 0 and long_entry > 0:
                tp_price = long_entry * (Decimal("1") + TP_GAP_PCT)
                tp_price = round(tp_price, 4)
                place_average_tp_order(symbol, "long", long_entry, int(long_size))
            
            # 주력 숏: 개별 진입가 TP
            # ⚡⚡⚡ 수정: post_threshold_entries.get("short", [])
            #        → post_threshold_entries.get(symbol, {}).get("short", [])
            if short_size > 0 and len(post_threshold_entries.get(symbol, {}).get("short", [])) > 0:
                for entry_info in post_threshold_entries[symbol]["short"]:
                    entry_price = entry_info["price"]
                    entry_qty = int(entry_info["qty"])
                    
                    tp_price = entry_price * (Decimal("1") - TP_GAP_PCT)
                    tp_price = round(tp_price, 4)
                    
                    try:
                        order_size = entry_qty
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
                        tp_orders[symbol]["short"].append({
                            "order_id": result.id,
                            "tp_price": tp_price,
                            "qty": Decimal(str(entry_qty)),
                            "type": "individual"
                        })
                        log_debug("✅ 개별 TP", f"숏 {entry_qty}개 @{tp_price:.4f}")
                    except:
                        pass
            
            # 평단가 TP도 추가 (안전장치)
            elif short_size > 0 and short_entry > 0:
                place_average_tp_order(symbol, "short", short_entry, int(short_size))
            
            return
        
        # ============================================================
        # 임계값 미달 → 평단가 TP
        # ============================================================
        log_debug("⚪ 평단가 TP", "임계값 미달")
        tp_type[symbol] = "average"
        
        if long_size > 0 and long_entry > 0:
            place_average_tp_order(symbol, "long", long_entry, int(long_size))
        
        if short_size > 0 and short_entry > 0:
            place_average_tp_order(symbol, "short", short_entry, int(short_size))
        
    except Exception as e:
        log_debug("❌ TP 재생성 오류", str(e), exc_info=True)


# =============================================================================
# 그리드 관리 (initialize_grid 함수 - 비주력 헤징 로직 추가)
# =============================================================================
def initialize_grid(entry_price, skip_check=False):
    """그리드 초기화 - 전략 v19.0 완벽 구현"""
    try:
        with balance_lock:
            current_balance = INITIAL_BALANCE
        
        threshold = current_balance * THRESHOLD_RATIO
        
        with position_lock:
            pos = position_state.get(SYMBOL, {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            long_price = pos.get("long", {}).get("price", Decimal("0"))
            short_price = pos.get("short", {}).get("price", Decimal("0"))
        
        long_value = long_size * long_price if long_price > 0 else Decimal("0")
        short_value = short_size * short_price if short_price > 0 else Decimal("0")
        
        GRID_QTY = calculate_grid_qty(entry_price)
        
        cancel_grid_orders(SYMBOL)
        
        COUNTER_ENTRY_RATIO = Decimal("0.30")
        SAME_SIDE_RATIO = Decimal("0.10")
        
        # ============================================================
        # 롱 주력 + 임계값 초과
        # ============================================================
        if long_value >= threshold and short_value < threshold:
            log_debug("🔵 롱 주력 모드", f"임계값 초과 ({float(long_value):.1f} ≥ {float(threshold):.1f})")
            
            counter_qty = int(long_size * COUNTER_ENTRY_RATIO)
            same_side_qty = int(long_size * SAME_SIDE_RATIO)
            base_hedge_qty = int(long_size * HEDGE_RATIO)
            same_side_qty = max(same_side_qty, base_hedge_qty)
            
            # 비주력 숏 그리드 (역방향)
            if counter_qty >= CONTRACT_SIZE:
                short_grid_price = entry_price * (Decimal("1") + GRID_GAP_PCT)
                short_grid_price = round(short_grid_price, 4)
                
                if place_limit_order(SYMBOL, "short", short_grid_price, counter_qty):
                    log_debug("✅ 비주력 숏", f"{counter_qty}개 @{short_grid_price}")
                time.sleep(0.1)
            
            # 같은 방향 롱 그리드 (위아래 각 1개)
            if same_side_qty >= CONTRACT_SIZE:
                # 하단 그리드
                long_grid_down = entry_price * (Decimal("1") - GRID_GAP_PCT)
                long_grid_down = round(long_grid_down, 4)
                if place_limit_order(SYMBOL, "long", long_grid_down, same_side_qty):
                    log_debug("✅ 롱 하단", f"{same_side_qty}개 @{long_grid_down}")
                time.sleep(0.1)
                
                # 상단 그리드
                long_grid_up = entry_price * (Decimal("1") + GRID_GAP_PCT)
                long_grid_up = round(long_grid_up, 4)
                if place_limit_order(SYMBOL, "long", long_grid_up, same_side_qty):
                    log_debug("✅ 롱 상단", f"{same_side_qty}개 @{long_grid_up}")
                time.sleep(0.1)
            
            return
        
        # ============================================================
        # 숏 주력 + 임계값 초과
        # ============================================================
        elif short_value >= threshold and long_value < threshold:
            log_debug("🔴 숏 주력 모드", f"임계값 초과 ({float(short_value):.1f} ≥ {float(threshold):.1f})")
            
            counter_qty = int(short_size * COUNTER_ENTRY_RATIO)
            same_side_qty = int(short_size * SAME_SIDE_RATIO)
            base_hedge_qty = int(short_size * HEDGE_RATIO)
            same_side_qty = max(same_side_qty, base_hedge_qty)
            
            # 비주력 롱 그리드 (역방향)
            if counter_qty >= CONTRACT_SIZE:
                long_grid_price = entry_price * (Decimal("1") - GRID_GAP_PCT)
                long_grid_price = round(long_grid_price, 4)
                
                if place_limit_order(SYMBOL, "long", long_grid_price, counter_qty):
                    log_debug("✅ 비주력 롱", f"{counter_qty}개 @{long_grid_price}")
                time.sleep(0.1)
            
            # 같은 방향 숏 그리드 (위아래 각 1개)
            if same_side_qty >= CONTRACT_SIZE:
                # 상단 그리드
                short_grid_up = entry_price * (Decimal("1") + GRID_GAP_PCT)
                short_grid_up = round(short_grid_up, 4)
                if place_limit_order(SYMBOL, "short", short_grid_up, same_side_qty):
                    log_debug("✅ 숏 상단", f"{same_side_qty}개 @{short_grid_up}")
                time.sleep(0.1)
                
                # 하단 그리드
                short_grid_down = entry_price * (Decimal("1") - GRID_GAP_PCT)
                short_grid_down = round(short_grid_down, 4)
                if place_limit_order(SYMBOL, "short", short_grid_down, same_side_qty):
                    log_debug("✅ 숏 하단", f"{same_side_qty}개 @{short_grid_down}")
                time.sleep(0.1)
            
            return
        
        # ============================================================
        # 임계값 미달 - 케이스별 분기
        # ============================================================
        log_debug("⚪ 임계값 미달", f"롱:{long_size} 숏:{short_size}")
        
        # 1-3) 양방향 포지션 → 그리드 생성 안 함, TP만 유지
        if long_size > 0 and short_size > 0:
            log_debug("⚪ 양방향 포지션", "그리드 없이 TP만 유지")
            return
        
        # 1-2) 한쪽 포지션만 있음 → 평단가 TP + 현재가 양방향 그리드 1개씩
        elif long_size > 0 or short_size > 0:
            log_debug("⚪ 한쪽 포지션", "현재가 양방향 그리드 1개씩")
            
            long_grid_price = entry_price * (Decimal("1") - GRID_GAP_PCT)
            long_grid_price = round(long_grid_price, 4)
            if place_limit_order(SYMBOL, "long", long_grid_price, GRID_QTY):
                log_debug("✅ 롱 그리드", f"{GRID_QTY}개 @{long_grid_price}")
            time.sleep(0.1)
            
            short_grid_price = entry_price * (Decimal("1") + GRID_GAP_PCT)
            short_grid_price = round(short_grid_price, 4)
            if place_limit_order(SYMBOL, "short", short_grid_price, GRID_QTY):
                log_debug("✅ 숏 그리드", f"{GRID_QTY}개 @{short_grid_price}")
            time.sleep(0.1)
            
            return
        
        # 1-1) 포지션 없음 → 양방향 그리드 생성
        else:
            log_debug("⚪ 포지션 없음", "양방향 그리드 생성")
            
            long_grid_price = entry_price * (Decimal("1") - GRID_GAP_PCT)
            long_grid_price = round(long_grid_price, 4)
            if place_limit_order(SYMBOL, "long", long_grid_price, GRID_QTY):
                log_debug("✅ 롱 그리드", f"{GRID_QTY}개 @{long_grid_price}")
            time.sleep(0.1)
            
            short_grid_price = entry_price * (Decimal("1") + GRID_GAP_PCT)
            short_grid_price = round(short_grid_price, 4)
            if place_limit_order(SYMBOL, "short", short_grid_price, GRID_QTY):
                log_debug("✅ 숏 그리드", f"{GRID_QTY}개 @{short_grid_price}")
            time.sleep(0.1)
            
            return
        
    except Exception as e:
        log_debug("❌ 그리드 초기화 오류", str(e), exc_info=True)


# =============================================================================
# 체결 모니터링
# =============================================================================

def fill_monitor():
    """체결 모니터링 - 그리드 재생성 포함"""
    global threshold_exceeded_time, post_threshold_entries
    
    try:
        update_position_state(SYMBOL, show_log=True)
        
        prev_long_size = Decimal("0")
        prev_short_size = Decimal("0")
        last_long_action_time = 0
        last_short_action_time = 0
        last_heartbeat = time.time()
        
        with position_lock:
            pos = position_state.get(SYMBOL, {})
            prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
            prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
        
        log_debug("📊 체결 모니터 시작", f"롱:{prev_long_size} 숏:{prev_short_size}")
        
        while True:
            try:
                time.sleep(2)
                update_initial_balance()
                now = time.time()
                
                if now - last_heartbeat >= 180:
                    with position_lock:
                        pos = position_state.get(SYMBOL, {})
                        current_long = pos.get("long", {}).get("size", Decimal("0"))
                        current_short = pos.get("short", {}).get("size", Decimal("0"))
                    log_debug("💓 하트비트", f"롱:{current_long} 숏:{current_short}")
                    last_heartbeat = now
                
                update_position_state(SYMBOL)
                
                with position_lock:
                    pos = position_state.get(SYMBOL, {})
                    long_size = pos.get("long", {}).get("size", Decimal("0"))
                    short_size = pos.get("short", {}).get("size", Decimal("0"))
                    long_price = pos.get("long", {}).get("price", Decimal("0"))
                    short_price = pos.get("short", {}).get("price", Decimal("0"))
                
                # 임계값 확인
                with balance_lock:
                    current_balance = INITIAL_BALANCE
                threshold = current_balance * THRESHOLD_RATIO
                long_value = long_size * long_price if long_price > 0 else Decimal("0")
                short_value = short_size * short_price if short_price > 0 else Decimal("0")
                
                # 임계값 미달로 복귀 시 초기화
                if long_value < threshold and short_value < threshold and threshold_exceeded_time > 0:
                    log_debug("⚪ 임계값 미달 복귀", "초과 추적 초기화")
                    threshold_exceeded_time = 0
                    post_threshold_entries[SYMBOL] = {"long": [], "short": []}  # ✅ 수정
                
                # ⚡ 롱 증가 (진입)
                if long_size > prev_long_size:
                    if now - last_long_action_time >= 3:
                        added_long = long_size - prev_long_size
                        log_debug("📊 롱 진입", f"+{added_long}")
                        record_entry(SYMBOL, "long", long_price, added_long)
                        
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            current_price = Decimal(str(ticker[0].last))
                            place_hedge_order_with_counter_check(SYMBOL, "long", current_price)
                        
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        
                        with position_lock:
                            pos2 = position_state.get(SYMBOL, {})
                            recheck_long = pos2.get("long", {}).get("size", Decimal("0"))
                            recheck_short = pos2.get("short", {}).get("size", Decimal("0"))
                        
                        # ⚡⚡⚡ 그리드 취소 후 TP 재생성 → 그리드 재생성
                        cancel_grid_orders(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(0.3)
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            grid_price = Decimal(str(ticker[0].last))
                            initialize_grid(grid_price, skip_check=False)
                        
                        prev_long_size = recheck_long
                        prev_short_size = recheck_short
                        last_long_action_time = now
                
                # ⚡ 롱 감소 (청산)
                elif long_size < prev_long_size:
                    if now - last_long_action_time >= 3:
                        reduced_long = prev_long_size - long_size
                        log_debug("📉 롱 청산", f"-{reduced_long}")
                        
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        
                        with position_lock:
                            pos2 = position_state.get(SYMBOL, {})
                            recheck_long = pos2.get("long", {}).get("size", Decimal("0"))
                            recheck_short = pos2.get("short", {}).get("size", Decimal("0"))
                        
                        # ⚡⚡⚡ 청산 후 그리드 재생성
                        cancel_grid_orders(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(0.3)
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            grid_price = Decimal(str(ticker[0].last))
                            initialize_grid(grid_price, skip_check=False)
                        
                        prev_long_size = recheck_long
                        prev_short_size = recheck_short
                        last_long_action_time = now
                
                # ⚡ 숏 증가 (진입)
                if short_size > prev_short_size:
                    if now - last_short_action_time >= 3:
                        added_short = short_size - prev_short_size
                        log_debug("📊 숏 진입", f"+{added_short}")
                        record_entry(SYMBOL, "short", short_price, added_short)
                        
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            current_price = Decimal(str(ticker[0].last))
                            place_hedge_order_with_counter_check(SYMBOL, "short", current_price)
                        
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        
                        with position_lock:
                            pos2 = position_state.get(SYMBOL, {})
                            recheck_long = pos2.get("long", {}).get("size", Decimal("0"))
                            recheck_short = pos2.get("short", {}).get("size", Decimal("0"))
                        
                        # ⚡⚡⚡ 그리드 취소 후 재생성
                        cancel_grid_orders(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(0.3)
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            grid_price = Decimal(str(ticker[0].last))
                            initialize_grid(grid_price, skip_check=False)
                        
                        prev_long_size = recheck_long
                        prev_short_size = recheck_short
                        last_short_action_time = now
                
                # ⚡ 숏 감소 (청산)
                elif short_size < prev_short_size:
                    if now - last_short_action_time >= 3:
                        reduced_short = prev_short_size - short_size
                        log_debug("📉 숏 청산", f"-{reduced_short}")
                        
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        
                        with position_lock:
                            pos2 = position_state.get(SYMBOL, {})
                            recheck_long = pos2.get("long", {}).get("size", Decimal("0"))
                            recheck_short = pos2.get("short", {}).get("size", Decimal("0"))
                        
                        # ⚡⚡⚡ 청산 후 그리드 재생성
                        cancel_grid_orders(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(0.3)
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            grid_price = Decimal(str(ticker[0].last))
                            initialize_grid(grid_price, skip_check=False)
                        
                        prev_long_size = recheck_long
                        prev_short_size = recheck_short
                        last_short_action_time = now
                
            except Exception as e:
                log_debug("❌ 모니터 루프 오류", str(e), exc_info=True)
                time.sleep(3)
                
    except Exception as e:
        log_debug("❌ fill_monitor 오류", str(e), exc_info=True)


# =============================================================================
# TP 체결 모니터링
# =============================================================================

def tp_monitor():
    """TP 체결 감지 - 개별 TP 청산 시 비주력 20% 동반 청산"""
    while True:
        time.sleep(5)
        
        try:
            with position_lock:
                pos = position_state.get(SYMBOL, {})
                long_size = pos.get("long", {}).get("size", Decimal("0"))
                short_size = pos.get("short", {}).get("size", Decimal("0"))
            
            # 개별 TP 청산 감지
            if SYMBOL in tp_orders:
                for side in ["long", "short"]:
                    if side in tp_orders[SYMBOL]:
                        remaining_tps = []
                        
                        for tp_info in tp_orders[SYMBOL][side]:
                            order_id = tp_info.get("order_id")
                            tp_qty = tp_info.get("qty", Decimal("0"))
                            tp_type_val = tp_info.get("type", "average")
                            
                            try:
                                # 주문 상태 확인
                                order = api.get_futures_order(SETTLE, order_id)
                                
                                if order.status == "finished":
                                    log_debug("✅ TP 청산", f"{side.upper()} {tp_qty}개")
                                    
                                    # 개별 TP인 경우 비주력 20% 동반 청산
                                    if tp_type_val == "individual":
                                        close_counter_position_on_main_tp(SYMBOL, side, tp_qty)
                                    
                                    # TP 목록에서 제거
                                    continue
                                else:
                                    remaining_tps.append(tp_info)
                                    
                            except:
                                remaining_tps.append(tp_info)
                        
                        tp_orders[SYMBOL][side] = remaining_tps
            
            # 안전장치: 포지션은 있는데 그리드가 없는 경우
            try:
                orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                grid_orders = [o for o in orders if not o.is_reduce_only]
                
                if not grid_orders and (long_size > 0 or short_size > 0):
                    log_debug("⚠️ 안전장치", "그리드 없음 → 재생성")
                    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                    if ticker:
                        current_price = Decimal(str(ticker[0].last))
                        initialize_grid(current_price, skip_check=True)
                        time.sleep(1.0)
                        refresh_tp_orders(SYMBOL)
            except:
                pass
            
        except Exception as e:
            log_debug("❌ TP 모니터 오류", str(e), exc_info=True)


# =============================================================================
# WebSocket & 웹서버
# =============================================================================

async def price_monitor():
    """가격 모니터링"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    retry_count = 0
    
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
                
                if retry_count == 0:
                    log_debug("🔗 WebSocket 연결", SYMBOL)
                
                retry_count = 0
                
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    
                    if data.get("event") == "update" and data.get("channel") == "futures.tickers":
                        result = data.get("result")
                        if result and isinstance(result, dict):
                            price = Decimal(str(result.get("last", "0")))
                            if price > 0:
                                latest_prices[SYMBOL] = price
                    
        except:
            retry_count += 1
            await asyncio.sleep(5)


@app.route("/ping", methods=["GET", "POST"])
def ping():
    """Health Check"""
    return jsonify({"status": "ok", "time": time.time()})


# =============================================================================
# 메인
# =============================================================================

if __name__ == "__main__":
    log_debug("=" * 50)
    log_debug("🚀 시작", "v19.0-ADVANCED (로그 최적화)")
    
    # 초기 잔고 업데이트
    update_initial_balance(force=True)
    
    with balance_lock:
        current_balance = INITIAL_BALANCE
    log_debug("💰 초기 잔고", f"{float(current_balance):.2f} USDT")
    log_debug("📊 그리드 간격", f"{float(GRID_GAP_PCT) * 100:.2f}%")
    log_debug("🎯 TP 간격", f"{float(TP_GAP_PCT) * 100:.2f}%")
    log_debug("⚠️ 임계값", f"{float(current_balance * THRESHOLD_RATIO):.2f} USDT")
    
    # 전역 변수 초기화
    entry_history[SYMBOL] = {"long": [], "short": []}
    tp_orders[SYMBOL] = {"long": [], "short": []}
    
    # Shadow OBV MACD 계산
    obvmacd_val = calculate_obv_macd(SYMBOL)
    log_debug("🌑 Shadow OBV MACD", f"{float(obvmacd_val) * 1000:.2f}")
    
    # 현재 포지션 확인
    update_position_state(SYMBOL, show_log=True)
    
    with position_lock:
        pos = position_state.get(SYMBOL, {})
        long_size = pos.get("long", {}).get("size", Decimal("0"))
        short_size = pos.get("short", {}).get("size", Decimal("0"))
    
    # 현재가 조회
    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
    if not ticker or len(ticker) == 0:
        log_debug("❌ 현재가 조회 실패", "시스템 종료")
        exit(1)
    
    entry_price = Decimal(str(ticker[0].last))
    log_debug("📈 현재가", f"{float(entry_price):.4f} USDT")
    
    # 포지션 유무에 따른 초기화
    if long_size == 0 and short_size == 0:
        log_debug("🔷 초기 그리드 생성", "포지션 없음")
        initialize_grid(entry_price, skip_check=False)
    else:
        log_debug("🔶 기존 포지션 존재", f"롱:{long_size} 숏:{short_size}")
        cancel_grid_orders(SYMBOL)
        time.sleep(0.5)
        refresh_tp_orders(SYMBOL)
        
        time.sleep(0.5)
        log_debug("📊 그리드 재생성", "기존 포지션 기준")
        initialize_grid(entry_price, skip_check=False)
    
    # 모니터 시작
    log_debug("=" * 50)
    log_debug("🎬 모니터 시작")
    log_debug("=" * 50)
    
    threading.Thread(target=fill_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    # Flask 서버
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 Flask 서버", f"0.0.0.0:{port} 시작")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
