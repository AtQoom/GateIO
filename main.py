#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ONDO 역방향 그리드 매매 시스템 v18.2-OPTIMIZED
- 복리 자동화: 1시간마다 실제 잔고 업데이트
- 환경변수 기반 설정 (속도/안정성 극대화)
- 수량 계산: 레버리지 1배 기준
- OBV MACD 가중 수량 (0.10~0.35)
- 그리드/TP 간격 0.12%
- 헤징 0.1배
- 임계값 1배
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
# 환경변수 기반 설정
# =============================================================================

SETTLE = "usdt"
SYMBOL = "ONDO_USDT"
CONTRACT_SIZE = Decimal("1")

# ⭐ 환경변수로 모든 설정 관리
GRID_GAP_PCT = Decimal(os.environ.get("GRID_GAP_PCT", "0.12")) / Decimal("100")
TP_GAP_PCT = Decimal(os.environ.get("TP_GAP_PCT", "0.12")) / Decimal("100")
HEDGE_RATIO = Decimal(os.environ.get("HEDGE_RATIO", "0.1"))
THRESHOLD_RATIO = Decimal(os.environ.get("THRESHOLD_RATIO", "1.0"))
BALANCE_UPDATE_INTERVAL = int(os.environ.get("BALANCE_UPDATE_INTERVAL", "3600"))  # 기본 1시간

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

# ⭐ 복리를 위한 전역 변수
INITIAL_BALANCE = Decimal("0")
last_balance_update = 0
balance_lock = threading.RLock()

# 전역 변수
position_lock = threading.RLock()
position_state = {}
latest_prices = {}
entry_history = {}
tp_orders = {}
tp_type = {}
threshold_exceeded_time = {}  # 임계값 초과 시점 기록 {symbol: {side: timestamp}}
post_threshold_entries = {}   # 임계값 초과 후 진입 기록 {symbol: {side: [entries]}}

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
        # ✅ 수정: get_current_balance() → INITIAL_BALANCE 사용
        with balance_lock:
            current_balance = INITIAL_BALANCE
        
        # OBV MACD 값 가져오기
        obv_macd_value = calculate_obv_macd(SYMBOL)
        abs_val = abs(obv_macd_value * 1000)
        
        # ⭐ OBV 기반 가중치 (0.10 ~ 0.35)
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
        
        log_debug("🔢 수량 계산", f"OBV:{obv_macd_value:.5f} → 가중:{float(weight):.2f} → {qty}계약")
        return max(qty, CONTRACT_SIZE)
    except Exception as e:
        log_debug("❌ 수량 계산 오류", str(e), exc_info=True)
        return int(Decimal("10"))


def place_limit_order(symbol, side, price, qty, retry=3):
    """지정가 주문 (그리드용)"""
    for attempt in range(retry):
        try:
            if side == "short":
                order_size = -int(qty)
            else:
                order_size = int(qty)
            
            order = FuturesOrder(
                contract=symbol,
                size=order_size,
                price=str(round(float(price), 4)),
                tif="gtc",
                reduce_only=False
            )
            result = api.create_futures_order(SETTLE, order)
            log_debug("📍 그리드 주문 생성", f"{symbol}_{side} {qty}@{price:.4f} ID:{result.id}")
            return result.id
        except Exception as e:
            if attempt < retry - 1:
                log_debug(f"⚠️ 그리드 주문 재시도 ({attempt+1}/{retry})", str(e))
                time.sleep(0.5)
            else:
                log_debug("❌ 그리드 주문 오류", str(e), exc_info=True)
                return None


