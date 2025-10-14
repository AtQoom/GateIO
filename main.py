#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ONDO 역방향 그리드 매매 시스템 v17.0-FINAL
- TP 기반 그리드 재생성
- 듀얼 TP (평단가/개별)
- 헤징 포지션: 항상 평단가 TP
- 주력 포지션: 임계값 초과 시 개별 TP
- 모든 전체 청산 시 그리드 재생성
- 예외 처리 강화
- 개별 쿨다운
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

GRID_GAP_PCT = Decimal("0.16") / Decimal("100")  # 0.21%
TP_GAP_PCT = Decimal("0.16") / Decimal("100")    # 0.21%
HEDGE_RATIO = Decimal("0.2")  # 헤징 0.3배
THRESHOLD_RATIO = Decimal("2.0")  # 임계값 2배

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
        
        return Decimal(str(round(float(final_value), 6)))
        
    except Exception as e:
        log_debug("❌ OBV MACD 오류", str(e), exc_info=True)
        return Decimal("0")


def calculate_grid_qty(current_price):
    """그리드 수량 계산 (OBV MACD 기반 0.3~0.6배)"""
    try:
        if INITIAL_BALANCE <= 0:
            return 1
        
        obv_macd = calculate_obv_macd(SYMBOL)
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

# =============================================================================
# 포지션 관리
# =============================================================================

def update_position_state(symbol, retry=5, show_log=False):
    """포지션 상태 업데이트 (선택적 로그)"""
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
                
                if show_log:
                    log_debug("🔍 포지션 최종", f"롱:{long_size}@{long_price:.4f} 숏:{short_size}@{short_price:.4f}")
                
                return True
                
        except Exception as e:
            if attempt < retry - 1:
                log_debug(f"⚠️ 포지션 조회 재시도 {attempt + 1}/{retry}", str(e))
                time.sleep(0.5)
            else:
                log_debug("❌ 포지션 업데이트 실패", str(e), exc_info=True)
                return False


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
    for retry in range(2):
        try:
            orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
            cancelled_count = 0
            cancelled_ids = []
            
            for order in orders:
                try:
                    if not order.is_reduce_only:
                        api.cancel_futures_order(SETTLE, order.id)
                        cancelled_count += 1
                        cancelled_ids.append(f"ID:{order.id} {order.size}@{order.price}")
                        time.sleep(0.1)
                except Exception as e:
                    log_debug("⚠️ 주문 취소 실패", f"ID:{order.id}")
            
            if cancelled_count > 0:
                log_debug("✅ 그리드 취소 완료", f"{cancelled_count}개 주문")
                for order_info in cancelled_ids:
                    log_debug("  ㄴ 취소", order_info)
            else:
                log_debug("ℹ️ 취소할 그리드 없음", "")
            break
            
        except Exception as e:
            if retry < 1:
                log_debug("⚠️ 그리드 취소 재시도", str(e))
                time.sleep(0.3)
            else:
                log_debug("❌ 그리드 취소 실패", str(e))


def cancel_tp_orders(symbol, side):
    """TP 주문 취소 (거래소 주문 직접 확인)"""
    try:
        cancelled_count = 0
        
        orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
        
        for order in orders:
            if not order.is_reduce_only:
                continue
            
            if side == "long" and order.size < 0:
                for retry in range(3):
                    try:
                        api.cancel_futures_order(SETTLE, order.id)
                        log_debug("✅ TP 취소", f"{symbol}_{side} ID:{order.id} {order.size}@{order.price}")
                        cancelled_count += 1
                        break
                    except Exception as e:
                        if retry < 2:
                            time.sleep(0.3)
                        else:
                            log_debug("⚠️ TP 취소 실패", f"ID:{order.id}")
            
            elif side == "short" and order.size > 0:
                for retry in range(3):
                    try:
                        api.cancel_futures_order(SETTLE, order.id)
                        log_debug("✅ TP 취소", f"{symbol}_{side} ID:{order.id} {order.size}@{order.price}")
                        cancelled_count += 1
                        break
                    except Exception as e:
                        if retry < 2:
                            time.sleep(0.3)
                        else:
                            log_debug("⚠️ TP 취소 실패", f"ID:{order.id}")
        
        if symbol in tp_orders and side in tp_orders[symbol]:
            tp_orders[symbol][side] = []
        
        if cancelled_count > 0:
            log_debug("✅ TP 전체 취소", f"{symbol}_{side} {cancelled_count}개")
        else:
            log_debug("ℹ️ 취소할 TP 없음", f"{symbol}_{side}")
            
    except Exception as e:
        log_debug("❌ TP 취소 오류", str(e), exc_info=True)

