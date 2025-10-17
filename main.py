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
BASE_QTY = Decimal("0.1")

# ⭐ 환경변수로 모든 설정 관리
GRID_GAP_PCT = Decimal(os.environ.get("GRID_GAP_PCT", "0.12")) / Decimal("100")
TP_GAP_PCT = Decimal(os.environ.get("TP_GAP_PCT", "0.12")) / Decimal("100")
HEDGE_RATIO = Decimal(os.environ.get("HEDGE_RATIO", "0.1"))
THRESHOLD_RATIO = Decimal(os.environ.get("THRESHOLD_RATIO", "0.8"))
BALANCE_UPDATE_INTERVAL = int(os.environ.get("BALANCE_UPDATE_INTERVAL", "3600"))

# ⭐⭐⭐ 새로운 설정
COUNTER_POSITION_RATIO = Decimal("0.30")  # 역방향 그리드 비율
COUNTER_CLOSE_RATIO = Decimal("0.20")     # 동반 청산 비율
COUNTER_ENTRY_RATIO = Decimal("0.30")     # ⭐ 추가! 역방향 진입 비율
max_position_locked = {"long": False, "short": False}  # ⭐ 추가! 500% 제한 플래그

# API 설정
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
if not API_KEY or not API_SECRET:
    logger.critical("API 키 없음")
    exit(1)

# 그리드 생성 락
grid_lock = threading.Lock()
grid_creation_time = 0

# 최대 포지션 제한
MAX_POSITION_RATIO = Decimal("5.0")  # 자산의 3배
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
post_threshold_entries = {SYMBOL: {"long": [], "short": []}}
grid_lock = threading.Lock()
grid_creation_time = 0
grid_orders = {SYMBOL: {"long": [], "short": []}}

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

def calculate_base_quantity():
    """기본 수량 계산"""
    with balance_lock:
        current_balance = INITIAL_BALANCE
    
    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
    if ticker:
        current_price = Decimal(str(ticker[0].last))
        target_value = current_balance * Decimal("0.10")
        qty = round(float(target_value / current_price))
        return max(qty, 1)
    return 1

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
# 헤징 로직 수정 (자산의 0.1배)
# =============================================================================

def place_hedge_order(symbol, side, price):
    """일반 헤징 (자산의 0.1배) - 시장가 주문"""
    try:
        # ⚡⚡⚡ 자산(잔고)의 0.1배 계산
        with balance_lock:
            current_balance = INITIAL_BALANCE
        
        if current_balance == 0:
            return None
        
        # 현재가 조회
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker:
            log_debug("❌ 헤징 실패", "현재가 조회 실패")
            return None
        
        current_price = Decimal(str(ticker[0].last))
        
        # ⚡⚡⚡ 자산의 0.1배를 현재가로 환산하여 수량 계산
        target_value = current_balance * HEDGE_RATIO  # 자산 × 0.1
        hedge_qty = round(float(target_value / current_price))  # 수량 = 자산 / 가격
        
        if hedge_qty < 1:
            hedge_qty = 1
        
        # 역방향 헤징
        hedge_side = "short" if side == "long" else "long"
        
        # 시장가 주문 (IOC)
        if hedge_side == "long":
            order_size = hedge_qty  # 양수
        else:
            order_size = -hedge_qty  # 음수
        
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price=str(round(float(current_price), 4)),
            tif='ioc',  # 시장가 주문
            reduce_only=False
        )
        
        result = api.create_futures_order(SETTLE, order)
        
        if result:
            log_debug(f"✅ 헤징", f"{hedge_side.upper()} {hedge_qty}개 (자산 0.1배)")
        
        return result
        
    except Exception as e:
        log_debug("❌ 헤징 오류", str(e), exc_info=True)
        return None


