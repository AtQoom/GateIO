#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ONDO 역방향 그리드 매매 시스템 v20.0-FINAL
- 복리 자동화: 1시간마다 실제 잔고 업데이트
- 환경변수 기반 설정 (속도/안정성 극대화)
- 수량 계산: 레버리지 1배 기준
- OBV MACD 가중 수량 (0.10~0.35)
- 그리드/TP 간격 0.12%
- 헤징 0.1배
- 임계값 80%
- ⭐ 임계값 초과 후: 역방향 주력 30%, 주력 개별 TP 시 역방향 20% 동반 청산
- ⭐ 로그 최적화: 중요한 이벤트만 출력
- ⭐ 중복 제거 및 논리 충돌 해결
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
# 환경 변수 설정
# =============================================================================
API_KEY = os.environ.get("GATE_API_KEY")
API_SECRET = os.environ.get("GATE_API_SECRET")
SYMBOL = os.environ.get("SYMBOL", "ONDO_USDT")
SETTLE = "usdt"

GRID_GAP_PCT = Decimal(os.environ.get("GRID_GAP_PCT", "0.12")) / Decimal("100")
TP_GAP_PCT = Decimal(os.environ.get("TP_GAP_PCT", "0.12")) / Decimal("100")
HEDGE_RATIO = Decimal(os.environ.get("HEDGE_RATIO", "0.1"))
THRESHOLD_RATIO = Decimal(os.environ.get("THRESHOLD_RATIO", "0.8"))
COUNTER_POSITION_RATIO = Decimal("0.30")
COUNTER_CLOSE_RATIO = Decimal("0.20")
MAX_POSITION_RATIO = Decimal("5.0")