# =============================================================================
# TP 관리
# =============================================================================

def place_average_tp_order(symbol, side, price, qty, retry=3):
    """평단가 TP 지정가 주문 (재시도)"""
    for attempt in range(retry):
        try:
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
            
            log_debug("✅ 평단 TP", f"{symbol}_{side} {qty}계약 TP:{float(tp_price):.4f}")
            
            return True
            
        except Exception as e:
            if attempt < retry - 1:
                log_debug(f"⚠️ TP 재시도 {attempt + 1}/{retry}", str(e))
                time.sleep(0.5)
            else:
                log_debug("❌ 평단 TP 실패", str(e), exc_info=True)
                return False


def place_individual_tp_orders(symbol, side, entries):
    """개별 진입별 TP 지정가 주문"""
    try:
        if not entries:
            log_debug("⚠️ 진입 기록 없음", f"{symbol}_{side} - 개별 TP 생성 불가")
            return
        
        log_debug("📌 개별 TP 생성 시작", f"{symbol}_{side} {len(entries)}개 진입")
        
        for idx, entry in enumerate(entries):
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
                "qty": Decimal(str(qty)),
                "type": "individual"
            })
            
            log_debug(f"  ㄴ [{idx+1}/{len(entries)}]", 
                     f"{qty}계약 진입:{float(entry_price):.4f} → TP:{float(tp_price):.4f} ID:{result.id}")
            
            time.sleep(0.1)
        
        log_debug("✅ 개별 TP 생성 완료", f"{symbol}_{side} {len(entries)}개")
            
    except Exception as e:
        log_debug("❌ 개별 TP 실패", str(e), exc_info=True)