def place_hedge_order_with_counter_check(symbol, main_side, current_price):
    """헤징 + 역방향 진입 통합 (즉시 실행)"""
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
        
        # ⚡ 500% 최대 포지션 제한 체크
        max_position_value = current_balance * MAX_POSITION_RATIO
        if main_side == "long" and short_value >= max_position_value:
            log_debug("⚠️ 헤징 스킵", f"숏 포지션 최대 초과 ({short_value:.2f})")
            return
        if main_side == "short" and long_value >= max_position_value:
            log_debug("⚠️ 헤징 스킵", f"롱 포지션 최대 초과 ({long_value:.2f})")
            return
        
        # ============================================================
        # 모드 판단
        # ============================================================
        main_value = long_value if main_side == "long" else short_value
        
        if main_value >= threshold:
            # 모드 2: 임계값 초과 - max(주력 10%, 자산 0.1배)
            main_size = long_size if main_side == "long" else short_size
            
            # 주력의 10%
            qty_10pct = int(main_size * Decimal("0.10"))
            
            # 자산의 0.1배
            target_value = current_balance * HEDGE_RATIO
            qty_hedge = round(float(target_value / current_price))
            
            # 둘 중 큰 값
            hedge_qty = max(qty_10pct, qty_hedge)
            
            log_debug("✅ 헤징", f"역방향 {hedge_qty}개 (임계값 초과: max(10%, 0.1배))")
        else:
            # 모드 1: 임계값 미달 - 자산의 0.1배
            target_value = current_balance * HEDGE_RATIO
            hedge_qty = round(float(target_value / current_price))
            
            log_debug("✅ 헤징", f"역방향 {hedge_qty}개 (자산 0.1배)")
        
        if hedge_qty <= 0:
            return
        
        # 시장가 주문 (역방향)
        counter_side = "short" if main_side == "long" else "long"
        order_size = -hedge_qty if counter_side == "long" else hedge_qty
        
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price="0",
            tif='ioc',
            reduce_only=False
        )
        
        api.create_futures_order(SETTLE, order)
        
    except Exception as e:
        log_debug("❌ 헤징 오류", str(e), exc_info=True)


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

def place_grid_order(symbol, side, price, qty):
    """그리드 주문 생성 및 ID 저장"""
    try:
        result = place_limit_order(symbol, side, price, qty, reduce_only=False)
        if result:
            if symbol not in grid_orders:
                grid_orders[symbol] = {"long": [], "short": []}
            grid_orders[symbol][side].append({
                'order_id': result.id,
                'side': side,
                'price': price,
                'qty': qty
            })
            log_debug("✅ 그리드 생성", f"{side.upper()} {qty}개 @{price}")
    except Exception as e:
        log_debug("❌ 그리드 생성 오류", str(e))


# =============================================================================
# TP 관리
# =============================================================================

def place_average_tp_order(symbol, side, price, qty, retry=3):
    """평단가 TP 지정가 주문 - 총 수량 기준 중복 방지"""
    for attempt in range(retry):
        try:
            # ⚡⚡⚡ 포지션 수량 확인
            with position_lock:
                pos = position_state.get(symbol, {})
                
                if side == "long":
                    current_size = pos.get("long", {}).get("size", Decimal("0"))
                else:
                    current_size = pos.get("short", {}).get("size", Decimal("0"))
            
            if current_size == 0:
                return False  # 포지션 없으면 스킵
            
            # 최대 수량 제한
            qty = min(int(qty), int(current_size))
            
            # ⚡⚡⚡ 기존 TP의 총 수량 계산
            try:
                orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
                
                total_tp_qty = 0
                for order in orders:
                    if not order.is_reduce_only:
                        continue
                    
                    # 같은 방향의 TP 수량 합산
                    if side == "long" and order.size < 0:
                        total_tp_qty += abs(order.size)
                    elif side == "short" and order.size > 0:
                        total_tp_qty += abs(order.size)
                
                # ⚡⚡⚡ TP 총 수량이 포지션 수량과 같거나 크면 스킵
                if total_tp_qty >= current_size:
                    return False  # 충분한 TP가 있음
                
                # ⚡⚡⚡ 부족한 수량만큼만 TP 생성
                remaining_qty = int(current_size - total_tp_qty)
                if remaining_qty <= 0:
                    return False
                
                qty = min(qty, remaining_qty)
                
            except:
                pass
            
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
            
            log_debug("✅ TP 생성", f"{side} {qty}개 @{float(tp_price):.4f}")
            return True
            
        except GateApiException as e:
            error_msg = str(e)
            if "REDUCE_ONLY_FAIL" in error_msg:
                return False
            elif attempt < retry - 1:
                time.sleep(0.5)
            else:
                return False
        except Exception as e:
            if attempt < retry - 1:
                time.sleep(0.5)
            else:
                return False