def calculate_position_value(qty, price):
    """포지션 가치 계산"""
    return qty * price * CONTRACT_SIZE

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
    """TP 주문 취소"""
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
    """평단가 TP 지정가 주문"""
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
            log_debug("⚠️ 진입 기록 없음", f"{symbol}_{side}")
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
    """TP 모드 체크 및 업데이트 (임계값 초과 후 진입만 개별 TP)"""
    try:
        if size == 0:
            return
        
        # 기존 TP 수량 체크
        existing_tp_qty = Decimal("0")
        try:
            orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
            for order in orders:
                if order.is_reduce_only:
                    if (side == "long" and order.size < 0) or (side == "short" and order.size > 0):
                        existing_tp_qty += Decimal(str(abs(order.size)))
        except:
            pass
        
        # 임계값 체크
        with balance_lock:
            current_balance = INITIAL_BALANCE
        position_value = size * price
        threshold_value = current_balance * THRESHOLD_RATIO
        
        # 임계값 초과 여부 확인
        if position_value >= threshold_value:
            # ⭐ 임계값 초과 시점 기록 (최초 1회만)
            if symbol not in threshold_exceeded_time:
                threshold_exceeded_time[symbol] = {}
            if side not in threshold_exceeded_time[symbol]:
                threshold_exceeded_time[symbol][side] = time.time()
                log_debug("📍 임계값 초과 시점 기록", f"{symbol}_{side}")
                
                # 기존 포지션에 평단 TP 생성
                if existing_tp_qty != size:
                    cancel_tp_orders(symbol, side)
                    time.sleep(0.5)
                    place_average_tp_order(symbol, side, price, size)
                    log_debug("✅ 임계값 초과 (초기)", f"{symbol}_{side} 평단 TP")
                return
            
            # ⭐ 이미 임계값 초과 상태 → 추가 진입 확인
            if existing_tp_qty < size:
                # 새로운 진입이 추가됨!
                added_qty = size - existing_tp_qty
                log_debug("📍 임계값 초과 후 추가 진입", f"{symbol}_{side} +{added_qty}계약")
                
                # 추가 진입 기록
                if symbol not in post_threshold_entries:
                    post_threshold_entries[symbol] = {"long": [], "short": []}
                
                # entry_history에서 최신 진입 찾기
                entries = entry_history.get(symbol, {}).get(side, [])
                if entries:
                    latest_entry = entries[-1]
                    post_threshold_entries[symbol][side].append(latest_entry)
                    
                    # 추가 진입에 대한 개별 TP 생성
                    entry_price = latest_entry["price"]
                    qty = latest_entry["qty"]
                    
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
                    log_debug("✅ 개별 TP 추가", f"{symbol}_{side} {qty}@{tp_price:.4f}")
                return
            
            elif existing_tp_qty > size:
                # 일부 청산됨
                log_debug("📍 TP 청산 감지", f"{symbol}_{side} -{existing_tp_qty - size}계약")
                
                # post_threshold_entries 정리 (FIFO)
                if symbol in post_threshold_entries and side in post_threshold_entries[symbol]:
                    entries_list = post_threshold_entries[symbol][side]
                    total_post_qty = sum(e["qty"] for e in entries_list)
                    
                    # 청산된 수량만큼 제거
                    removed_qty = existing_tp_qty - size
                    while entries_list and removed_qty > 0:
                        if entries_list[0]["qty"] <= removed_qty:
                            removed = entries_list.pop(0)
                            removed_qty -= removed["qty"]
                            log_debug("🗑️ 청산 진입 제거", f"{removed['price']:.4f} {removed['qty']}계약")
                        else:
                            entries_list[0]["qty"] -= removed_qty
                            removed_qty = 0
                return
        
        else:
            # 임계값 미만 = 헤징 포지션
            # 평단 TP로 관리
            if existing_tp_qty != size:
                cancel_tp_orders(symbol, side)
                time.sleep(0.5)
                place_average_tp_order(symbol, side, price, size)
                log_debug("✅ 헤징 TP", f"{symbol}_{side} 평단 {size}계약")
            return
            
    except Exception as e:
        log_debug("❌ TP 모드 체크 오류", str(e), exc_info=True)


def refresh_tp_orders(symbol):
    """TP 주문 새로고침"""
    try:
        log_debug("🔄 TP 새로고침 시작", symbol)
        
        for retry in range(5):
            if update_position_state(symbol):
                break
            time.sleep(0.5)
        else:
            log_debug("❌ 포지션 조회 실패", "")
            return
        
        time.sleep(1.0)
        update_position_state(symbol)
        time.sleep(0.5)
        
        with position_lock:
            for side in ["long", "short"]:
                pos = position_state.get(symbol, {}).get(side, {})
                size = pos.get("size", Decimal("0"))
                price = pos.get("price", Decimal("0"))
                
                if size > 0:
                    check_and_update_tp_mode_locked(symbol, side, size, price)
                    time.sleep(0.3)
                    
    except Exception as e:
        log_debug("❌ TP 새로고침 오류", str(e), exc_info=True)