def check_and_update_tp_mode_locked(symbol, side, size, price):
    """임계값 체크 및 TP 모드 전환 (헤징 포지션은 항상 평단가 TP)"""
    try:
        if size == 0:
            log_debug("⚠️ 포지션 0", f"{symbol}_{side} size=0 - TP 생성 불가")
            return
        
        # 실제 거래소 주문 확인
        existing_tp_qty = Decimal("0")
        try:
            orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
            for order in orders:
                if order.is_reduce_only:
                    if (side == "long" and order.size < 0) or (side == "short" and order.size > 0):
                        order_size = abs(order.size)
                        existing_tp_qty += Decimal(str(order_size))
                        log_debug(f"🔍 TP 발견", f"{side} {order_size}계약 @ {order.price}")
        except Exception as e:
            log_debug("⚠️ TP 주문 조회 실패", str(e))
        
        # 딕셔너리 수량과 비교
        dict_tp_qty = Decimal("0")
        if symbol in tp_orders and side in tp_orders[symbol]:
            for tp in tp_orders[symbol][side]:
                dict_tp_qty += tp.get("qty", Decimal("0"))
        
        # 불일치 시 딕셔너리 정리
        if existing_tp_qty != dict_tp_qty:
            log_debug("⚠️ TP 수량 불일치", 
                     f"{symbol}_{side} 거래소:{existing_tp_qty} vs 딕셔너리:{dict_tp_qty}")
            if symbol in tp_orders and side in tp_orders[symbol]:
                tp_orders[symbol][side] = []
        
        # TP 부족하면 무조건 재생성
        if existing_tp_qty < size:
            log_debug("⚠️ TP 부족", f"{symbol}_{side} 기존:{existing_tp_qty} < 포지션:{size}")
            
            cancel_tp_orders(symbol, side)
            time.sleep(0.5)
            
            success = place_average_tp_order(symbol, side, price, size, retry=3)
            
            if success:
                if symbol not in tp_type:
                    tp_type[symbol] = {"long": "average", "short": "average"}
                tp_type[symbol][side] = "average"
            else:
                log_debug("❌ TP 생성 실패", f"{symbol}_{side}")
            
            return
        
        # TP 초과하면 재생성
        if existing_tp_qty > size:
            log_debug("⚠️ TP 초과", f"{symbol}_{side} 기존:{existing_tp_qty} > 포지션:{size}")
            
            cancel_tp_orders(symbol, side)
            time.sleep(0.5)
            
            success = place_average_tp_order(symbol, side, price, size, retry=3)
            
            if success:
                if symbol not in tp_type:
                    tp_type[symbol] = {"long": "average", "short": "average"}
                tp_type[symbol][side] = "average"
            else:
                log_debug("❌ TP 생성 실패", f"{symbol}_{side}")
            
            return
        
        # TP 정확히 일치
        log_debug("✅ TP 정확", f"{symbol}_{side} 기존:{existing_tp_qty} == 포지션:{size}")
        
        # ⭐⭐⭐ 헤징 포지션 체크 (임계값의 1.5배 미만)
        position_value = calculate_position_value(size, price)
        hedge_threshold = INITIAL_BALANCE * HEDGE_RATIO * Decimal("1.5")
        
        if position_value < hedge_threshold:
            log_debug("ℹ️ 헤징 포지션", 
                     f"{symbol}_{side} {float(position_value):.2f} < {float(hedge_threshold):.2f} (개별 TP 적용 제외)")
            
            # ⭐ 헤징 포지션은 항상 평단가 TP 유지
            current_type = tp_type.get(symbol, {}).get(side, "average")
            if current_type == "individual":
                log_debug("🔄 헤징 → 평단가 전환", f"{symbol}_{side}")
                
                cancel_tp_orders(symbol, side)
                time.sleep(0.5)
                
                success = place_average_tp_order(symbol, side, price, size, retry=3)
                
                if success:
                    if symbol not in tp_type:
                        tp_type[symbol] = {"long": "average", "short": "average"}
                    tp_type[symbol][side] = "average"
            
            return
        
        # ⭐ 주력 포지션만 임계값 체크
        threshold_value = INITIAL_BALANCE * THRESHOLD_RATIO
        current_type = tp_type.get(symbol, {}).get(side, "average")
        
        if position_value > threshold_value:
            if current_type != "individual":
                log_debug("⚠️ 임계값 초과 (주력)", 
                         f"{symbol}_{side} {float(position_value):.2f} > {float(threshold_value):.2f}")
                
                cancel_tp_orders(symbol, side)
                time.sleep(0.5)
                
                entries = entry_history.get(symbol, {}).get(side, [])
                if entries:
                    log_debug("📋 진입 기록 확인", f"{symbol}_{side} {len(entries)}개 진입")
                    
                    place_individual_tp_orders(symbol, side, entries)
                    
                    if symbol not in tp_type:
                        tp_type[symbol] = {"long": "average", "short": "average"}
                    tp_type[symbol][side] = "individual"
                    
                    log_debug("✅ 개별 TP 전환 완료 (주력)", f"{symbol}_{side}")
                else:
                    log_debug("⚠️ 진입 기록 없음", f"{symbol}_{side} - 개별 TP 생성 불가")
                    
                    if symbol not in tp_type:
                        tp_type[symbol] = {"long": "average", "short": "average"}
                    tp_type[symbol][side] = "average"
        
    except Exception as e:
        log_debug("❌ TP 모드 체크 오류", str(e), exc_info=True)