def refresh_tp_orders(symbol):
    """⭐ TP 공존 로직: 개별 TP + 평단가 TP"""
    try:
        # 기존 TP 전체 취소
        try:
            orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
            tp_list = [o for o in orders if o.is_reduce_only]
            for tp in tp_list:
                api.cancel_futures_order(SETTLE, tp.id)
            log_debug("🔄 TP 취소", f"{len(tp_list)}개")
        except:
            pass
        
        tp_orders[symbol] = {"long": [], "short": []}
        
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
        
        # ============================================================
        # 롱 포지션 TP 생성
        # ============================================================
        if long_size > 0:
            # 개별 TP 수량 계산
            individual_qty_total = sum(Decimal(str(e['qty'])) for e in post_threshold_entries.get(symbol, {}).get('long', []))
            average_qty = long_size - individual_qty_total
            
            # 개별 TP 생성 (임계값 초과 후 진입)
            for entry_info in post_threshold_entries.get(symbol, {}).get('long', []):
                entry_price = entry_info["price"]
                entry_qty = int(entry_info["qty"])
                tp_price = entry_price * (Decimal("1") + TP_GAP_PCT)
                tp_price = round(tp_price, 4)
                
                try:
                    order_size = -entry_qty
                    order = FuturesOrder(
                        contract=symbol,
                        size=order_size,
                        price=str(tp_price),
                        tif="gtc",
                        reduce_only=True
                    )
                    result = api.create_futures_order(SETTLE, order)
                    tp_orders[symbol]["long"].append({
                        "order_id": result.id,
                        "tp_price": tp_price,
                        "qty": Decimal(str(entry_qty)),
                        "type": "individual"
                    })
                    log_debug("✅ 개별 TP", f"롱 {entry_qty}개 @{tp_price}")
                except Exception as e:
                    log_debug("❌ 개별 TP 생성 오류", str(e))
            
            # 평단가 TP 생성 (임계값 초과 전 진입)
            if average_qty > 0:
                tp_price = long_price * (Decimal("1") + TP_GAP_PCT)
                tp_price = round(tp_price, 4)
                
                try:
                    order_size = -int(average_qty)
                    order = FuturesOrder(
                        contract=symbol,
                        size=order_size,
                        price=str(tp_price),
                        tif="gtc",
                        reduce_only=True
                    )
                    result = api.create_futures_order(SETTLE, order)
                    tp_orders[symbol]["long"].append({
                        "order_id": result.id,
                        "tp_price": tp_price,
                        "qty": average_qty,
                        "type": "average"
                    })
                    log_debug("✅ 평단가 TP", f"롱 {int(average_qty)}개 @{tp_price}")
                except Exception as e:
                    log_debug("❌ 평단가 TP 생성 오류", str(e))
        
        # ============================================================
        # 숏 포지션 TP 생성
        # ============================================================
        if short_size > 0:
            # 개별 TP 수량 계산
            individual_qty_total = sum(Decimal(str(e['qty'])) for e in post_threshold_entries.get(symbol, {}).get('short', []))
            average_qty = short_size - individual_qty_total
            
            # 개별 TP 생성 (임계값 초과 후 진입)
            for entry_info in post_threshold_entries.get(symbol, {}).get('short', []):
                entry_price = entry_info["price"]
                entry_qty = int(entry_info["qty"])
                tp_price = entry_price * (Decimal("1") - TP_GAP_PCT)
                tp_price = round(tp_price, 4)
                
                try:
                    order_size = entry_qty
                    order = FuturesOrder(
                        contract=symbol,
                        size=order_size,
                        price=str(tp_price),
                        tif="gtc",
                        reduce_only=True
                    )
                    result = api.create_futures_order(SETTLE, order)
                    tp_orders[symbol]["short"].append({
                        "order_id": result.id,
                        "tp_price": tp_price,
                        "qty": Decimal(str(entry_qty)),
                        "type": "individual"
                    })
                    log_debug("✅ 개별 TP", f"숏 {entry_qty}개 @{tp_price}")
                except Exception as e:
                    log_debug("❌ 개별 TP 생성 오류", str(e))
            
            # 평단가 TP 생성 (임계값 초과 전 진입)
            if average_qty > 0:
                tp_price = short_price * (Decimal("1") - TP_GAP_PCT)
                tp_price = round(tp_price, 4)
                
                try:
                    order_size = int(average_qty)
                    order = FuturesOrder(
                        contract=symbol,
                        size=order_size,
                        price=str(tp_price),
                        tif="gtc",
                        reduce_only=True
                    )
                    result = api.create_futures_order(SETTLE, order)
                    tp_orders[symbol]["short"].append({
                        "order_id": result.id,
                        "tp_price": tp_price,
                        "qty": average_qty,
                        "type": "average"
                    })
                    log_debug("✅ 평단가 TP", f"숏 {int(average_qty)}개 @{tp_price}")
                except Exception as e:
                    log_debug("❌ 평단가 TP 생성 오류", str(e))
                    
    except Exception as e:
        log_debug("❌ TP 재생성 오류", str(e), exc_info=True)


