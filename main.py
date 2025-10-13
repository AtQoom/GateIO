#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

SETTLE = "usdt"
GRID_GAP_PCT = Decimal("0.19") / Decimal("100")  # 0.19%
TP_GAP_PCT = Decimal("0.18") / Decimal("100")  # 0.18% TP

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
entry_history = {}  # 진입 기록
INITIAL_BALANCE = Decimal("100")  # 초기 자본금
THRESHOLD_RATIO = Decimal("10.0")  # 10배 임계값 (테스트)
CONTRACT_SIZE = Decimal("0.01")  # ETH 계약 크기

# ⭐ 마지막 체결가 기록
last_long_fill_price = None
last_short_fill_price = None

app = Flask(__name__)

# =============================================================================
# 유틸리티 함수
# =============================================================================

def log_debug(label, msg="", exc_info=False):
    """로그 출력"""
    if exc_info:
        logger.error(f"[{label}] {msg}", exc_info=True)
    else:
        logger.info(f"[{label}] {msg}")


def get_primary_direction():
    """주력 방향 판단 (실제 포지션 물량 기준)"""
    try:
        with position_lock:
            pos = position_state.get("ETH_USDT", {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            long_price = pos.get("long", {}).get("price", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            short_price = pos.get("short", {}).get("price", Decimal("0"))
            
            # ⭐ 포지션 가치 기준 (더 정확)
            long_value = calculate_position_value(long_size, long_price)
            short_value = calculate_position_value(short_size, short_price)
            
            # 물량(가치)이 많은 쪽이 주력
            if long_value > short_value:
                return "long"   # 롱 가치 많음 → 롱 주력
            elif short_value > long_value:
                return "short"  # 숏 가치 많음 → 숏 주력
            else:
                # 동일하면 OBV MACD로 판단
                obv_macd = calculate_obv_macd("ETH_USDT")
                if obv_macd >= 0:
                    return "short"
                else:
                    return "long"
                    
    except Exception as e:
        log_debug("❌ 주력 방향 판단 오류", str(e))
        # 에러 시 OBV MACD 백업
        try:
            obv_macd = calculate_obv_macd("ETH_USDT")
            if obv_macd >= 0:
                return "short"
            else:
                return "long"
        except:
            return None


def get_candles(symbol, interval="1m", limit=100):
    """캔들 데이터 가져오기"""
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
        log_debug("❌ 캔들 조회 실패", str(e), exc_info=True)
        return None


def calculate_obv_macd(symbol):
    """OBV MACD 계산"""
    try:
        df = get_candles(symbol, interval="5m", limit=200)
        if df is None or len(df) < 50:
            return Decimal("0")
        
        # OBV 계산
        obv = [0]
        for i in range(1, len(df)):
            if df['close'].iloc[i] > df['close'].iloc[i-1]:
                obv.append(obv[-1] + df['volume'].iloc[i])
            elif df['close'].iloc[i] < df['close'].iloc[i-1]:
                obv.append(obv[-1] - df['volume'].iloc[i])
            else:
                obv.append(obv[-1])
        
        df['obv'] = obv
        
        # OBV MACD 계산
        exp1 = df['obv'].ewm(span=12, adjust=False).mean()
        exp2 = df['obv'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        
        return Decimal(str(macd.iloc[-1]))
    except Exception as e:
        log_debug("❌ OBV MACD 오류", str(e), exc_info=True)
        return Decimal("0")


def get_available_balance(show_log=False):
    """사용 가능 잔고 조회 (Unified Account 우선)"""
    try:
        # 1. Unified Account 시도
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
                        
                        if isinstance(usdt_data, dict):
                            equity_str = str(usdt_data.get("equity", "0"))
                        else:
                            equity_str = str(getattr(usdt_data, "equity", "0"))
                        usdt_balance = float(equity_str)
                        if usdt_balance > 0:
                            if show_log:
                                log_debug("💰 잔고 (Unified Equity)", f"{usdt_balance:.2f} USDT")
                            return usdt_balance
                    except Exception as e:
                        if show_log:
                            log_debug("⚠️ USDT 파싱 오류", str(e))
        except Exception as e:
            if show_log:
                log_debug("⚠️ Unified API 오류", str(e))
        
        # 2. Futures Account 시도
        try:
            account = api.list_futures_accounts(settle=SETTLE)
            if account:
                available = float(getattr(account, "available", "0"))
                if available > 0:
                    if show_log:
                        log_debug("💰 잔고 (Futures)", f"{available:.2f} USDT")
                    return available
                total = float(getattr(account, "total", "0"))
                if total > 0:
                    if show_log:
                        log_debug("💰 잔고 (Futures Total)", f"{total:.2f} USDT")
                    return total
        except Exception as e:
            if show_log:
                log_debug("❌ Futures API 오류", str(e))
        
        if show_log:
            log_debug("⚠️ 잔고 0", "모든 API에서 잔고를 찾을 수 없음")
        return 0.0
    except Exception as e:
        if show_log:
            log_debug("❌ 잔고 조회 실패", str(e), exc_info=True)
        return 0.0


def calculate_grid_qty(current_price):
    """그리드 수량 계산 (OBV MACD 기반) - 초기 자본금 고정"""
    try:
        if INITIAL_BALANCE <= 0:
            return 1
        
        obv_macd = calculate_obv_macd("ETH_USDT")
        abs_val = abs(float(obv_macd))
        
        if abs_val < 20:
            leverage = Decimal("0.5")
        elif abs_val >= 20 and abs_val < 30:
            leverage = Decimal("0.8")
        elif abs_val >= 30 and abs_val < 40:
            leverage = Decimal("1.0")
        elif abs_val >= 40 and abs_val < 50:
            leverage = Decimal("1.2")
        elif abs_val >= 50 and abs_val < 60:
            leverage = Decimal("1.4")
        elif abs_val >= 60 and abs_val < 70:
            leverage = Decimal("1.6")
        elif abs_val >= 70 and abs_val < 80:
            leverage = Decimal("1.8")
        elif abs_val >= 80 and abs_val < 90:
            leverage = Decimal("2.0")
        elif abs_val >= 90 and abs_val < 100:
            leverage = Decimal("2.2")
        elif abs_val >= 100 and abs_val < 110:
            leverage = Decimal("2.4")
        else:
            leverage = Decimal("3.0")
        
        qty = int((INITIAL_BALANCE * leverage) / (current_price * CONTRACT_SIZE))
        
        return max(1, qty)
    except Exception as e:
        log_debug("❌ 그리드 수량 계산 오류", str(e))
        return 1


def calculate_position_value(qty, price):
    """포지션 가치 계산 (USDT)"""
    return qty * price * CONTRACT_SIZE


def calculate_capital_usage_pct(symbol):
    """자본금 사용률 계산 (%)"""
    try:
        pos = position_state.get(symbol, {})
        long_size = pos.get("long", {}).get("size", Decimal("0"))
        long_price = pos.get("long", {}).get("price", Decimal("0"))
        short_size = pos.get("short", {}).get("size", Decimal("0"))
        short_price = pos.get("short", {}).get("price", Decimal("0"))
        
        long_value = calculate_position_value(long_size, long_price)
        short_value = calculate_position_value(short_size, short_price)
        total_value = long_value + short_value
        
        if INITIAL_BALANCE > 0:
            usage_pct = (total_value / INITIAL_BALANCE) * Decimal("100")
            return float(usage_pct)
        return 0.0
    except Exception as e:
        log_debug("❌ 자본금 사용률 계산 오류", str(e))
        return 0.0


# =============================================================================
# 진입 기록 관리
# =============================================================================

def record_entry(symbol, side, price, qty):
    """진입 기록"""
    if symbol not in entry_history:
        entry_history[symbol] = {"long": [], "short": []}
    
    entry_history[symbol][side].append({
        "price": Decimal(str(price)),
        "qty": Decimal(str(qty)),
        "timestamp": time.time()
    })
    
    usage_pct = calculate_capital_usage_pct(symbol)
    position_value = calculate_position_value(Decimal(str(qty)), Decimal(str(price)))
    
    log_debug("📝 진입 기록", 
             f"{symbol}_{side} {qty}계약 @ {price} | "
             f"포지션가치: {float(position_value):.2f} USDT | "
             f"자본금사용률: {usage_pct:.1f}%")


def classify_positions(symbol, side):
    """포지션을 기본/초과로 분류 (10배 임계값)"""
    try:
        threshold_value = INITIAL_BALANCE * THRESHOLD_RATIO
        
        pos = position_state.get(symbol, {}).get(side, {})
        total_size = pos.get("size", Decimal("0"))
        avg_price = pos.get("price", Decimal("0"))
        
        if total_size <= 0:
            return {"base": [], "overflow": []}
        
        entries = entry_history.get(symbol, {}).get(side, [])
        
        if not entries:
            total_value = calculate_position_value(total_size, avg_price)
            
            if total_value <= threshold_value:
                return {
                    "base": [{"qty": total_size, "price": avg_price, "timestamp": time.time()}],
                    "overflow": []
                }
            else:
                base_qty = int(threshold_value / (avg_price * CONTRACT_SIZE))
                overflow_qty = total_size - base_qty
                
                return {
                    "base": [{"qty": base_qty, "price": avg_price, "timestamp": time.time()}] if base_qty > 0 else [],
                    "overflow": [{"qty": overflow_qty, "price": avg_price, "timestamp": time.time()}] if overflow_qty > 0 else []
                }
        
        base_positions = []
        overflow_positions = []
        accumulated_value = Decimal("0")
        
        for entry in entries:
            entry_qty = entry["qty"]
            entry_price = entry["price"]
            entry_value = calculate_position_value(entry_qty, entry_price)
            
            if accumulated_value + entry_value <= threshold_value:
                base_positions.append(entry)
                accumulated_value += entry_value
            else:
                overflow_positions.append(entry)
        
        recorded_qty = sum(p["qty"] for p in base_positions) + sum(p["qty"] for p in overflow_positions)
        
        if recorded_qty < total_size:
            missing_qty = total_size - recorded_qty
            missing_value = calculate_position_value(missing_qty, avg_price)
            
            if accumulated_value + missing_value <= threshold_value:
                base_positions.append({"qty": missing_qty, "price": avg_price, "timestamp": time.time()})
            else:
                remaining_base_value = threshold_value - accumulated_value
                if remaining_base_value > 0:
                    base_add_qty = int(remaining_base_value / (avg_price * CONTRACT_SIZE))
                    overflow_add_qty = missing_qty - base_add_qty
                    
                    if base_add_qty > 0:
                        base_positions.append({"qty": base_add_qty, "price": avg_price, "timestamp": time.time()})
                    if overflow_add_qty > 0:
                        overflow_positions.append({"qty": overflow_add_qty, "price": avg_price, "timestamp": time.time()})
                else:
                    overflow_positions.append({"qty": missing_qty, "price": avg_price, "timestamp": time.time()})
        
        return {
            "base": base_positions,
            "overflow": overflow_positions
        }
        
    except Exception as e:
        log_debug("❌ 포지션 분류 오류", str(e), exc_info=True)
        return {"base": [], "overflow": []}


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


def cancel_open_orders(symbol):
    """미체결 주문 취소"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
        for order in orders:
            try:
                api.cancel_futures_order(SETTLE, order.id)
            except:
                pass
    except:
        pass


# =============================================================================
# 그리드 주문 (⭐ 수정: 기준 가격 지정 가능)
# =============================================================================

def initialize_hedge_orders(base_price=None):
    """ETH 역방향 그리드 주문 초기화 (마지막 체결가 기준)"""
    try:
        symbol = "ETH_USDT"
        
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker or len(ticker) == 0:
            return
        
        # ⭐ 기준 가격 결정
        global last_long_fill_price, last_short_fill_price
        
        if base_price is not None:
            # 명시적으로 지정된 기준가
            current_price = Decimal(str(base_price))
            log_debug("🎯 그리드 기준가", f"지정 체결가: {current_price:.2f}")
        elif last_long_fill_price is not None or last_short_fill_price is not None:
            # 마지막 체결가 우선
            if last_long_fill_price is not None and last_short_fill_price is not None:
                current_price = max(last_long_fill_price, last_short_fill_price)
            elif last_long_fill_price is not None:
                current_price = last_long_fill_price
            else:
                current_price = last_short_fill_price
            log_debug("🎯 그리드 기준가", f"마지막 체결가: {current_price:.2f}")
        else:
            # 초기 실행 시 현재가
            current_price = Decimal(str(ticker[0].last))
            log_debug("🎯 그리드 기준가", f"현재 시장가: {current_price:.2f}")
        
        obv_macd = calculate_obv_macd(symbol)
        
        upper_price = float(current_price * (Decimal("1") + GRID_GAP_PCT))
        lower_price = float(current_price * (Decimal("1") - GRID_GAP_PCT))
        
        cancel_open_orders(symbol)
        time.sleep(0.5)
        
        if obv_macd >= 0:
            short_qty = calculate_grid_qty(current_price)
            long_qty = int((INITIAL_BALANCE * Decimal("0.5")) / (current_price * CONTRACT_SIZE))
            long_qty = max(1, long_qty)
        else:
            long_qty = calculate_grid_qty(current_price)
            short_qty = int((INITIAL_BALANCE * Decimal("0.5")) / (current_price * CONTRACT_SIZE))
            short_qty = max(1, short_qty)
        
        # 위쪽 숏 주문
        try:
            order = FuturesOrder(
                contract=symbol,
                size=-short_qty,
                price=str(round(upper_price, 2)),
                tif="gtc"
            )
            api.create_futures_order(SETTLE, order)
        except Exception as e:
            log_debug("❌ 숏 주문 실패", str(e))
        
        # 아래쪽 롱 주문
        try:
            order = FuturesOrder(
                contract=symbol,
                size=long_qty,
                price=str(round(lower_price, 2)),
                tif="gtc"
            )
            api.create_futures_order(SETTLE, order)
        except Exception as e:
            log_debug("❌ 롱 주문 실패", str(e))
        
        log_debug("🎯 역방향 그리드 초기화", 
                 f"ETH 위숏:{short_qty}@{upper_price:.2f} 아래롱:{long_qty}@{lower_price:.2f} | "
                 f"기준가:{current_price:.2f} | OBV:{float(obv_macd):.2f} {'(롱강세→숏주력)' if obv_macd >= 0 else '(숏강세→롱주력)'}")
        
    except Exception as e:
        log_debug("❌ 그리드 초기화 실패", str(e), exc_info=True)


# =============================================================================
# 체결 모니터링 (⭐ 수정: 체결 시 즉시 그리드 재생성)
# =============================================================================

def eth_hedge_fill_monitor():
    """ETH 체결 감지 및 역방향 헤징 + 진입 기록"""
    global last_long_fill_price, last_short_fill_price
    prev_long_size = Decimal("0")
    prev_short_size = Decimal("0")
    last_action_time = 0
    
    while True:
        time.sleep(2)
        update_position_state("ETH_USDT")
        
        with position_lock:
            pos = position_state.get("ETH_USDT", {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            long_price = pos.get("long", {}).get("price", Decimal("0"))
            short_price = pos.get("short", {}).get("price", Decimal("0"))
            
            now = time.time()
            
            try:
                ticker = api.list_futures_tickers(SETTLE, contract="ETH_USDT")
                current_price = Decimal(str(ticker[0].last)) if ticker else Decimal("0")
            except:
                current_price = Decimal("0")
            
            hedge_qty = int((INITIAL_BALANCE * Decimal("0.5")) / (current_price * CONTRACT_SIZE))
            hedge_qty = max(1, hedge_qty)
            
            # 롱 체결 시
            if long_size > prev_long_size and now - last_action_time >= 10:
                current_balance = get_available_balance(show_log=True)
                added_long = long_size - prev_long_size
                
                usage_pct = calculate_capital_usage_pct("ETH_USDT")
                long_value = calculate_position_value(long_size, long_price)
                
                classified = classify_positions("ETH_USDT", "long")
                base_qty = sum(p["qty"] for p in classified["base"])
                overflow_qty = sum(p["qty"] for p in classified["overflow"])
                
                log_debug("📊 롱 체결", 
                         f"ETH @ {long_price} +{added_long}계약 (총 {long_size}계약) | "
                         f"포지션가치: {float(long_value):.2f} USDT | "
                         f"자본금사용률: {usage_pct:.1f}% | "
                         f"기본/초과: {base_qty}/{overflow_qty}계약")
                
                record_entry("ETH_USDT", "long", long_price, added_long)
                
                # ⭐ 마지막 체결가 기록
                last_long_fill_price = long_price
                
                prev_long_size = long_size
                prev_short_size = short_size
                last_action_time = now
                
                if hedge_qty >= 1:
                    try:
                        order = FuturesOrder(contract="ETH_USDT", size=-int(hedge_qty), price="0", tif="ioc")
                        api.create_futures_order(SETTLE, order)
                        log_debug("🔄 숏 헤징 (0.5배 고정)", f"{hedge_qty}계약")
                        time.sleep(1)
                        update_position_state("ETH_USDT")
                        pos = position_state.get("ETH_USDT", {})
                        prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                        prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                    except Exception as e:
                        log_debug("❌ 헤징 실패", str(e))
                
                # ⭐ 체결 후 즉시 그리드 재생성 (체결가 기준)
                time.sleep(2)
                cancel_open_orders("ETH_USDT")
                time.sleep(1)
                initialize_hedge_orders(last_long_fill_price)
            
            # 숏 체결 시
            elif short_size > prev_short_size and now - last_action_time >= 10:
                current_balance = get_available_balance(show_log=True)
                added_short = short_size - prev_short_size
                
                usage_pct = calculate_capital_usage_pct("ETH_USDT")
                short_value = calculate_position_value(short_size, short_price)
                
                classified = classify_positions("ETH_USDT", "short")
                base_qty = sum(p["qty"] for p in classified["base"])
                overflow_qty = sum(p["qty"] for p in classified["overflow"])
                
                log_debug("📊 숏 체결", 
                         f"ETH @ {short_price} +{added_short}계약 (총 {short_size}계약) | "
                         f"포지션가치: {float(short_value):.2f} USDT | "
                         f"자본금사용률: {usage_pct:.1f}% | "
                         f"기본/초과: {base_qty}/{overflow_qty}계약")
                
                record_entry("ETH_USDT", "short", short_price, added_short)
                
                # ⭐ 마지막 체결가 기록
                last_short_fill_price = short_price
                
                prev_long_size = long_size
                prev_short_size = short_size
                last_action_time = now
                
                if hedge_qty >= 1:
                    try:
                        order = FuturesOrder(contract="ETH_USDT", size=int(hedge_qty), price="0", tif="ioc")
                        api.create_futures_order(SETTLE, order)
                        log_debug("🔄 롱 헤징 (0.5배 고정)", f"{hedge_qty}계약")
                        time.sleep(1)
                        update_position_state("ETH_USDT")
                        pos = position_state.get("ETH_USDT", {})
                        prev_long_size = pos.get("long", {}).get("size", Decimal("0"))
                        prev_short_size = pos.get("short", {}).get("size", Decimal("0"))
                    except Exception as e:
                        log_debug("❌ 헤징 실패", str(e))
                
                # ⭐ 체결 후 즉시 그리드 재생성 (체결가 기준)
                time.sleep(2)
                cancel_open_orders("ETH_USDT")
                time.sleep(1)
                initialize_hedge_orders(last_short_fill_price)


# =============================================================================
# 듀얼 TP 모니터링 (⭐ 수정: 청산 시 그리드 재생성 제거)
# =============================================================================

def eth_hedge_tp_monitor():
    """⭐ ETH TP 모니터링 (일반 TP 우선, 주력 방향만 듀얼 TP)"""
    while True:
        time.sleep(1)
        
        try:
            ticker = api.list_futures_tickers(SETTLE, contract="ETH_USDT")
            if not ticker:
                continue
            
            current_price = Decimal(str(ticker[0].last))
            primary_direction = get_primary_direction()
            
            with position_lock:
                pos = position_state.get("ETH_USDT", {})
                
                # ==================== 롱 포지션 ====================
                long_size = pos.get("long", {}).get("size", Decimal("0"))
                long_price = pos.get("long", {}).get("price", Decimal("0"))
                
                if long_size > 0 and long_price > 0:
                    # ⭐ 1순위: 일반 TP 체크
                    normal_tp_price = long_price * (Decimal("1") + TP_GAP_PCT)
                    
                    if current_price >= normal_tp_price:
                        long_value = calculate_position_value(long_size, long_price)
                        
                        log_debug("🎯 일반 롱 TP 도달", 
                                f"{long_size}계약 평단:{long_price:.2f} TP:{normal_tp_price:.2f} | "
                                f"포지션가치: {float(long_value):.2f} USDT")
                        
                        try:
                            order = FuturesOrder(
                                contract="ETH_USDT",
                                size=-int(long_size),
                                price="0",
                                tif="ioc",
                                reduce_only=True
                            )
                            result = api.create_futures_order(SETTLE, order)
                            
                            if result:
                                log_debug("✅ 일반 롱 청산", f"{long_size}계약 @ {current_price:.2f}")
                                
                                if "ETH_USDT" in entry_history and "long" in entry_history["ETH_USDT"]:
                                    entry_history["ETH_USDT"]["long"] = []
                                
                                # ⭐ 청산 시 그리드 재생성 없음 (체결 시에만 생성)
                                update_position_state("ETH_USDT")
                                continue
                                
                        except Exception as e:
                            log_debug("❌ 일반 롱 청산 오류", str(e))
                    
                    # ⭐ 2순위: 듀얼 TP (롱이 주력일 때만)
                    elif primary_direction == "long":
                        classified = classify_positions("ETH_USDT", "long")
                        base_positions = classified["base"]
                        overflow_positions = classified["overflow"]
                        
                        # 기본 포지션 TP
                        if base_positions:
                            base_total_qty = sum(p["qty"] for p in base_positions)
                            base_avg_price = sum(p["qty"] * p["price"] for p in base_positions) / base_total_qty
                            base_tp_price = base_avg_price * (Decimal("1") + TP_GAP_PCT)
                            
                            if current_price >= base_tp_price:
                                base_value = calculate_position_value(base_total_qty, base_avg_price)
                                
                                log_debug("🎯 기본 롱 TP 도달", 
                                        f"{base_total_qty}계약 평단:{base_avg_price:.2f} TP:{base_tp_price:.2f} | "
                                        f"포지션가치: {float(base_value):.2f} USDT")
                                
                                try:
                                    order = FuturesOrder(
                                        contract="ETH_USDT",
                                        size=-int(base_total_qty),
                                        price="0",
                                        tif="ioc",
                                        reduce_only=True
                                    )
                                    result = api.create_futures_order(SETTLE, order)
                                    
                                    if result:
                                        log_debug("✅ 기본 롱 청산", f"{base_total_qty}계약 @ {current_price:.2f}")
                                        
                                        time.sleep(1)
                                        update_position_state("ETH_USDT")
                                        
                                        if "ETH_USDT" in entry_history and "long" in entry_history["ETH_USDT"]:
                                            entry_history["ETH_USDT"]["long"] = [
                                                e for e in entry_history["ETH_USDT"]["long"] 
                                                if e not in base_positions
                                            ]
                                        
                                        pos_after = position_state.get("ETH_USDT", {})
                                        long_size_after = pos_after.get("long", {}).get("size", Decimal("0"))
                                        
                                        if long_size_after > 0:
                                            classified_after = classify_positions("ETH_USDT", "long")
                                            base_after = sum(p["qty"] for p in classified_after["base"])
                                            overflow_after = sum(p["qty"] for p in classified_after["overflow"])
                                            log_debug("📊 청산 후 재분류", f"남은 롱: {long_size_after}계약 | 기본/초과: {base_after}/{overflow_after}계약")
                                        
                                        # ⭐ 청산 시 그리드 재생성 없음
                                        update_position_state("ETH_USDT")
                                        continue
                                        
                                except Exception as e:
                                    log_debug("❌ 기본 롱 청산 오류", str(e))
                        
                        # 초과 포지션 TP
                        for overflow_pos in overflow_positions[:]:
                            overflow_qty = overflow_pos["qty"]
                            overflow_price = overflow_pos["price"]
                            overflow_tp_price = overflow_price * (Decimal("1") + TP_GAP_PCT)
                            
                            if current_price >= overflow_tp_price:
                                overflow_value = calculate_position_value(overflow_qty, overflow_price)
                                
                                log_debug("🎯 초과 롱 TP 도달", 
                                        f"{overflow_qty}계약 진입:{overflow_price:.2f} TP:{overflow_tp_price:.2f} | "
                                        f"포지션가치: {float(overflow_value):.2f} USDT")
                                
                                try:
                                    order = FuturesOrder(
                                        contract="ETH_USDT",
                                        size=-int(overflow_qty),
                                        price="0",
                                        tif="ioc",
                                        reduce_only=True
                                    )
                                    result = api.create_futures_order(SETTLE, order)
                                    
                                    if result:
                                        log_debug("✅ 초과 롱 청산", f"{overflow_qty}계약 @ {current_price:.2f}")
                                        
                                        time.sleep(1)
                                        update_position_state("ETH_USDT")
                                        
                                        if "ETH_USDT" in entry_history and "long" in entry_history["ETH_USDT"]:
                                            entries = entry_history["ETH_USDT"]["long"]
                                            if overflow_pos in entries:
                                                entries.remove(overflow_pos)
                                        
                                        pos_after = position_state.get("ETH_USDT", {})
                                        long_size_after = pos_after.get("long", {}).get("size", Decimal("0"))
                                        
                                        if long_size_after > 0:
                                            classified_after = classify_positions("ETH_USDT", "long")
                                            base_after = sum(p["qty"] for p in classified_after["base"])
                                            overflow_after = sum(p["qty"] for p in classified_after["overflow"])
                                            log_debug("📊 청산 후 재분류", f"남은 롱: {long_size_after}계약 | 기본/초과: {base_after}/{overflow_after}계약")
                                        
                                except Exception as e:
                                    log_debug("❌ 초과 롱 청산 오류", str(e))
                    
                    # ⭐ 헤징 방향 (롱이 헤징일 때)
                    elif primary_direction == "short":
                        long_tp_price = long_price * (Decimal("1") + TP_GAP_PCT)
                        
                        if current_price >= long_tp_price:
                            long_value = calculate_position_value(long_size, long_price)
                            
                            log_debug("🎯 헤징 롱 TP 도달", 
                                    f"{long_size}계약 진입:{long_price:.2f} TP:{long_tp_price:.2f} | "
                                    f"포지션가치: {float(long_value):.2f} USDT")
                            
                            try:
                                order = FuturesOrder(
                                    contract="ETH_USDT",
                                    size=-int(long_size),
                                    price="0",
                                    tif="ioc",
                                    reduce_only=True
                                )
                                result = api.create_futures_order(SETTLE, order)
                                
                                if result:
                                    log_debug("✅ 헤징 롱 청산", f"{long_size}계약 @ {current_price:.2f}")
                                    
                                    if "ETH_USDT" in entry_history and "long" in entry_history["ETH_USDT"]:
                                        entry_history["ETH_USDT"]["long"] = []
                                    
                                    # ⭐ 청산 시 그리드 재생성 없음
                                    update_position_state("ETH_USDT")
                                    continue
                                    
                            except Exception as e:
                                log_debug("❌ 헤징 롱 청산 오류", str(e))
                
                # ==================== 숏 포지션 ====================
                short_size = pos.get("short", {}).get("size", Decimal("0"))
                short_price = pos.get("short", {}).get("price", Decimal("0"))
                
                if short_size > 0 and short_price > 0:
                    # ⭐ 1순위: 일반 TP 체크
                    normal_tp_price = short_price * (Decimal("1") - TP_GAP_PCT)
                    
                    if current_price <= normal_tp_price:
                        short_value = calculate_position_value(short_size, short_price)
                        
                        log_debug("🎯 일반 숏 TP 도달", 
                                f"{short_size}계약 평단:{short_price:.2f} TP:{normal_tp_price:.2f} | "
                                f"포지션가치: {float(short_value):.2f} USDT")
                        
                        try:
                            order = FuturesOrder(
                                contract="ETH_USDT",
                                size=int(short_size),
                                price="0",
                                tif="ioc",
                                reduce_only=True
                            )
                            result = api.create_futures_order(SETTLE, order)
                            
                            if result:
                                log_debug("✅ 일반 숏 청산", f"{short_size}계약 @ {current_price:.2f}")
                                
                                if "ETH_USDT" in entry_history and "short" in entry_history["ETH_USDT"]:
                                    entry_history["ETH_USDT"]["short"] = []
                                
                                # ⭐ 청산 시 그리드 재생성 없음
                                update_position_state("ETH_USDT")
                                continue
                                
                        except Exception as e:
                            log_debug("❌ 일반 숏 청산 오류", str(e))
                    
                    # ⭐ 2순위: 듀얼 TP (숏이 주력일 때)
                    elif primary_direction == "short":
                        classified = classify_positions("ETH_USDT", "short")
                        base_positions = classified["base"]
                        overflow_positions = classified["overflow"]
                        
                        # 기본 포지션 TP
                        if base_positions:
                            base_total_qty = sum(p["qty"] for p in base_positions)
                            base_avg_price = sum(p["qty"] * p["price"] for p in base_positions) / base_total_qty
                            base_tp_price = base_avg_price * (Decimal("1") - TP_GAP_PCT)
                            
                            if current_price <= base_tp_price:
                                base_value = calculate_position_value(base_total_qty, base_avg_price)
                                
                                log_debug("🎯 기본 숏 TP 도달", 
                                        f"{base_total_qty}계약 평단:{base_avg_price:.2f} TP:{base_tp_price:.2f} | "
                                        f"포지션가치: {float(base_value):.2f} USDT")
                                
                                try:
                                    order = FuturesOrder(
                                        contract="ETH_USDT",
                                        size=int(base_total_qty),
                                        price="0",
                                        tif="ioc",
                                        reduce_only=True
                                    )
                                    result = api.create_futures_order(SETTLE, order)
                                    
                                    if result:
                                        log_debug("✅ 기본 숏 청산", f"{base_total_qty}계약 @ {current_price:.2f}")
                                        
                                        time.sleep(1)
                                        update_position_state("ETH_USDT")
                                        
                                        if "ETH_USDT" in entry_history and "short" in entry_history["ETH_USDT"]:
                                            entry_history["ETH_USDT"]["short"] = [
                                                e for e in entry_history["ETH_USDT"]["short"] 
                                                if e not in base_positions
                                            ]
                                        
                                        pos_after = position_state.get("ETH_USDT", {})
                                        short_size_after = pos_after.get("short", {}).get("size", Decimal("0"))
                                        
                                        if short_size_after > 0:
                                            classified_after = classify_positions("ETH_USDT", "short")
                                            base_after = sum(p["qty"] for p in classified_after["base"])
                                            overflow_after = sum(p["qty"] for p in classified_after["overflow"])
                                            log_debug("📊 청산 후 재분류", f"남은 숏: {short_size_after}계약 | 기본/초과: {base_after}/{overflow_after}계약")
                                        
                                        # ⭐ 청산 시 그리드 재생성 없음
                                        update_position_state("ETH_USDT")
                                        continue
                                        
                                except Exception as e:
                                    log_debug("❌ 기본 숏 청산 오류", str(e))
                        
                        # 초과 포지션 TP
                        for overflow_pos in overflow_positions[:]:
                            overflow_qty = overflow_pos["qty"]
                            overflow_price = overflow_pos["price"]
                            overflow_tp_price = overflow_price * (Decimal("1") - TP_GAP_PCT)
                            
                            if current_price <= overflow_tp_price:
                                overflow_value = calculate_position_value(overflow_qty, overflow_price)
                                
                                log_debug("🎯 초과 숏 TP 도달", 
                                        f"{overflow_qty}계약 진입:{overflow_price:.2f} TP:{overflow_tp_price:.2f} | "
                                        f"포지션가치: {float(overflow_value):.2f} USDT")
                                
                                try:
                                    order = FuturesOrder(
                                        contract="ETH_USDT",
                                        size=int(overflow_qty),
                                        price="0",
                                        tif="ioc",
                                        reduce_only=True
                                    )
                                    result = api.create_futures_order(SETTLE, order)
                                    
                                    if result:
                                        log_debug("✅ 초과 숏 청산", f"{overflow_qty}계약 @ {current_price:.2f}")
                                        
                                        time.sleep(1)
                                        update_position_state("ETH_USDT")
                                        
                                        if "ETH_USDT" in entry_history and "short" in entry_history["ETH_USDT"]:
                                            entries = entry_history["ETH_USDT"]["short"]
                                            if overflow_pos in entries:
                                                entries.remove(overflow_pos)
                                        
                                        pos_after = position_state.get("ETH_USDT", {})
                                        short_size_after = pos_after.get("short", {}).get("size", Decimal("0"))
                                        
                                        if short_size_after > 0:
                                            classified_after = classify_positions("ETH_USDT", "short")
                                            base_after = sum(p["qty"] for p in classified_after["base"])
                                            overflow_after = sum(p["qty"] for p in classified_after["overflow"])
                                            log_debug("📊 청산 후 재분류", f"남은 숏: {short_size_after}계약 | 기본/초과: {base_after}/{overflow_after}계약")
                                        
                                except Exception as e:
                                    log_debug("❌ 초과 숏 청산 오류", str(e))
                    
                    # ⭐ 헤징 방향 (숏이 헤징일 때)
                    elif primary_direction == "long":
                        short_tp_price = short_price * (Decimal("1") - TP_GAP_PCT)
                        
                        if current_price <= short_tp_price:
                            short_value = calculate_position_value(short_size, short_price)
                            
                            log_debug("🎯 헤징 숏 TP 도달", 
                                    f"{short_size}계약 진입:{short_price:.2f} TP:{short_tp_price:.2f} | "
                                    f"포지션가치: {float(short_value):.2f} USDT")
                            
                            try:
                                order = FuturesOrder(
                                    contract="ETH_USDT",
                                    size=int(short_size),
                                    price="0",
                                    tif="ioc",
                                    reduce_only=True
                                )
                                result = api.create_futures_order(SETTLE, order)
                                
                                if result:
                                    log_debug("✅ 헤징 숏 청산", f"{short_size}계약 @ {current_price:.2f}")
                                    
                                    if "ETH_USDT" in entry_history and "short" in entry_history["ETH_USDT"]:
                                        entry_history["ETH_USDT"]["short"] = []
                                    
                                    # ⭐ 청산 시 그리드 재생성 없음
                                    update_position_state("ETH_USDT")
                                    continue
                                    
                            except Exception as e:
                                log_debug("❌ 헤징 숏 청산 오류", str(e))
        
        except Exception as e:
            log_debug("❌ TP 모니터 오류", str(e), exc_info=True)
            time.sleep(5)


# =============================================================================
# 가격 모니터링 (WebSocket)
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
                    "payload": ["ETH_USDT"]
                }
                await ws.send(json.dumps(subscribe_msg))
                log_debug("🔗 WebSocket 연결", "ETH_USDT")
                
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    
                    if data.get("event") == "update" and data.get("channel") == "futures.tickers":
                        result = data.get("result")
                        if result and isinstance(result, dict):
                            price = Decimal(str(result.get("last", "0")))
                            if price > 0:
                                latest_prices["ETH_USDT"] = price
                    
        except Exception as e:
            log_debug("❌ WebSocket 오류", str(e))
            await asyncio.sleep(5)


# =============================================================================
# 웹 API
# =============================================================================

@app.route("/ping", methods=["GET", "POST"])
def ping():
    """헬스체크"""
    return jsonify({"status": "ok", "time": time.time()})


# =============================================================================
# 메인
# =============================================================================

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "v14.0-grid-fill-based")
    
    INITIAL_BALANCE = Decimal(str(get_available_balance(show_log=True)))
    log_debug("💰 초기 잔고", f"{INITIAL_BALANCE:.2f} USDT")
    log_debug("🎯 임계값", f"{float(INITIAL_BALANCE * THRESHOLD_RATIO):.2f} USDT ({int(THRESHOLD_RATIO)}배)")
    
    entry_history["ETH_USDT"] = {"long": [], "short": []}
    
    obv_macd_val = calculate_obv_macd("ETH_USDT")
    log_debug("📊 OBV MACD", f"ETH_USDT: {obv_macd_val:.2f}")
    
    # 초기 그리드 생성
    initialize_hedge_orders()

    # 스레드 시작
    threading.Thread(target=eth_hedge_fill_monitor, daemon=True).start()
    threading.Thread(target=eth_hedge_tp_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()

    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