def refresh_tp_orders(symbol):
    """TP 주문 새로고침 (API 지연 대응 강화)"""
    try:
        log_debug("🔄 TP 새로고침 시작", symbol)
        
        for retry in range(5):
            if update_position_state(symbol):
                break
            log_debug(f"⚠️ 포지션 조회 재시도 {retry + 1}/5", "")
            time.sleep(0.5)
        else:
            log_debug("❌ 포지션 조회 실패", "TP 새로고침 중단")
            return
        
        time.sleep(1.0)
        update_position_state(symbol)
        time.sleep(0.5)
        
        with position_lock:
            for side in ["long", "short"]:
                pos = position_state.get(symbol, {}).get(side, {})
                size = pos.get("size", Decimal("0"))
                price = pos.get("price", Decimal("0"))
                
                log_debug(f"🔍 포지션 체크", f"{side} size:{size} price:{price:.4f}")
                
                if size > 0:
                    check_and_update_tp_mode_locked(symbol, side, size, price)
                    time.sleep(0.3)
                else:
                    log_debug(f"⚠️ 포지션 없음", f"{side} size=0")
                    
    except Exception as e:
        log_debug("❌ TP 새로고침 오류", str(e), exc_info=True)


def emergency_tp_fix(symbol):
    """긴급 TP 수정 (수동 실행용)"""
    try:
        log_debug("🚨 긴급 TP 수정 시작", symbol)
        
        update_position_state(symbol, show_log=True)
        
        for side in ["long", "short"]:
            pos = position_state.get(symbol, {}).get(side, {})
            size = pos.get("size", Decimal("0"))
            price = pos.get("price", Decimal("0"))
            
            if size > 0:
                log_debug(f"🔧 {side} TP 강제 생성", f"{size}계약 @ {price}")
                
                cancel_tp_orders(symbol, side)
                time.sleep(0.5)
                
                place_average_tp_order(symbol, side, price, size, retry=3)
                
    except Exception as e:
        log_debug("❌ 긴급 TP 수정 실패", str(e), exc_info=True)

# =============================================================================
# 그리드 관리
# =============================================================================

def initialize_grid(base_price=None, skip_check=False):
    """그리드 초기화 (지정가 주문)"""
    try:
        if not skip_check:
            orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
            grid_orders = [o for o in orders if not o.is_reduce_only]
            if grid_orders:
                log_debug("⚠️ 기존 그리드 있음", f"{len(grid_orders)}개")
                
                for order in grid_orders:
                    log_debug("  ㄴ 그리드", f"ID:{order.id} size:{order.size} price:{order.price}")
                
                log_debug("⚠️ 그리드 생성 중단", "skip_check=True로 호출 필요")
                return
        else:
            log_debug("🎯 그리드 강제 생성", "skip_check=True")
        
        if base_price is None:
            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
            if not ticker:
                return
            base_price = Decimal(str(ticker[0].last))
        
        obv_macd = calculate_obv_macd(SYMBOL)
        
        upper_price = float(base_price * (Decimal("1") + GRID_GAP_PCT))
        lower_price = float(base_price * (Decimal("1") - GRID_GAP_PCT))
        
        if obv_macd >= 0:
            short_qty = calculate_grid_qty(base_price)
            long_qty = max(1, int((INITIAL_BALANCE * HEDGE_RATIO) / (base_price * CONTRACT_SIZE)))
        else:
            long_qty = calculate_grid_qty(base_price)
            short_qty = max(1, int((INITIAL_BALANCE * HEDGE_RATIO) / (base_price * CONTRACT_SIZE)))
        
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
        
        log_debug("🎯 그리드 생성", 
                 f"기준:{base_price:.4f} 위:{upper_price:.4f}({short_qty}) 아래:{lower_price:.4f}({long_qty}) | OBV:{float(obv_macd * 1000):.2f}")
        
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
        
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price="0",
            tif="ioc"
        )
        
        result = api.create_futures_order(SETTLE, order)
        
        log_debug("📌 헤징 주문", f"{symbol} {side} {hedge_qty}계약 ID:{result.id}")
        
        time.sleep(0.5)
        try:
            order_status = api.get_futures_order(SETTLE, result.id)
            if order_status.status == "finished":
                log_debug("✅ 헤징 체결 완료", f"ID:{result.id}")
            else:
                log_debug("⚠️ 헤징 미체결", f"상태:{order_status.status}")
        except:
            pass
        
        return result.id
        
    except Exception as e:
        log_debug("❌ 헤징 주문 실패", str(e))
        return None