# =============================================================================
# 그리드 관리 (initialize_grid 함수 - 비주력 헤징 로직 추가)
# =============================================================================
def initialize_grid(current_price, skip_check=False):
    """그리드 초기화 (중복 방지 + 양방향 체크 강화)"""
    global last_grid_time
    
    # ⭐ 최신 포지션 상태 강제 업데이트 (중복 진입 방지)
    try:
        positions = api.list_all_positions(SETTLE, contract=SYMBOL)
        if positions:
            for p in positions:
                side = "long" if p.size > 0 else "short"
                with position_lock:
                    position_state[SYMBOL][side]["size"] = abs(Decimal(str(p.size)))
                    position_state[SYMBOL][side]["price"] = abs(Decimal(str(p.entry_price)))
    except:
        pass

    """그리드 초기화 - 중복 생성 완전 차단 + 500% 제한"""
    global grid_creation_time
    
    try:
        # ⚡ 1초 내 중복 호출 차단
        now = time.time()
        if now - grid_creation_time < 1.0:
            log_debug("⚠️ 그리드 스킵", "1초 내 중복 호출")
            return
        
        # ⚡ 락으로 동시 실행 차단
        if not grid_lock.acquire(blocking=False):
            log_debug("⚠️ 그리드 스킵", "이미 생성 중")
            return
        
        try:
            # ⚡ 기존 그리드 확인
            try:
                orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                grid_orders = [o for o in orders if not o.is_reduce_only]
                
                if len(grid_orders) > 0:
                    log_debug("⚠️ 그리드 스킵", f"기존 {len(grid_orders)}개 존재")
                    return
            except:
                pass
            
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
            
            # ⚡ 500% 최대 포지션 제한 체크 - 그리드 생성 차단만
            max_position_value = current_balance * MAX_POSITION_RATIO
            if long_value >= max_position_value or short_value >= max_position_value:
                log_debug("⚠️ 그리드 차단", f"최대 포지션 도달 - TP 대기 중")
                return
            
            # ============================================================
            # 모드 2: 임계값 초과 - 역방향 그리드 30%
            # ============================================================
            if long_value >= threshold and short_value < threshold:
                log_debug("🔵 롱 주력", f"역방향 숏 그리드 30% 생성")
                
                # 역방향 (숏) 그리드: 주력의 30%
                counter_qty = int(long_size * COUNTER_ENTRY_RATIO)
                if counter_qty > 0:
                    grid_price_short = entry_price * (Decimal("1") + GRID_GAP_PCT)
                    place_grid_order(SYMBOL, "short", grid_price_short, counter_qty)
                    
                    grid_creation_time = now
                return
            
            if short_value >= threshold and long_value < threshold:
                log_debug("🔴 숏 주력", f"역방향 롱 그리드 30% 생성")
                
                # 역방향 (롱) 그리드: 주력의 30%
                counter_qty = int(short_size * COUNTER_ENTRY_RATIO)
                if counter_qty > 0:
                    grid_price_long = entry_price * (Decimal("1") - GRID_GAP_PCT)
                    place_grid_order(SYMBOL, "long", grid_price_long, counter_qty)
                    
                    grid_creation_time = now
                return
            
            # ============================================================
            # 모드 1: 양방향 or 임계값 미달
            # ============================================================
            if long_size > 0 and short_size > 0:
                log_debug("⚪ 양방향 포지션", "그리드 없이 TP만 유지")
                return
            
            # 한쪽 포지션만 있음 - 양방향 그리드 1개씩
            if long_size > 0 or short_size > 0:
                log_debug("⚪ 한쪽 포지션", "양방향 그리드 1개씩")
                
                grid_price_long = entry_price * (Decimal("1") - GRID_GAP_PCT)
                grid_price_short = entry_price * (Decimal("1") + GRID_GAP_PCT)
                
                base_qty = calculate_base_quantity()
                
                place_grid_order(SYMBOL, "long", grid_price_long, base_qty)
                place_grid_order(SYMBOL, "short", grid_price_short, base_qty)
                
                grid_creation_time = now
                return
            
            # ⭐ 포지션 없음 - 현재가 기준 양방향 그리드 1개씩
            log_debug("⚪ 포지션 없음", "현재가 기준 양방향 그리드 생성")

            grid_price_long = current_price * (Decimal("1") - GRID_GAP_PCT)
            grid_price_short = current_price * (Decimal("1") + GRID_GAP_PCT)

            base_qty = calculate_base_quantity()

            place_grid_order(SYMBOL, "long", grid_price_long, base_qty)
            place_grid_order(SYMBOL, "short", grid_price_short, base_qty)

            grid_creation_time = now
            return
            
        finally:
            grid_lock.release()
            
    except Exception as e:
        log_debug("❌ 그리드 초기화 오류", str(e), exc_info=True)