# =============================================================================
# API 설정
# =============================================================================
config = Configuration(key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
api = FuturesApi(api_client)
unified_api = UnifiedApi(api_client)

# =============================================================================
# Flask 앱
# =============================================================================
app = Flask(__name__)

# =============================================================================
# 전역 변수 (단일화)
# =============================================================================
INITIAL_BALANCE = Decimal("50")
balance_lock = threading.Lock()
position_lock = threading.Lock()
last_grid_time = 0

position_state = {
    SYMBOL: {
        "long": {"size": Decimal("0"), "price": Decimal("0")},
        "short": {"size": Decimal("0"), "price": Decimal("0")}
    }
}

grid_orders = {SYMBOL: {"long": [], "short": []}}
tp_orders = {SYMBOL: {"long": [], "short": []}}
max_position_locked = {"long": False, "short": False}
post_threshold_entries = {SYMBOL: {"long": [], "short": []}}

# =============================================================================
# 유틸리티 함수
# =============================================================================
def log_debug(tag, msg, exc_info=False):
    """로그 출력 (중요한 것만)"""
    if exc_info:
        logger.info(f"[{tag}] {msg}", exc_info=True)
    else:
        logger.info(f"[{tag}] {msg}")

def update_initial_balance():
    """1시간마다 잔고 업데이트"""
    global INITIAL_BALANCE
    while True:
        try:
            time.sleep(3600)
            accounts = unified_api.list_unified_accounts(currency="USDT")
            if accounts and accounts.total:
                new_balance = Decimal(str(accounts.total))
                with balance_lock:
                    INITIAL_BALANCE = new_balance
                log_debug("💰 잔고 업데이트", f"{new_balance} USDT")
        except Exception as e:
            log_debug("❌ 잔고 오류", str(e))

# =============================================================================
# WebSocket 모니터링
# =============================================================================
async def watch_positions():
    """WebSocket 포지션 모니터링"""
    uri = f"wss://fx-ws.gateio.ws/v4/ws/{SETTLE}"
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                auth_msg = {
                    "time": int(time.time()),
                    "channel": "futures.positions",
                    "event": "subscribe",
                    "payload": [SYMBOL]
                }
                await ws.send(json.dumps(auth_msg))
                log_debug("🔌 WebSocket 연결", "포지션 모니터링 시작")
                
                async for message in ws:
                    data = json.loads(message)
                    
                    if data.get("event") == "update" and data.get("channel") == "futures.positions":
                        result = data.get("result", [])
                        for pos in result:
                            if pos.get("contract") == SYMBOL:
                                size_dec = Decimal(str(pos.get("size", "0")))
                                entry_price = abs(Decimal(str(pos.get("entry_price", "0"))))
                                
                                with position_lock:
                                    if size_dec > 0:
                                        position_state[SYMBOL]["long"]["size"] = size_dec
                                        position_state[SYMBOL]["long"]["price"] = entry_price
                                    elif size_dec < 0:
                                        position_state[SYMBOL]["short"]["size"] = abs(size_dec)
                                        position_state[SYMBOL]["short"]["price"] = entry_price
                                    else:
                                        position_state[SYMBOL]["long"]["size"] = Decimal("0")
                                        position_state[SYMBOL]["short"]["size"] = Decimal("0")
                        
        except Exception as e:
            log_debug("❌ WebSocket 오류", str(e))
            await asyncio.sleep(5)

def start_websocket():
    """WebSocket 스레드 시작"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(watch_positions())

# =============================================================================
# 포지션/주문 관리 함수
# =============================================================================
def sync_position_from_api(symbol):
    """API에서 포지션 강제 동기화"""
    try:
        positions = api.list_positions(SETTLE)
        
        with position_lock:
            position_state[symbol]["long"]["size"] = Decimal("0")
            position_state[symbol]["long"]["price"] = Decimal("0")
            position_state[symbol]["short"]["size"] = Decimal("0")
            position_state[symbol]["short"]["price"] = Decimal("0")
            
            if positions:
                for p in positions:
                    if p.contract == symbol:
                        size_dec = Decimal(str(p.size))
                        entry_price = abs(Decimal(str(p.entry_price)))
                        
                        if size_dec > 0:
                            position_state[symbol]["long"]["size"] = size_dec
                            position_state[symbol]["long"]["price"] = entry_price
                        elif size_dec < 0:
                            position_state[symbol]["short"]["size"] = abs(size_dec)
                            position_state[symbol]["short"]["price"] = entry_price
    except Exception as e:
        log_debug("❌ 동기화 오류", str(e))

def cancel_all_orders(symbol):
    """모든 주문 취소 (그리드 + TP)"""
    try:
        orders = api.list_futures_orders(SETTLE, symbol, status='open')
        for order in orders:
            try:
                api.cancel_futures_order(SETTLE, order.id)
                time.sleep(0.05)
            except:
                pass
        
        if symbol in grid_orders:
            grid_orders[symbol] = {"long": [], "short": []}
        if symbol in tp_orders:
            tp_orders[symbol] = {"long": [], "short": []}
            
    except Exception as e:
        log_debug("❌ 취소 오류", str(e))

def cancel_grid_orders(symbol):
    """그리드 주문만 취소"""
    try:
        orders = api.list_futures_orders(SETTLE, symbol, status='open')
        for order in orders:
            if not order.is_reduce_only:
                try:
                    api.cancel_futures_order(SETTLE, order.id)
                    time.sleep(0.05)
                except:
                    pass
        
        if symbol in grid_orders:
            grid_orders[symbol] = {"long": [], "short": []}
    except:
        pass

def refresh_all_orders(symbol, event_type="manual"):
    """마스터 새로고침: 모든 이벤트마다 호출"""
    log_debug("🔄 새로고침 시작", event_type)
    
    sync_position_from_api(symbol)
    cancel_all_orders(symbol)
    time.sleep(0.5)
    
    ticker = api.list_futures_tickers(SETTLE, contract=symbol)
    if ticker:
        current_price = Decimal(str(ticker[0].last))
        initialize_grid(current_price, skip_check=True)
    
    refresh_tp_orders(symbol)
    log_debug("✅ 새로고침 완료", event_type)

# =============================================================================
# 수량 계산 함수
# =============================================================================
def calculate_grid_qty(obv_macd_weight=None):
    """그리드 수량 계산 (OBV MACD 가중)"""
    try:
        with balance_lock:
            balance = INITIAL_BALANCE
        
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if not ticker:
            return 0
        
        price = Decimal(str(ticker[0].last))
        
        if obv_macd_weight is not None:
            weight = Decimal(str(obv_macd_weight))
        else:
            weight = Decimal("0.20")
        
        qty = round(float((balance * weight) / price))
        return max(1, qty)
        
    except Exception as e:
        log_debug("❌ 수량계산 오류", str(e))
        return 0

# =============================================================================
# 그리드 주문 함수
# =============================================================================
def place_grid_order(symbol, side, price, qty):
    """그리드 주문 생성"""
    try:
        size = qty if side == "long" else -qty
        
        order = FuturesOrder(
            contract=symbol,
            size=size,
            price=str(price),
            tif="gtc",
            reduce_only=False
        )
        
        result = api.create_futures_order(SETTLE, order)
        
        if result and hasattr(result, 'id'):
            if symbol not in grid_orders:
                grid_orders[symbol] = {"long": [], "short": []}
            
            grid_orders[symbol][side].append({
                "order_id": result.id,
                "price": float(price),
                "qty": int(qty)
            })
        
        return result
        
    except Exception as e:
        log_debug(f"❌ 그리드 {side}", str(e))
        return None

def initialize_grid(current_price, skip_check=False):
    """그리드 생성"""
    global last_grid_time
    
    if not skip_check:
        now = time.time()
        if now - last_grid_time < 5:
            return
        last_grid_time = now
    
    try:
        with position_lock:
            pos = position_state.get(SYMBOL, {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            long_price = pos.get("long", {}).get("price", Decimal("0"))
            short_price = pos.get("short", {}).get("price", Decimal("0"))
        
        with balance_lock:
            balance = INITIAL_BALANCE
        
        long_value = long_price * long_size if long_price > 0 else Decimal("0")
        short_value = short_price * short_size if short_price > 0 else Decimal("0")
        threshold = balance * THRESHOLD_RATIO
        
        long_above_threshold = (long_value >= threshold and long_size > 0)
        short_above_threshold = (short_value >= threshold and short_size > 0)
        
        cancel_grid_orders(SYMBOL)
        
        long_grid_price = current_price * (Decimal("1") - GRID_GAP_PCT)
        short_grid_price = current_price * (Decimal("1") + GRID_GAP_PCT)
        
        if long_above_threshold:
            counter_qty = max(1, int(long_size * COUNTER_POSITION_RATIO))
            same_dir_qty = max(1, int(long_size * Decimal("0.10")))
            
            with balance_lock:
                balance_for_hedge = INITIAL_BALANCE
            base_hedge_qty = round(float((balance_for_hedge * HEDGE_RATIO) / current_price))
            same_dir_qty = max(same_dir_qty, base_hedge_qty)
            
            place_grid_order(SYMBOL, "short", short_grid_price, counter_qty)
            place_grid_order(SYMBOL, "long", long_grid_price, same_dir_qty)
            
            log_debug("⚖️ 비대칭 그리드", f"역:{counter_qty} 동:{same_dir_qty}")
            
        elif short_above_threshold:
            counter_qty = max(1, int(short_size * COUNTER_POSITION_RATIO))
            same_dir_qty = max(1, int(short_size * Decimal("0.10")))
            
            with balance_lock:
                balance_for_hedge = INITIAL_BALANCE
            base_hedge_qty = round(float((balance_for_hedge * HEDGE_RATIO) / current_price))
            same_dir_qty = max(same_dir_qty, base_hedge_qty)
            
            place_grid_order(SYMBOL, "long", long_grid_price, counter_qty)
            place_grid_order(SYMBOL, "short", short_grid_price, same_dir_qty)
            
            log_debug("⚖️ 비대칭 그리드", f"역:{counter_qty} 동:{same_dir_qty}")
            
        else:
            qty = calculate_grid_qty()
            place_grid_order(SYMBOL, "long", long_grid_price, qty)
            place_grid_order(SYMBOL, "short", short_grid_price, qty)
            log_debug("📐 대칭 그리드", f"각 {qty}개")
            
    except Exception as e:
        log_debug("❌ 그리드 오류", str(e))


# =============================================================================
# 그리드 체결 모니터링
# =============================================================================
def grid_fill_monitor():
    """그리드 체결 모니터링"""
    while True:
        try:
            time.sleep(0.5)
            
            if SYMBOL not in grid_orders:
                continue
            
            for side in ["long", "short"]:
                filled_orders = []
                
                for order_info in grid_orders[SYMBOL][side]:
                    order_id = order_info.get("order_id")
                    
                    try:
                        order_status = api.get_futures_order(SETTLE, order_id)
                        
                        if order_status.status == 'finished':
                            log_debug("🎉 그리드 체결", f"{side.upper()} 주문 {order_id} 체결")
                            
                            main_side = 'short' if side == 'long' else 'long'
                            
                            with position_lock:
                                main_pos = position_state.get(SYMBOL, {}).get(main_side, {})
                                main_size = main_pos.get('size', Decimal("0"))
                            
                            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                            if ticker and main_size > 0:
                                current_price = Decimal(str(ticker[0].last))
                                
                                qty_10pct = int(main_size * Decimal("0.10"))
                                
                                with balance_lock:
                                    balance = INITIAL_BALANCE
                                qty_hedge_asset = round(float((balance * HEDGE_RATIO) / current_price))
                                
                                hedge_qty = max(qty_10pct, qty_hedge_asset)
                                
                                if hedge_qty > 0:
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
                            
                            time.sleep(0.5)
                            refresh_all_orders(SYMBOL, "그리드_체결_완료")
                            
                            filled_orders.append(order_info)
                            break
                            
                    except GateApiException as e:
                        if "ORDER_NOT_FOUND" in str(e):
                            filled_orders.append(order_info)
                    except:
                        pass
                
                grid_orders[SYMBOL][side] = [o for o in grid_orders[SYMBOL][side] if o not in filled_orders]
                
        except Exception as e:
            log_debug("❌ 그리드모니터 오류", str(e))
            time.sleep(1)

# =============================================================================
# TP 관리 함수
# =============================================================================
def create_tp_order(symbol, side, qty, tp_price, tp_type):
    """TP 주문 생성"""
    try:
        size = qty if side == "long" else -qty
        
        order = FuturesOrder(
            contract=symbol,
            size=-size,
            price=str(tp_price),
            tif="gtc",
            reduce_only=True
        )
        
        result = api.create_futures_order(SETTLE, order)
        
        if result and hasattr(result, 'id'):
            if symbol not in tp_orders:
                tp_orders[symbol] = {"long": [], "short": []}
            
            tp_orders[symbol][side].append({
                "order_id": result.id,
                "qty": int(qty),
                "type": tp_type
            })
        
        log_debug(f"✅ {tp_type} TP", f"{side.upper()} {abs(qty)}개 @{tp_price}")
        
    except Exception as e:
        log_debug(f"❌ {tp_type} TP", str(e))

def create_long_tp_orders(symbol, long_size, long_price, long_value, threshold):
    """롱 TP 생성"""
    tp_price = long_price * (Decimal("1") + TP_GAP_PCT)
    
    if long_value >= threshold:
        entry_qty = int(long_size * Decimal("0.30"))
        average_qty = int(long_size * Decimal("0.70"))
        
        if entry_qty > 0:
            create_tp_order(symbol, "long", entry_qty, tp_price, "individual")
        if average_qty > 0:
            create_tp_order(symbol, "long", average_qty, tp_price, "average")
    else:
        if long_size > 0:
            create_tp_order(symbol, "long", long_size, tp_price, "average")

def create_short_tp_orders(symbol, short_size, short_price, short_value, threshold):
    """숏 TP 생성"""
    tp_price = short_price * (Decimal("1") - TP_GAP_PCT)
    
    if short_value >= threshold:
        entry_qty = int(short_size * Decimal("0.30"))
        average_qty = int(short_size * Decimal("0.70"))
        
        if entry_qty > 0:
            create_tp_order(symbol, "short", entry_qty, tp_price, "individual")
        if average_qty > 0:
            create_tp_order(symbol, "short", average_qty, tp_price, "average")
    else:
        if short_size > 0:
            create_tp_order(symbol, "short", short_size, tp_price, "average")

def refresh_tp_orders(symbol):
    """TP 주문 전체 재생성"""
    if symbol not in tp_orders:
        tp_orders[symbol] = {"long": [], "short": []}
    
    tp_orders[symbol]["long"] = []
    tp_orders[symbol]["short"] = []
    
    try:
        orders = api.list_futures_orders(SETTLE, symbol, status='open')
        for order in orders:
            if order.is_reduce_only:
                try:
                    api.cancel_futures_order(SETTLE, order.id)
                    time.sleep(0.05)
                except:
                    pass
    except:
        pass
    
    with position_lock:
        pos = position_state.get(symbol, {})
        long_size = pos.get("long", {}).get("size", Decimal("0"))
        short_size = pos.get("short", {}).get("size", Decimal("0"))
        long_price = pos.get("long", {}).get("price", Decimal("0"))
        short_price = pos.get("short", {}).get("price", Decimal("0"))
    
    with balance_lock:
        current_balance = INITIAL_BALANCE
    
    long_value = long_price * long_size if long_price > 0 else Decimal("0")
    short_value = short_price * short_size if short_price > 0 else Decimal("0")
    threshold = current_balance * THRESHOLD_RATIO
    
    if long_size > 0:
        create_long_tp_orders(symbol, long_size, long_price, long_value, threshold)
    
    if short_size > 0:
        create_short_tp_orders(symbol, short_size, short_price, short_value, threshold)

def close_counter_on_individual_tp(symbol, main_side, tp_qty):
    """개별 TP 체결 시 역방향 20% 동반 청산"""
    try:
        counter_side = 'short' if main_side == 'long' else 'long'
        
        with position_lock:
            counter_pos = position_state.get(symbol, {}).get(counter_side, {})
            counter_size = counter_pos.get('size', Decimal("0"))
        
        if counter_size <= 0:
            return
        
        close_qty = max(1, int(counter_size * COUNTER_CLOSE_RATIO))
        
        if close_qty <= 0:
            return
        
        order_size = -close_qty if counter_side == 'long' else close_qty
        
        close_order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price="0",
            tif='ioc',
            reduce_only=True
        )
        
        api.create_futures_order(SETTLE, close_order)
        log_debug("🔄 동반 청산", f"{counter_side.upper()} {close_qty}개 (20%)")
        
    except Exception as e:
        log_debug("❌ 동반청산 오류", str(e))

def track_threshold_entries(long_size, short_size, prev_long_size, prev_short_size,
                            long_price, short_price, long_value, short_value, threshold):
    """임계값 초과 진입 추적"""
    long_increased = (long_size > prev_long_size)
    short_increased = (short_size > prev_short_size)
    
    long_above_threshold = (long_value >= threshold and long_size > 0)
    short_above_threshold = (short_value >= threshold and short_size > 0)
    
    if long_increased and long_above_threshold:
        if SYMBOL not in post_threshold_entries:
            post_threshold_entries[SYMBOL] = {"long": [], "short": []}
        
        post_threshold_entries[SYMBOL]["long"].append({
            "size": float(long_size - prev_long_size),
            "price": float(long_price),
            "timestamp": time.time()
        })
        log_debug("📈 임계값 초과 진입", f"롱 {long_size - prev_long_size}개")
    
    if short_increased and short_above_threshold:
        if SYMBOL not in post_threshold_entries:
            post_threshold_entries[SYMBOL] = {"long": [], "short": []}
        
        post_threshold_entries[SYMBOL]["short"].append({
            "size": float(short_size - prev_short_size),
            "price": float(short_price),
            "timestamp": time.time()
        })
        log_debug("📉 임계값 초과 진입", f"숏 {short_size - prev_short_size}개")

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

                                    if tp_type_val == "individual":
                                        close_counter_on_individual_tp(SYMBOL, side, tp_qty)
                                        time.sleep(0.5)
                                    
                                    refresh_all_orders(SYMBOL, "TP_체결")
                                    break
                                    
                                else:
                                    remaining_tps.append(tp_info)
                                    
                            except GateApiException as e:
                                if "not found" in str(e).lower():
                                    continue
                                remaining_tps.append(tp_info)
                            except:
                                remaining_tps.append(tp_info)
                        
                        tp_orders[SYMBOL][side] = remaining_tps
            
            try:
                if long_size == 0 and short_size == 0:
                    continue
                
                orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status="open")
                grid_orders_list = [o for o in orders if not o.is_reduce_only]
                tp_orders_list = [o for o in orders if o.is_reduce_only]
                
                if not tp_orders_list:
                    log_debug("⚠️ 안전장치", "TP 없음 → 재생성")
                    refresh_tp_orders(SYMBOL)
                    time.sleep(0.5)
                
                if not grid_orders_list and not (long_size > 0 and short_size > 0):
                    log_debug("⚠️ 안전장치", "그리드 없음 → 재생성")
                    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
                    if ticker:
                        current_price = Decimal(str(ticker[0].last))
                        initialize_grid(current_price, skip_check=True)
            except:
                pass
            
        except Exception as e:
            log_debug("❌ TP 모니터 오류", str(e))
            time.sleep(1)

# =============================================================================
# 포지션 모니터링
# =============================================================================
def fill_monitor():
    """체결 모니터링 (메인)"""
    global INITIAL_BALANCE
    
    prev_long_size = Decimal("0")
    prev_short_size = Decimal("0")
    
    log_debug("🚀 시작", "체결 모니터링 시작")
    
    while True:
        try:
            time.sleep(0.3)
            
            ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
            if not ticker:
                continue
            current_price = Decimal(str(ticker[0].last))
            
            positions = api.list_positions(SETTLE)
            long_size = Decimal("0")
            short_size = Decimal("0")
            long_price = Decimal("0")
            short_price = Decimal("0")
            
            if positions:
                for p in positions:
                    if p.contract == SYMBOL:
                        size_dec = Decimal(str(p.size))
                        if size_dec > 0:
                            long_size = size_dec
                            long_price = abs(Decimal(str(p.entry_price)))
                        elif size_dec < 0:
                            short_size = abs(size_dec)
                            short_price = abs(Decimal(str(p.entry_price)))
            
            with position_lock:
                position_state[SYMBOL]["long"]["size"] = long_size
                position_state[SYMBOL]["long"]["price"] = long_price
                position_state[SYMBOL]["short"]["size"] = short_size
                position_state[SYMBOL]["short"]["price"] = short_price
            
            with balance_lock:
                current_balance = INITIAL_BALANCE
            
            long_value = long_price * long_size if long_price > 0 else Decimal("0")
            short_value = short_price * short_size if short_price > 0 else Decimal("0")
            threshold = current_balance * THRESHOLD_RATIO
            max_position_value = current_balance * MAX_POSITION_RATIO
            
            if long_value >= max_position_value and not max_position_locked["long"]:
                log_debug("⚠️ 최대 포지션", "롱 500%")
                max_position_locked["long"] = True
                cancel_grid_orders(SYMBOL)
            
            if short_value >= max_position_value and not max_position_locked["short"]:
                log_debug("⚠️ 최대 포지션", "숏 500%")
                max_position_locked["short"] = True
                cancel_grid_orders(SYMBOL)
            
            if long_value < max_position_value and max_position_locked["long"]:
                log_debug("✅ 잠금 해제", "롱")
                max_position_locked["long"] = False
            
            if short_value < max_position_value and max_position_locked["short"]:
                log_debug("✅ 잠금 해제", "숏")
                max_position_locked["short"] = False
            
            if max_position_locked["long"] or max_position_locked["short"]:
                prev_long_size = long_size
                prev_short_size = short_size
                continue
            
            track_threshold_entries(long_size, short_size, prev_long_size, prev_short_size, 
                                   long_price, short_price, long_value, short_value, threshold)
            
            if long_size != prev_long_size or short_size != prev_short_size:
                log_debug("🔄 포지션 변경", 
                         f"롱: {prev_long_size} → {long_size} | 숏: {prev_short_size} → {short_size}")
                
                refresh_all_orders(SYMBOL, "포지션_변경")
                
                prev_long_size = long_size
                prev_short_size = short_size
                
        except Exception as e:
            log_debug("❌ 모니터 오류", str(e))
            time.sleep(1)

# =============================================================================
# Flask 엔드포인트
# =============================================================================
@app.route('/webhook', methods=['POST'])
def webhook():
    """TradingView 웹훅"""
    try:
        data = request.get_json()
        action = data.get('action', '')
        
        log_debug("📨 웹훅 수신", f"Action: {action}")
        
        return jsonify({"status": "success", "action": action}), 200
        
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/status', methods=['GET'])
def status():
    """상태 확인"""
    try:
        with position_lock:
            pos = position_state.get(SYMBOL, {})
            long_size = float(pos.get("long", {}).get("size", 0))
            short_size = float(pos.get("short", {}).get("size", 0))
            long_price = float(pos.get("long", {}).get("price", 0))
            short_price = float(pos.get("short", {}).get("price", 0))
        
        with balance_lock:
            balance = float(INITIAL_BALANCE)
        
        return jsonify({
            "status": "running",
            "symbol": SYMBOL,
            "balance": balance,
            "position": {
                "long": {"size": long_size, "price": long_price},
                "short": {"size": short_size, "price": short_price}
            }
        }), 200
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# =============================================================================
# 메인 실행
# =============================================================================
if __name__ == '__main__':
    log_debug("=" * 80, "")
    log_debug("🚀 시스템 시작", "ONDO 역방향 그리드 매매 v20.0-FINAL")
    log_debug("=" * 80, "")
    
    # 초기 잔고 조회
    try:
        accounts = unified_api.list_unified_accounts(currency="USDT")
        if accounts and accounts.total:
            INITIAL_BALANCE = Decimal(str(accounts.total))
            log_debug("💰 초기 잔고", f"{INITIAL_BALANCE} USDT")
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
    
    # 기존 포지션 조회
    try:
        positions = api.list_positions(SETTLE)
        long_size = Decimal("0")
        short_size = Decimal("0")
        long_price = Decimal("0")
        short_price = Decimal("0")
        
        if positions:
            for p in positions:
                if p.contract == SYMBOL:
                    size_dec = Decimal(str(p.size))
                    if size_dec > 0:
                        long_size = size_dec
                        long_price = abs(Decimal(str(p.entry_price)))
                    elif size_dec < 0:
                        short_size = abs(size_dec)
                        short_price = abs(Decimal(str(p.entry_price)))
        
        with position_lock:
            position_state[SYMBOL]["long"]["size"] = long_size
            position_state[SYMBOL]["long"]["price"] = long_price
            position_state[SYMBOL]["short"]["size"] = short_size
            position_state[SYMBOL]["short"]["price"] = short_price
        
        log_debug("📊 기존 포지션", f"롱:{long_size}@{long_price} 숏:{short_size}@{short_price}")
        
    except Exception as e:
        log_debug("❌ 포지션 조회 실패", str(e))
    
    # 초기 그리드 생성
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if ticker:
            entry_price = Decimal(str(ticker[0].last))
            log_debug("💹 현재가", f"{entry_price}")
            
            if long_size == 0 and short_size == 0:
                log_debug("🔷 초기 그리드 생성", "포지션 없음")
                initialize_grid(entry_price, skip_check=False)
            else:
                log_debug("🔶 기존 포지션 존재", f"롱:{long_size} 숏:{short_size}")
                cancel_grid_orders(SYMBOL)
                time.sleep(0.5)
                refresh_tp_orders(SYMBOL)
                initialize_grid(entry_price, skip_check=True)
                
    except Exception as e:
        log_debug("❌ 초기화 오류", str(e))
    
    # 스레드 시작
    threading.Thread(target=update_initial_balance, daemon=True).start()
    threading.Thread(target=start_websocket, daemon=True).start()
    threading.Thread(target=fill_monitor, daemon=True).start()
    threading.Thread(target=grid_fill_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    
    log_debug("✅ 모든 스레드 시작", "")
    log_debug("=" * 80, "")
    
    # Flask 서버 시작
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