# =============================================================================
# 체결 모니터링
# =============================================================================

def fill_monitor():
    """체결 감지 (예외 처리 강화 + 개별 쿨다운)"""
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
        
        log_debug("👀 체결 모니터 시작", f"초기 롱:{prev_long_size} 숏:{prev_short_size}")
        
        while True:
            try:
                time.sleep(2)
                
                now = time.time()
                if now - last_heartbeat >= 60:
                    with position_lock:
                        pos = position_state.get(SYMBOL, {})
                        current_long = pos.get("long", {}).get("size", Decimal("0"))
                        current_short = pos.get("short", {}).get("size", Decimal("0"))
                    log_debug("💓 체결 모니터 작동 중", f"롱:{current_long} 숏:{current_short}")
                    last_heartbeat = now
                
                update_position_state(SYMBOL)
                
                with position_lock:
                    pos = position_state.get(SYMBOL, {})
                    long_size = pos.get("long", {}).get("size", Decimal("0"))
                    short_size = pos.get("short", {}).get("size", Decimal("0"))
                    long_price = pos.get("long", {}).get("price", Decimal("0"))
                    short_price = pos.get("short", {}).get("price", Decimal("0"))
                    
                    now = time.time()
                    
                    try:
                        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                        current_price = Decimal(str(ticker[0].last)) if ticker else Decimal("0")
                    except:
                        current_price = Decimal("0")
                    
                    # 롱 체결 감지
                    if long_size > prev_long_size and now - last_long_action_time >= 3:
                        try:
                            added_long = long_size - prev_long_size
                            
                            log_debug("📊 롱 체결 감지", f"+{added_long}계약 @ {long_price:.4f} (총 {long_size}계약)")
                            
                            record_entry(SYMBOL, "long", long_price, added_long)
                            
                            cancel_grid_orders(SYMBOL)
                            time.sleep(0.5)
                            
                            max_wait = 1.0
                            check_interval = 0.2
                            elapsed = 0
                            
                            while elapsed < max_wait:
                                time.sleep(check_interval)
                                elapsed += check_interval
                                
                                try:
                                    orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                                    grid_orders = [o for o in orders if not o.is_reduce_only]
                                    
                                    if not grid_orders:
                                        log_debug("✅ 그리드 취소 확인", f"{elapsed:.1f}초")
                                        break
                                except:
                                    pass
                            else:
                                log_debug("⚠️ 그리드 미취소", "TP 설정 계속 진행")
                            
                            log_debug("🔄 TP 새로고침 (롱)", "")
                            refresh_tp_orders(SYMBOL)
                            time.sleep(0.3)
                            
                            if current_price > 0:
                                log_debug("🔨 숏 헤징 주문", f"{current_price:.4f}")
                                place_hedge_order(SYMBOL, "short", current_price)
                                
                                log_debug("⏳ 헤징 체결 확인 중...", "")
                                hedge_filled = False
                                for retry in range(10):
                                    time.sleep(0.5)
                                    if update_position_state(SYMBOL):
                                        with position_lock:
                                            pos = position_state.get(SYMBOL, {})
                                            current_short = pos.get("short", {}).get("size", Decimal("0"))
                                            
                                            if current_short > prev_short_size:
                                                hedge_filled = True
                                                log_debug("✅ 헤징 체결 확인", f"숏:{current_short}계약 (재시도 {retry + 1}/10)")
                                                break
                                
                                if hedge_filled:
                                    time.sleep(1.0)
                                    update_position_state(SYMBOL, show_log=True)
                                    
                                    with position_lock:
                                        pos = position_state.get(SYMBOL, {})
                                        final_short = pos.get("short", {}).get("size", Decimal("0"))
                                        log_debug("🔍 최종 숏 포지션", f"{final_short}계약")
                                else:
                                    log_debug("⚠️ 헤징 미체결", "TP 설정 주의 필요")
                            
                            success = False
                            for retry in range(5):
                                if update_position_state(SYMBOL):
                                    log_debug("✅ 포지션 재조회 성공", f"재시도 {retry + 1}/5")
                                    success = True
                                    break
                                time.sleep(0.5)
                            
                            if success:
                                time.sleep(0.5)
                                log_debug("🔄 전체 TP 재설정", "롱+숏")
                                refresh_tp_orders(SYMBOL)
                                
                                time.sleep(1)
                                update_position_state(SYMBOL, show_log=True)
                            
                            with position_lock:
                                pos = position_state.get(SYMBOL, {})
                                prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                                prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                                log_debug("✅ 롱 체결 처리 완료", f"최종 롱:{prev_long_size} 숏:{prev_short_size}")
                            
                            last_long_action_time = now
                            
                        except Exception as e:
                            log_debug("❌ 롱 처리 오류", str(e), exc_info=True)
                    
                    # 숏 체결 감지
                    if short_size > prev_short_size and now - last_short_action_time >= 3:
                        try:
                            added_short = short_size - prev_short_size
                            
                            log_debug("📊 숏 체결 감지", f"+{added_short}계약 @ {short_price:.4f} (총 {short_size}계약)")
                            
                            record_entry(SYMBOL, "short", short_price, added_short)
                            
                            cancel_grid_orders(SYMBOL)
                            time.sleep(0.5)
                            
                            max_wait = 1.0
                            check_interval = 0.2
                            elapsed = 0
                            
                            while elapsed < max_wait:
                                time.sleep(check_interval)
                                elapsed += check_interval
                                
                                try:
                                    orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                                    grid_orders = [o for o in orders if not o.is_reduce_only]
                                    
                                    if not grid_orders:
                                        log_debug("✅ 그리드 취소 확인", f"{elapsed:.1f}초")
                                        break
                                except:
                                    pass
                            else:
                                log_debug("⚠️ 그리드 미취소", "TP 설정 계속 진행")
                            
                            log_debug("🔄 TP 새로고침 (숏)", "")
                            refresh_tp_orders(SYMBOL)
                            time.sleep(0.3)
                            
                            if current_price > 0:
                                log_debug("🔨 롱 헤징 주문", f"{current_price:.4f}")
                                place_hedge_order(SYMBOL, "long", current_price)
                                
                                log_debug("⏳ 헤징 체결 확인 중...", "")
                                hedge_filled = False
                                for retry in range(10):
                                    time.sleep(0.5)
                                    if update_position_state(SYMBOL):
                                        with position_lock:
                                            pos = position_state.get(SYMBOL, {})
                                            current_long = pos.get("long", {}).get("size", Decimal("0"))
                                            
                                            if current_long > prev_long_size:
                                                hedge_filled = True
                                                log_debug("✅ 헤징 체결 확인", f"롱:{current_long}계약 (재시도 {retry + 1}/10)")
                                                break
                                
                                if hedge_filled:
                                    time.sleep(1.0)
                                    update_position_state(SYMBOL, show_log=True)
                                    
                                    with position_lock:
                                        pos = position_state.get(SYMBOL, {})
                                        final_long = pos.get("long", {}).get("size", Decimal("0"))
                                        log_debug("🔍 최종 롱 포지션", f"{final_long}계약")
                                else:
                                    log_debug("⚠️ 헤징 미체결", "TP 설정 주의 필요")
                            
                            success = False
                            for retry in range(5):
                                if update_position_state(SYMBOL):
                                    log_debug("✅ 포지션 재조회 성공", f"재시도 {retry + 1}/5")
                                    success = True
                                    break
                                time.sleep(0.5)
                            
                            if success:
                                time.sleep(0.5)
                                log_debug("🔄 전체 TP 재설정", "롱+숏")
                                refresh_tp_orders(SYMBOL)
                                
                                time.sleep(1)
                                update_position_state(SYMBOL, show_log=True)
                            
                            with position_lock:
                                pos = position_state.get(SYMBOL, {})
                                prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                                prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                                log_debug("✅ 숏 체결 처리 완료", f"최종 롱:{prev_long_size} 숏:{prev_short_size}")
                            
                            last_short_action_time = now
                            
                        except Exception as e:
                            log_debug("❌ 숏 처리 오류", str(e), exc_info=True)
                
            except Exception as e:
                log_debug("❌ 체결 모니터 루프 오류", str(e), exc_info=True)
                time.sleep(5)
                continue
                
    except Exception as e:
        log_debug("❌ 체결 모니터 초기화 실패", str(e), exc_info=True)