def grid_fill_monitor():
    """⭐ 그리드 체결 감지 및 후속 헤징 실행"""
    log_debug("🔍 그리드 체결 모니터 시작", "")
    
    while True:
        time.sleep(2)
        
        try:
            for side in ['long', 'short']:
                filled_orders = []
                
                for order_info in grid_orders[SYMBOL][side]:
                    order_id = order_info['order_id']
                    
                    try:
                        order_status = api.get_futures_order(SETTLE, order_id)
                        
                        if order_status.status == 'finished':
                            log_debug("🎉 그리드 체결", f"{side.upper()} 주문 {order_id} 체결")
                            
                            # 후속 헤징 실행: 주력 방향으로 max(10%, 0.1배) 시장가
                            main_side = 'short' if side == 'long' else 'long'
                            
                            with position_lock:
                                main_pos = position_state.get(SYMBOL, {}).get(main_side, {})
                                main_size = main_pos.get('size', Decimal("0"))
                            
                            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                            if ticker and main_size > 0:
                                current_price = Decimal(str(ticker[0].last))
                                
                                # 주력의 10%
                                qty_10pct = int(main_size * Decimal("0.10"))
                                
                                # 자산의 0.1배
                                with balance_lock:
                                    balance = INITIAL_BALANCE
                                qty_hedge_asset = round(float((balance * HEDGE_RATIO) / current_price))
                                
                                # 둘 중 큰 값
                                hedge_qty = max(qty_10pct, qty_hedge_asset)
                                
                                if hedge_qty > 0:
                                    # 시장가 헤징 주문
                                    hedge_order_size = -hedge_qty if main_side == 'long' else hedge_qty
                                    hedge_order = FuturesOrder(
                                        contract=SYMBOL,
                                        size=hedge_order_size,
                                        price="0",
                                        tif='ioc',
                                        reduce_only=False
                                    )
                                    api.create_futures_order(SETTLE, hedge_order)
                                    log_debug("✅ 후속 헤징", f"{main_side.upper()} {hedge_qty}개 시장가")
                            
                            filled_orders.append(order_info)
                            
                    except GateApiException as e:
                        if "order not found" in str(e).lower():
                            filled_orders.append(order_info)
                        else:
                            log_debug("❌ 그리드 상태 확인 오류", str(e))
                    except Exception as e:
                        log_debug("❌ 그리드 처리 오류", str(e))
                
                # 처리된 주문 목록에서 제거
                grid_orders[SYMBOL][side] = [o for o in grid_orders[SYMBOL][side] if o not in filled_orders]
        
        except Exception as e:
            log_debug("❌ 그리드 모니터 오류", str(e), exc_info=True)


# =============================================================================
# 체결 모니터링
# =============================================================================