def emergency_tp_fix(symbol):
    """긴급 TP 수정"""
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

def initialize_grid(current_price=None, skip_check=False):
    """그리드 초기화 (양방향 포지션 시 생성 방지)"""
    try:
        # ⭐ current_price가 None이면 현재가 가져오기
        if current_price is None:
            try:
                ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                if ticker:
                    current_price = Decimal(str(ticker[0].last))
                else:
                    log_debug("❌ 현재가 조회 실패", "그리드 생성 중단")
                    return
            except Exception as e:
                log_debug("❌ 현재가 조회 오류", str(e))
                return
        
        # ⭐ skip_check=True여도 양방향 포지션 체크!
        with position_lock:
            pos = position_state.get(SYMBOL, {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            
            # 양방향 있으면 절대 그리드 생성 안함!
            if long_size > 0 and short_size > 0:
                log_debug("⚠️ 양방향 포지션 감지", "그리드 생성 중단!")
                return
            
            # 한쪽만 있으면 반대쪽만 생성
            if long_size > 0 and short_size == 0:
                log_debug("📍 롱만 존재", "숏 그리드만 생성")
                qty = calculate_grid_qty(current_price)
                upper_price = current_price * (Decimal("1") + GRID_GAP_PCT)
                place_limit_order(SYMBOL, "short", upper_price, qty)  # ✅ place_limit_order
                log_debug("✅ 숏 그리드 생성", f"{qty}@{upper_price:.4f}")
                return
            
            elif short_size > 0 and long_size == 0:
                log_debug("📍 숏만 존재", "롱 그리드만 생성")
                qty = calculate_grid_qty(current_price)
                lower_price = current_price * (Decimal("1") - GRID_GAP_PCT)
                place_limit_order(SYMBOL, "long", lower_price, qty)  # ✅ place_limit_order
                log_debug("✅ 롱 그리드 생성", f"{qty}@{lower_price:.4f}")
                return
        
        # 포지션 없을 때만 양방향 그리드
        if not skip_check:
            with position_lock:
                pos = position_state.get(SYMBOL, {})
                long_size = pos.get("long", {}).get("size", Decimal("0"))
                short_size = pos.get("short", {}).get("size", Decimal("0"))
                
                if long_size > 0 or short_size > 0:
                    log_debug("⚠️ 포지션 존재", "그리드 생성 안함")
                    return
        
        # 양방향 그리드 생성
        cancel_grid_orders(SYMBOL)
        time.sleep(0.3)
        
        qty = calculate_grid_qty(current_price)
        upper_price = current_price * (Decimal("1") + GRID_GAP_PCT)
        lower_price = current_price * (Decimal("1") - GRID_GAP_PCT)
        
        place_limit_order(SYMBOL, "short", upper_price, qty)  # ✅ place_limit_order
        time.sleep(0.2)
        place_limit_order(SYMBOL, "long", lower_price, qty)   # ✅ place_limit_order
        
        log_debug("✅ 그리드 생성 완료", f"상:{upper_price:.4f} 하:{lower_price:.4f}")
        
    except Exception as e:
        log_debug("❌ 그리드 생성 오류", str(e), exc_info=True)


# =============================================================================
# 헤징 관리
# =============================================================================

def place_hedge_order(symbol, side, current_price):
    """헤징 시장가 주문"""
    try:
        with balance_lock:
            current_balance = INITIAL_BALANCE
        
        hedge_qty = max(1, int((current_balance * HEDGE_RATIO) / (current_price * CONTRACT_SIZE)))
        
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
    """체결 모니터링 (청산/진입 구분 + 양방향 포지션 시 그리드 생성 방지)"""
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
                
                # 자본 업데이트
                update_initial_balance()
                
                now = time.time()
                
                # 하트비트 (3분마다)
                if now - last_heartbeat >= 180:
                    with position_lock:
                        pos = position_state.get(SYMBOL, {})
                        current_long = pos.get("long", {}).get("size", Decimal("0"))
                        current_short = pos.get("short", {}).get("size", Decimal("0"))
                    with balance_lock:
                        current_balance = INITIAL_BALANCE
                    log_debug("💓 하트비트", f"롱:{current_long} 숏:{current_short} 잔고:{float(current_balance):.2f}U")
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
                
                # ⭐⭐⭐ 롱 포지션 변화 감지 (3초 쿨타임)
                if long_size != prev_long_size and now - last_long_action_time >= 3:
                    try:
                        added_long = long_size - prev_long_size
                        
                        # ⭐ 증가했을 때만 진입으로 처리
                        if added_long > 0:
                            # OBV MACD 계산
                            obv_macd = calculate_obv_macd(SYMBOL)
                            obv_display = float(obv_macd) * 1000
                            
                            log_debug("📊 롱 진입", f"+{added_long}@{long_price:.4f} → 총:{long_size} OBV:{obv_display:.2f}")
                            
                            # 진입 기록
                            record_entry(SYMBOL, "long", long_price, added_long)
                            
                            # ⭐⭐⭐ 헤징 대기 (3초) 후 재확인
                            time.sleep(3.0)
                            update_position_state(SYMBOL)
                            
                            with position_lock:
                                pos2 = position_state.get(SYMBOL, {})
                                recheck_long = pos2.get("long", {}).get("size", Decimal("0"))
                                recheck_short = pos2.get("short", {}).get("size", Decimal("0"))
                                
                                # ⭐ 양방향 포지션이면 그리드 생성 안함!
                                if recheck_long > 0 and recheck_short > 0:
                                    log_debug("✅ 재확인 → 양방향 포지션", f"롱:{recheck_long} 숏:{recheck_short} → TP만")
                                    cancel_grid_orders(SYMBOL)
                                    time.sleep(0.5)
                                    refresh_tp_orders(SYMBOL)
                                
                                # 여전히 롱만 있으면 그리드 생성
                                elif recheck_long > 0 and recheck_short == 0:
                                    log_debug("⚡ 재확인 → 롱만 존재", "그리드 생성!")
                                    cancel_grid_orders(SYMBOL)
                                    time.sleep(0.5)
                                    refresh_tp_orders(SYMBOL)
                                    
                                    # 헤징 주문
                                    if current_price > 0:
                                        log_debug("🔄 헤징 주문", f"숏@{current_price:.4f}")
                                        place_hedge_order(SYMBOL, "short", current_price)
                                    
                                    time.sleep(5)
                                    update_position_state(SYMBOL)
                                    time.sleep(0.5)
                                    refresh_tp_orders(SYMBOL)
                                    time.sleep(1)
                                    update_position_state(SYMBOL, show_log=True)
                                    
                                    # 최종 확인 후 그리드 생성
                                    with position_lock:
                                        pos3 = position_state.get(SYMBOL, {})
                                        final_long = pos3.get("long", {}).get("size", Decimal("0"))
                                        final_short = pos3.get("short", {}).get("size", Decimal("0"))
                                        
                                        if final_long > 0 and final_short == 0:
                                            log_debug("⚡ 최종 확인 → 롱만", "그리드 생성!")
                                            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                                            if ticker:
                                                grid_price = Decimal(str(ticker[0].last))
                                                initialize_grid(grid_price, skip_check=True)
                                        elif final_short > 0 and final_long == 0:
                                            log_debug("⚡ 최종 확인 → 숏만", "그리드 생성!")
                                            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                                            if ticker:
                                                grid_price = Decimal(str(ticker[0].last))
                                                initialize_grid(grid_price, skip_check=True)
                                        else:
                                            log_debug("✅ 최종 → 양방향", f"롱:{final_long} 숏:{final_short} → TP만")
                                        
                                        prev_long_size = final_long
                                        prev_short_size = final_short
                            
                            log_debug("✅ 롱 처리 완료", f"롱:{prev_long_size} 숏:{prev_short_size}")
                        
                        else:
                            # 감소 = 청산
                            log_debug("📊 롱 청산", f"{added_long}@{long_price:.4f} → 총:{long_size}")
                            prev_long_size = long_size
                        
                        last_long_action_time = now
                        
                    except Exception as e:
                        log_debug("❌ 롱 처리 오류", str(e), exc_info=True)
                
                # ⭐⭐⭐ 숏 포지션 변화 감지 (3초 쿨타임)
                if short_size != prev_short_size and now - last_short_action_time >= 3:
                    try:
                        added_short = short_size - prev_short_size
                        
                        # ⭐ 증가했을 때만 진입으로 처리
                        if added_short > 0:
                            # OBV MACD 계산
                            obv_macd = calculate_obv_macd(SYMBOL)
                            obv_display = float(obv_macd) * 1000
                            
                            log_debug("📊 숏 진입", f"+{added_short}@{short_price:.4f} → 총:{short_size} OBV:{obv_display:.2f}")
                            
                            # 진입 기록
                            record_entry(SYMBOL, "short", short_price, added_short)
                            
                            # ⭐⭐⭐ 헤징 대기 (3초) 후 재확인
                            time.sleep(3.0)
                            update_position_state(SYMBOL)
                            
                            with position_lock:
                                pos2 = position_state.get(SYMBOL, {})
                                recheck_long = pos2.get("long", {}).get("size", Decimal("0"))
                                recheck_short = pos2.get("short", {}).get("size", Decimal("0"))
                                
                                # ⭐ 양방향 포지션이면 그리드 생성 안함!
                                if recheck_long > 0 and recheck_short > 0:
                                    log_debug("✅ 재확인 → 양방향 포지션", f"롱:{recheck_long} 숏:{recheck_short} → TP만")
                                    cancel_grid_orders(SYMBOL)
                                    time.sleep(0.5)
                                    refresh_tp_orders(SYMBOL)
                                
                                # 여전히 숏만 있으면 그리드 생성
                                elif recheck_short > 0 and recheck_long == 0:
                                    log_debug("⚡ 재확인 → 숏만 존재", "그리드 생성!")
                                    cancel_grid_orders(SYMBOL)
                                    time.sleep(0.5)
                                    refresh_tp_orders(SYMBOL)
                                    
                                    # 헤징 주문
                                    if current_price > 0:
                                        log_debug("🔄 헤징 주문", f"롱@{current_price:.4f}")
                                        place_hedge_order(SYMBOL, "long", current_price)
                                    
                                    time.sleep(5)
                                    update_position_state(SYMBOL)
                                    time.sleep(0.5)
                                    refresh_tp_orders(SYMBOL)
                                    time.sleep(1)
                                    update_position_state(SYMBOL, show_log=True)
                                    
                                    # 최종 확인 후 그리드 생성
                                    with position_lock:
                                        pos3 = position_state.get(SYMBOL, {})
                                        final_long = pos3.get("long", {}).get("size", Decimal("0"))
                                        final_short = pos3.get("short", {}).get("size", Decimal("0"))
                                        
                                        if final_long > 0 and final_short == 0:
                                            log_debug("⚡ 최종 확인 → 롱만", "그리드 생성!")
                                            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                                            if ticker:
                                                grid_price = Decimal(str(ticker[0].last))
                                                initialize_grid(grid_price, skip_check=True)
                                        elif final_short > 0 and final_long == 0:
                                            log_debug("⚡ 최종 확인 → 숏만", "그리드 생성!")
                                            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                                            if ticker:
                                                grid_price = Decimal(str(ticker[0].last))
                                                initialize_grid(grid_price, skip_check=True)
                                        else:
                                            log_debug("✅ 최종 → 양방향", f"롱:{final_long} 숏:{final_short} → TP만")
                                        
                                        prev_long_size = final_long
                                        prev_short_size = final_short
                            
                            log_debug("✅ 숏 처리 완료", f"롱:{prev_long_size} 숏:{prev_short_size}")
                        
                        else:
                            # 감소 = 청산
                            log_debug("📊 숏 청산", f"{added_short}@{short_price:.4f} → 총:{short_size}")
                            prev_short_size = short_size
                        
                        last_short_action_time = now
                        
                    except Exception as e:
                        log_debug("❌ 숏 처리 오류", str(e), exc_info=True)
                
            except Exception as e:
                log_debug("❌ 모니터 루프 오류", str(e), exc_info=True)
                
    except Exception as e:
        log_debug("❌ fill_monitor 오류", str(e), exc_info=True)


# =============================================================================
# TP 체결 모니터링
# =============================================================================

def tp_monitor():
    """TP 체결 감지 및 그리드 재생성"""
    prev_long_size = None
    prev_short_size = None
    last_grid_check = time.time()  # ⭐ 안전장치
    
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
                
                # ⭐⭐⭐ 디버깅 로그 추가
                if long_size != prev_long_size or short_size != prev_short_size:
                    log_debug("🔍 TP 모니터 포지션 변화 감지", 
                             f"롱: {prev_long_size} → {long_size} | 숏: {prev_short_size} → {short_size}")
                
                # 롱 포지션 0 감지
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
                    
                    # ⭐ 복리: 그리드 재생성 전 자본금 업데이트
                    update_initial_balance(force=True)
                    
                    cancel_grid_orders(SYMBOL)
                    time.sleep(0.5)
                    
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
                        
                        last_grid_check = time.time()  # ⭐ 안전장치 리셋

                        with position_lock:
                            pos = position_state.get(SYMBOL, {})
                            final_long = pos.get("long", {}).get("size", Decimal("0"))
                            final_short = pos.get("short", {}).get("size", Decimal("0"))
    
                            if final_long > 0 or final_short > 0:
                                log_debug("✅ 그리드 재생성 완료", f"롱:{final_long} 숏:{final_short}")
                
                # 숏 포지션 0 감지
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
                    
                    # ⭐ 복리: 그리드 재생성 전 자본금 업데이트
                    update_initial_balance(force=True)
                    
                    cancel_grid_orders(SYMBOL)
                    time.sleep(0.5)
                    
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
                        
                        last_grid_check = time.time()  # ⭐ 안전장치 리셋

                        with position_lock:
                            pos = position_state.get(SYMBOL, {})
                            final_long = pos.get("long", {}).get("size", Decimal("0"))
                            final_short = pos.get("short", {}).get("size", Decimal("0"))
    
                            if final_long > 0 or final_short > 0:
                                log_debug("✅ 그리드 재생성 완료", f"롱:{final_long} 숏:{final_short}")
                
                else:
                    prev_long_size = long_size
                    prev_short_size = short_size
                
                # ⭐⭐⭐ 안전장치: 5분마다 그리드 체크
                now = time.time()
                if now - last_grid_check >= 300:  # 5분
                    log_debug("⏰ 안전장치: 그리드 상태 확인", f"롱:{long_size} 숏:{short_size}")
                    
                    # 그리드 주문 확인
                    try:
                        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                        grid_orders = [o for o in orders if not o.is_reduce_only]
                        
                        if not grid_orders and (long_size > 0 or short_size > 0):
                            log_debug("⚠️ 안전장치: 그리드 없음!", "강제 재생성")
                            
                            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                            if ticker:
                                current_price = Decimal(str(ticker[0].last))
                                initialize_grid(current_price, skip_check=True)
                                
                                time.sleep(1.0)
                                refresh_tp_orders(SYMBOL)
                    except Exception as e:
                        log_debug("❌ 안전장치 오류", str(e))
                    
                    last_grid_check = now
                
        except Exception as e:
            log_debug("❌ TP 모니터 오류", str(e), exc_info=True)


# =============================================================================
# WebSocket 가격 모니터링
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
                
                if retry_count > 0:
                    log_debug("🔗 WebSocket 재연결 성공", f"{SYMBOL}")
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
    log_debug("🚀 서버 시작", "v18.2-OPTIMIZED (복리 + 최적화)")
    
    # ⭐ 초기 자본금 설정
    update_initial_balance(force=True)
    
    with balance_lock:
        current_balance = INITIAL_BALANCE
    
    # 설정 출력
    log_debug("⚙️ 초기 자본금", f"{float(current_balance):.2f} USDT")
    log_debug("⚙️ 복리 업데이트 주기", f"{BALANCE_UPDATE_INTERVAL}초 ({BALANCE_UPDATE_INTERVAL/3600:.1f}시간)")
    log_debug("⚙️ 그리드 간격", f"{float(GRID_GAP_PCT * 100):.2f}%")
    log_debug("⚙️ TP 간격", f"{float(TP_GAP_PCT * 100):.2f}%")
    log_debug("⚙️ 헤징 비율", f"{float(HEDGE_RATIO):.1f}배")
    log_debug("⚙️ 임계값", f"{float(current_balance * THRESHOLD_RATIO):.2f} USDT ({float(THRESHOLD_RATIO):.1f}배)")
    
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
            
            cancel_grid_orders(SYMBOL)
            time.sleep(1)
            
            emergency_tp_fix(SYMBOL)
            time.sleep(1)
            
            initialize_grid(skip_check=True)
        else:
            initialize_grid()
    
    threading.Thread(target=fill_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