# =============================================================================
# TP 체결 모니터링
# =============================================================================

def tp_monitor():
    """TP 체결 감지 및 그리드 재생성 (모든 전체 청산 시 재생성!)"""
    prev_long_size = None
    prev_short_size = None
    
    while True:
        time.sleep(3)
        
        try:
            update_position_state(SYMBOL)
            
            with position_lock:
                pos = position_state.get(SYMBOL, {})
                long_size = pos.get("long", {}).get("size", Decimal("0"))
                short_size = pos.get("short", {}).get("size", Decimal("0"))
                
                if prev_long_size is None:
                    prev_long_size = long_size
                    prev_short_size = short_size
                    log_debug("👀 TP 모니터 시작", f"초기 롱:{long_size} 숏:{short_size}")
                    continue
                
                # ⭐⭐⭐ 롱 포지션이 0이 되었을 때 (개별 TP도 재생성!)
                if long_size == 0 and prev_long_size > 0:
                    prev_long_size = long_size
                    
                    long_type = tp_type.get(SYMBOL, {}).get("long", "average")
                    
                    if long_type == "average":
                        log_debug("✅ 롱 평단 TP 청산", "그리드 재생성!")
                    else:
                        log_debug("✅ 롱 개별 TP 전체 청산", "그리드 재생성!")
                    
                    if SYMBOL in entry_history:
                        entry_history[SYMBOL]["long"] = []
                    if SYMBOL in tp_type:
                        tp_type[SYMBOL]["long"] = "average"
                    
                    cancel_grid_orders(SYMBOL)
                    time.sleep(0.5)
                    
                    max_wait = 2.0
                    check_interval = 0.2
                    elapsed = 0
                    
                    while elapsed < max_wait:
                        time.sleep(check_interval)
                        elapsed += check_interval
                        
                        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                        grid_orders = [o for o in orders if not o.is_reduce_only]
                        
                        if not grid_orders:
                            log_debug("✅ 그리드 취소 완료", f"{elapsed:.1f}초")
                            break
                    else:
                        log_debug("⚠️ 그리드 취소 미완료", "강제 재생성")
                    
                    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                    if ticker:
                        current_price = Decimal(str(ticker[0].last))
                        initialize_grid(current_price, skip_check=True)
                        
                        time.sleep(1.5)
                        update_position_state(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(1.0)
                        update_position_state(SYMBOL, show_log=True)
                        refresh_tp_orders(SYMBOL)

                        with position_lock:
                            pos = position_state.get(SYMBOL, {})
                            final_long = pos.get("long", {}).get("size", Decimal("0"))
                            final_short = pos.get("short", {}).get("size", Decimal("0"))
    
                            if final_long > 0 or final_short > 0:
                                log_debug("✅ 그리드 재생성 완료", f"롱:{final_long} 숏:{final_short}")
                            else:
                                log_debug("⚠️ 포지션 없음", "대기 상태")
                
                # ⭐⭐⭐ 숏 포지션이 0이 되었을 때 (개별 TP도 재생성!)
                elif short_size == 0 and prev_short_size > 0:
                    prev_short_size = short_size
                    
                    short_type = tp_type.get(SYMBOL, {}).get("short", "average")
                    
                    if short_type == "average":
                        log_debug("✅ 숏 평단 TP 청산", "그리드 재생성!")
                    else:
                        log_debug("✅ 숏 개별 TP 전체 청산", "그리드 재생성!")
                    
                    if SYMBOL in entry_history:
                        entry_history[SYMBOL]["short"] = []
                    if SYMBOL in tp_type:
                        tp_type[SYMBOL]["short"] = "average"
                    
                    cancel_grid_orders(SYMBOL)
                    time.sleep(0.5)
                    
                    max_wait = 2.0
                    check_interval = 0.2
                    elapsed = 0
                    
                    while elapsed < max_wait:
                        time.sleep(check_interval)
                        elapsed += check_interval
                        
                        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                        grid_orders = [o for o in orders if not o.is_reduce_only]
                        
                        if not grid_orders:
                            log_debug("✅ 그리드 취소 완료", f"{elapsed:.1f}초")
                            break
                    else:
                        log_debug("⚠️ 그리드 취소 미완료", "강제 재생성")
                    
                    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                    if ticker:
                        current_price = Decimal(str(ticker[0].last))
                        initialize_grid(current_price, skip_check=True)
                        
                        time.sleep(1.5)
                        update_position_state(SYMBOL)
                        refresh_tp_orders(SYMBOL)
                        
                        time.sleep(1.0)
                        update_position_state(SYMBOL, show_log=True)
                        refresh_tp_orders(SYMBOL)

                        with position_lock:
                            pos = position_state.get(SYMBOL, {})
                            final_long = pos.get("long", {}).get("size", Decimal("0"))
                            final_short = pos.get("short", {}).get("size", Decimal("0"))
    
                            if final_long > 0 or final_short > 0:
                                log_debug("✅ 그리드 재생성 완료", f"롱:{final_long} 숏:{final_short}")
                            else:
                                log_debug("⚠️ 포지션 없음", "대기 상태")
                
                else:
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
                
                if retry_count > 0:
                    log_debug("🔗 WebSocket 재연결 성공", f"{SYMBOL} (재시도 {retry_count}회 후)")
                else:
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
                    
        except Exception as e:
            retry_count += 1
            if retry_count % 10 == 1:
                log_debug("❌ WebSocket 오류", f"재시도 {retry_count}회")
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
    log_debug("🚀 서버 시작", "v17.0-FINAL")
    
    INITIAL_BALANCE = Decimal(str(get_available_balance(show_log=True)))
    log_debug("💰 초기 잔고", f"{INITIAL_BALANCE:.2f} USDT")
    log_debug("🎯 임계값", f"{float(INITIAL_BALANCE * THRESHOLD_RATIO):.2f} USDT ({int(THRESHOLD_RATIO)}배)")
    log_debug("🛡️ 헤징 기준", f"{float(INITIAL_BALANCE * HEDGE_RATIO * Decimal('1.5')):.2f} USDT (0.45배)")
    
    entry_history[SYMBOL] = {"long": [], "short": []}
    tp_orders[SYMBOL] = {"long": [], "short": []}
    tp_type[SYMBOL] = {"long": "average", "short": "average"}
    
    obv_macd_val = calculate_obv_macd(SYMBOL)
    log_debug("📊 Shadow OBV MACD", f"{SYMBOL}: {float(obv_macd_val * 1000):.2f}")
    
    update_position_state(SYMBOL, show_log=True)
    with position_lock:
        pos = position_state.get(SYMBOL, {})
        long_size = pos.get("long", {}).get("size", Decimal("0"))
        short_size = pos.get("short", {}).get("size", Decimal("0"))
        
        if long_size > 0 or short_size > 0:
            log_debug("⚠️ 기존 포지션 감지", f"롱:{long_size} 숏:{short_size}")
            
            log_debug("🗑️ 기존 그리드 취소", "시작...")
            cancel_grid_orders(SYMBOL)
            time.sleep(1)
            
            log_debug("🔧 기존 포지션 TP 설정", "시작...")
            emergency_tp_fix(SYMBOL)
            time.sleep(1)
            
            log_debug("🎯 새 그리드 생성", "시작...")
            initialize_grid(skip_check=True)
        else:
            log_debug("✅ 포지션 없음", "초기 그리드 생성...")
            initialize_grid()
    
    threading.Thread(target=fill_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