def fill_monitor():
    """체결 모니터링 - 동반 청산 제거, 그리드 재생성만"""
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
                
                # 하트비트
                if now - last_heartbeat >= 600:
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
                
                # 변화 없으면 스킵
                if long_size == prev_long_size and short_size == prev_short_size:
                    continue
                
                # 500% 최대 포지션 제한 체크
                with balance_lock:
                    current_balance = INITIAL_BALANCE
                
                max_position_value = current_balance * MAX_POSITION_RATIO
                long_value = long_size * long_price if long_price > 0 else Decimal("0")
                short_value = short_size * short_price if short_price > 0 else Decimal("0")
                
                # ⭐ 500% 제한: 청산 대신 진입 차단
                if long_value >= max_position_value and not max_position_locked["long"]:
                    log_debug("⚠️ 최대 포지션", f"롱 500% 도달 - 추가 진입 차단")
                    max_position_locked["long"] = True
                    cancel_grid_orders(SYMBOL)  # 그리드만 취소 (TP는 유지)

                if short_value >= max_position_value and not max_position_locked["short"]:
                    log_debug("⚠️ 최대 포지션", f"숏 500% 도달 - 추가 진입 차단")
                    max_position_locked["short"] = True
                    cancel_grid_orders(SYMBOL)  # 그리드만 취소 (TP는 유지)

                # TP로 포지션 감소 시 잠금 해제
                if long_value < max_position_value and max_position_locked["long"]:
                    log_debug("✅ 잠금 해제", "롱 500% 미만 복귀")
                    max_position_locked["long"] = False

                if short_value < max_position_value and max_position_locked["short"]:
                    log_debug("✅ 잠금 해제", "숏 500% 미만 복귀")
                    max_position_locked["short"] = False

                # 잠금 상태에서는 진입 로직 스킵
                if max_position_locked["long"] and long_size > prev_long_size:
                    log_debug("🚫 진입 차단", "롱 최대 포지션 잠금 중")
                    prev_long_size = long_size
                    continue

                if max_position_locked["short"] and short_size > prev_short_size:
                    log_debug("🚫 진입 차단", "숏 최대 포지션 잠금 중")
                    prev_short_size = short_size
                    continue
                
                # 임계값 확인
                threshold = current_balance * THRESHOLD_RATIO
                
                # 임계값 미달 복귀
                if long_value < threshold and short_value < threshold and threshold_exceeded_time > 0:
                    log_debug("⚪ 임계값 미달 복귀", "추적 초기화")
                    threshold_exceeded_time = 0
                    post_threshold_entries[SYMBOL] = {"long": [], "short": []}
                
                # 롱 증가 (진입)
                if long_size > prev_long_size:
                    if now - last_long_action_time >= 3:
                        added_long = long_size - prev_long_size
                        log_debug("📈 롱 진입", f"+{added_long}")
                        record_entry(SYMBOL, "long", long_price, added_long)
                        
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        
                        cancel_grid_orders(SYMBOL)
                        cancel_tp_orders(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(1.0)
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            grid_price = Decimal(str(ticker[0].last))
                            initialize_grid(grid_price, skip_check=False)
                        
                        with position_lock:
                            pos = position_state.get(SYMBOL, {})
                            prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                            prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                        
                        last_long_action_time = now
                
                # 롱 감소 (청산) - ⭐ 동반 청산 제거
                elif long_size < prev_long_size:
                    if now - last_long_action_time >= 3:
                        reduced_long = prev_long_size - long_size
                        log_debug("📉 롱 청산", f"-{reduced_long}")
                        
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        
                        cancel_grid_orders(SYMBOL)
                        cancel_tp_orders(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(1.0)
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            grid_price = Decimal(str(ticker[0].last))
                            initialize_grid(grid_price, skip_check=False)
                        
                        with position_lock:
                            pos = position_state.get(SYMBOL, {})
                            prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                            prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                        
                        last_long_action_time = now
                
                # 숏 증가 (진입)
                if short_size > prev_short_size:
                    if now - last_short_action_time >= 3:
                        added_short = short_size - prev_short_size
                        log_debug("📈 숏 진입", f"+{added_short}")
                        record_entry(SYMBOL, "short", short_price, added_short)
                        
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        
                        cancel_grid_orders(SYMBOL)
                        cancel_tp_orders(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(1.0)
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            grid_price = Decimal(str(ticker[0].last))
                            initialize_grid(grid_price, skip_check=False)
                        
                        with position_lock:
                            pos = position_state.get(SYMBOL, {})
                            prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                            prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                        
                        last_short_action_time = now
                
                # 숏 감소 (청산) - ⭐ 동반 청산 제거
                elif short_size < prev_short_size:
                    if now - last_short_action_time >= 3:
                        reduced_short = prev_short_size - short_size
                        log_debug("📉 숏 청산", f"-{reduced_short}")
                        
                        time.sleep(0.5)
                        update_position_state(SYMBOL)
                        
                        cancel_grid_orders(SYMBOL)
                        cancel_tp_orders(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(1.0)
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        if ticker:
                            grid_price = Decimal(str(ticker[0].last))
                            initialize_grid(grid_price, skip_check=False)
                        
                        with position_lock:
                            pos = position_state.get(SYMBOL, {})
                            prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                            prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                        
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
    """TP 체결 감지 및 개별 TP 체결 시 동반 청산"""
    while True:
        time.sleep(5)
        
        try:
            with position_lock:
                pos = position_state.get(SYMBOL, {})
                long_size = pos.get("long", {}).get("size", Decimal("0"))
                short_size = pos.get("short", {}).get("size", Decimal("0"))
            
            # ⭐ 개별 TP 청산 감지
            if SYMBOL in tp_orders:
                for side in ["long", "short"]:
                    if side in tp_orders[SYMBOL]:
                        remaining_tps = []
                        
                        for tp_info in tp_orders[SYMBOL][side]:
                            order_id = tp_info.get("order_id")
                            tp_qty = tp_info.get("qty", Decimal("0"))
                            tp_type_val = tp_info.get("type", "average")
                            
                            try:
                                order = api.get_futures_order(SETTLE, order_id)
                                
                                if order.status == "finished":
                                    log_debug("✅ TP 청산", f"{side.upper()} {tp_qty}개")
                                    
                                    # ⭐ 개별 TP가 체결되면 역방향 20% 청산
                                    if tp_type_val == "individual":
                                        close_counter_on_individual_tp(SYMBOL, side, tp_qty)
                                    
                                    continue
                                else:
                                    remaining_tps.append(tp_info)
                                    
                            except:
                                remaining_tps.append(tp_info)
                        
                        tp_orders[SYMBOL][side] = remaining_tps
            
            # 안전장치: 포지션 있는데 주문 없으면 재생성
            try:
                if long_size == 0 and short_size == 0:
                    continue
                
                orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                grid_orders_list = [o for o in orders if not o.is_reduce_only]
                tp_orders_list = [o for o in orders if o.is_reduce_only]
                
                # TP가 없으면 재생성
                if not tp_orders_list:
                    log_debug("⚠️ 안전장치", "TP 없음 → 재생성")
                    cancel_tp_orders(SYMBOL)
                    refresh_tp_orders(SYMBOL)
                    time.sleep(0.5)
                
                # 그리드는 양방향 아닐 때만 재생성
                if not grid_orders_list and not (long_size > 0 and short_size > 0):
                    log_debug("⚠️ 안전장치", "그리드 없음 → 재생성")
                    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                    if ticker:
                        current_price = Decimal(str(ticker[0].last))
                        initialize_grid(current_price, skip_check=True)
            except:
                pass
            
        except Exception as e:
            log_debug("❌ TP 모니터 오류", str(e), exc_info=True)

def close_counter_on_individual_tp(symbol, main_side, tp_qty):
    """개별 TP 체결 시 역방향 포지션 20% 청산"""
    try:
        # 역방향 포지션 확인
        counter_side = "short" if main_side == "long" else "long"
        
        with position_lock:
            pos = position_state.get(symbol, {})
            counter_size = pos.get(counter_side, {}).get("size", Decimal("0"))
        
        if counter_size <= 0:
            return
        
        # 임계값 확인 (공격 모드일 때만 동작)
        with balance_lock:
            current_balance = INITIAL_BALANCE
        threshold = current_balance * THRESHOLD_RATIO
        
        with position_lock:
            main_value = pos.get(main_side, {}).get("size", 0) * pos.get(main_side, {}).get("price", 0)
        
        # 주력이 임계값 초과 상태일 때만 동작
        if main_value >= threshold:
            # 역방향 포지션의 20% 계산
            close_qty = int(counter_size * Decimal("0.20"))
            if close_qty < 1:
                close_qty = 1
            
            log_debug("⚡ 동반 청산", f"{counter_side.upper()} {close_qty}개 (개별 TP {tp_qty}개 체결)")
            
            # 시장가 청산
            order_size = close_qty if counter_side == "long" else -close_qty
            order = FuturesOrder(
                contract=symbol,
                size=order_size,
                price="0",
                tif='ioc',
                reduce_only=True
            )
            api.create_futures_order(SETTLE, order)
            
    except Exception as e:
        log_debug("❌ 동반 청산 오류", str(e), exc_info=True)



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
        cancel_tp_orders(SYMBOL)  # ← 추가!
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
    threading.Thread(target=grid_fill_monitor, daemon=True).start()  # ⭐ 신규 추가
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    # Flask 서버
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 Flask 서버", f"0.0.0.0:{port} 시작")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
