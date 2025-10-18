#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ONDO 역방향 그리드 매매 시스템 v23.1-FIXED
- 헤징 진입 추적 버그 수정
- 실시간 개별 TP 생성
- Counter Snapshot 로직 개선
- 임계값 이하 복귀 시 진입 기록 초기화
"""

import os
import time
import asyncio
import threading
import logging
import json
from decimal import Decimal, ROUND_DOWN
from collections import deque
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi

try:
    from gate_api.exceptions import ApiException as GateApiException
except ImportError:
    from gate_api import ApiException as GateApiException

import websockets

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# 환경 변수
# =============================================================================
API_KEY = os.environ.get("GATE_API_KEY")
API_SECRET = os.environ.get("GATE_API_SECRET")
SYMBOL = os.environ.get("SYMBOL", "ONDO_USDT")
SETTLE = "usdt"

GRID_GAP_PCT = Decimal("0.12") / Decimal("100")
TP_GAP_PCT = Decimal("0.12") / Decimal("100")
BASE_RATIO = Decimal("0.1")
THRESHOLD_RATIO = Decimal("0.8")
COUNTER_RATIO = Decimal("0.30")
COUNTER_CLOSE_RATIO = Decimal("0.20")
MAX_POSITION_RATIO = Decimal("5.0")
HEDGE_RATIO_MAIN = Decimal("0.10")

# =============================================================================
# API 설정
# =============================================================================
config = Configuration(key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
api = FuturesApi(api_client)
unified_api = UnifiedApi(api_client)

app = Flask(__name__)

# =============================================================================
# 전역 변수
# =============================================================================
INITIAL_BALANCE = Decimal("50")
balance_lock = threading.Lock()
position_lock = threading.Lock()

position_state = {
    SYMBOL: {
        "long": {"size": Decimal("0"), "price": Decimal("0")},
        "short": {"size": Decimal("0"), "price": Decimal("0")}
    }
}

post_threshold_entries = {
    SYMBOL: {
        "long": deque(maxlen=100),
        "short": deque(maxlen=100)
    }
}

counter_position_snapshot = {
    SYMBOL: {"long": Decimal("0"), "short": Decimal("0")}
}

average_tp_orders = {
    SYMBOL: {"long": None, "short": None}
}

max_position_locked = {"long": False, "short": False}
grid_orders = {SYMBOL: {"long": [], "short": []}}

obv_macd_value = Decimal("0")
last_grid_time = 0

# =============================================================================
# 로그
# =============================================================================
def log(tag, msg):
    logger.info(f"[{tag}] {msg}")

def log_divider(char="=", length=80):
    logger.info(char * length)

def log_event_header(event_name):
    log_divider("-")
    log("🔔", f"EVENT: {event_name}")
    log_divider("-")

# =============================================================================
# 잔고 업데이트
# =============================================================================
def update_balance_thread():
    global INITIAL_BALANCE
    while True:
        try:
            time.sleep(3600)
            accounts = unified_api.list_unified_accounts()
            if accounts and hasattr(accounts, 'total') and accounts.total:
                with balance_lock:
                    INITIAL_BALANCE = Decimal(str(accounts.total))
                log("💰", f"Balance updated: {INITIAL_BALANCE:.2f} USDT")
        except Exception as e:
            log("❌", f"Balance update error: {e}")

# =============================================================================
# WebSocket 포지션 모니터링
# =============================================================================
async def watch_positions():
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
                log("🔌", "WebSocket connected.")
                
                async for message in ws:
                    data = json.loads(message)
                    if data.get("event") == "update" and data.get("channel") == "futures.positions":
                        for pos in data.get("result", []):
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
            log("❌", f"WebSocket error: {e}")
            await asyncio.sleep(5)

def start_websocket():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(watch_positions())

# =============================================================================
# 포지션 동기화
# =============================================================================
def sync_position():
    try:
        positions = api.list_positions(SETTLE)
        with position_lock:
            position_state[SYMBOL]["long"]["size"] = Decimal("0")
            position_state[SYMBOL]["long"]["price"] = Decimal("0")
            position_state[SYMBOL]["short"]["size"] = Decimal("0")
            position_state[SYMBOL]["short"]["price"] = Decimal("0")
            
            if positions:
                for p in positions:
                    if p.contract == SYMBOL:
                        size_dec = Decimal(str(p.size))
                        entry_price = abs(Decimal(str(p.entry_price)))
                        if size_dec > 0:
                            position_state[SYMBOL]["long"]["size"] = size_dec
                            position_state[SYMBOL]["long"]["price"] = entry_price
                        elif size_dec < 0:
                            position_state[SYMBOL]["short"]["size"] = abs(size_dec)
                            position_state[SYMBOL]["short"]["price"] = entry_price
    except Exception as e:
        log("❌", f"Position sync error: {e}")

# =============================================================================
# 주문 취소 (수정됨)
# =============================================================================
def cancel_all_orders():
    """모든 오픈 주문 취소 - 파라미터 중복 방지"""
    try:
        # status를 위치 인자가 아닌 키워드 인자로 명시
        orders = api.list_futures_orders(settle=SETTLE, contract=SYMBOL, status='open')
        if not orders:
            return
        
        log("🗑️", f"Cancelling {len(orders)} open order(s)...")
        for order in orders:
            try:
                api.cancel_futures_order(settle=SETTLE, order_id=str(order.id))
                time.sleep(0.05)
            except Exception as cancel_err:
                log("⚠️", f"Cancel failed for {order.id}: {cancel_err}")
        
        # 전역 상태 초기화
        grid_orders[SYMBOL] = {"long": [], "short": []}
        average_tp_orders[SYMBOL] = {"long": None, "short": None}
        
    except Exception as e:
        log("❌", f"Order cancellation error: {e}")

def cancel_grid_only():
    """진입 그리드만 취소 (TP 제외)"""
    try:
        orders = api.list_futures_orders(settle=SETTLE, contract=SYMBOL, status='open')
        if not orders:
            return
        
        grid_orders_to_cancel = [o for o in orders if not o.is_reduce_only]
        if not grid_orders_to_cancel:
            return
        
        log("🗑️", f"Cancelling {len(grid_orders_to_cancel)} grid order(s)...")
        for order in grid_orders_to_cancel:
            try:
                api.cancel_futures_order(settle=SETTLE, order_id=str(order.id))
                time.sleep(0.05)
            except Exception as cancel_err:
                log("⚠️", f"Grid cancel failed: {cancel_err}")
        
        grid_orders[SYMBOL] = {"long": [], "short": []}
        
    except Exception as e:
        log("❌", f"Grid cancellation error: {e}")

# =============================================================================
# 수량 계산
# =============================================================================
def calculate_obv_macd_weight(tt1_value):
    abs_val = abs(tt1_value)
    if abs_val < 5: return Decimal("0.10")
    elif abs_val < 10: return Decimal("0.11")
    elif abs_val < 15: return Decimal("0.12")
    elif abs_val < 20: return Decimal("0.13")
    elif abs_val < 30: return Decimal("0.15")
    elif abs_val < 40: return Decimal("0.17")
    elif abs_val < 50: return Decimal("0.20")
    elif abs_val < 70: return Decimal("0.23")
    elif abs_val < 100: return Decimal("0.27")
    elif abs_val < 150: return Decimal("0.30")
    else: return Decimal("0.35")

def calculate_grid_qty(is_above_threshold=False):
    try:
        with balance_lock:
            balance = INITIAL_BALANCE
        
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if not ticker: return 0
        price = Decimal(str(ticker[0].last))
        
        weight = BASE_RATIO if is_above_threshold else calculate_obv_macd_weight(float(obv_macd_value))
        
        qty = round(float((balance * weight) / price))
        return max(1, qty)
    except Exception as e:
        log("❌", f"Quantity calculation error: {e}")
        return 0

# =============================================================================
# 포지션 상태
# =============================================================================
def get_main_side():
    with position_lock:
        long_size = position_state[SYMBOL]["long"]["size"]
        short_size = position_state[SYMBOL]["short"]["size"]
    
    if long_size > short_size: return "long"
    elif short_size > long_size: return "short"
    else: return "none"

def is_above_threshold(side):
    """특정 포지션이 임계값 초과인지 확인"""
    with position_lock:
        size = position_state[SYMBOL][side]["size"]
        price = position_state[SYMBOL][side]["price"]
    
    with balance_lock:
        threshold = INITIAL_BALANCE * THRESHOLD_RATIO
    
    return (price * size >= threshold and size > 0)

# =============================================================================
# 주문 실행
# =============================================================================
def place_grid_order(side, price, qty):
    """그리드 주문 생성 - 에러 처리 강화"""
    try:
        if qty <= 0:
            log("⚠️", f"Invalid qty: {qty}")
            return None
        
        if price <= 0:
            log("⚠️", f"Invalid price: {price}")
            return None
        
        # 가격을 4자리로 반올림
        price_str = f"{float(price):.4f}"
        size = int(qty) if side == "long" else -int(qty)
        
        log("📐", f"Creating grid: {side.upper()} size={size} @ {price_str}")
        
        order = FuturesOrder(
            contract=SYMBOL,
            size=size,
            price=price_str,
            tif="gtc"
        )
        
        result = api.create_futures_order(settle=SETTLE, futures_order=order)
        
        if result and hasattr(result, 'id'):
            grid_orders[SYMBOL][side].append({
                "order_id": str(result.id),
                "price": float(price),
                "qty": abs(int(qty))
            })
            log("✅", f"Grid created: {side.upper()} {abs(int(qty))} @ {price_str}")
        
        return result
        
    except GateApiException as e:
        error_msg = str(e)
        if "INVALID_PRICE_PRECISION" in error_msg:
            log("❌", f"Price precision error: {price}")
        elif "INVALID_SIZE" in error_msg:
            log("❌", f"Size error: {qty}")
        else:
            log("❌", f"Grid order error ({side}): {error_msg}")
        return None
    except Exception as e:
        log("❌", f"Unexpected error ({side}): {e}")
        return None

def initialize_grid(current_price):
    global last_grid_time
    if time.time() - last_grid_time < 5: return
    last_grid_time = time.time()
    
    try:
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
            long_price = position_state[SYMBOL]["long"]["price"]
            short_price = position_state[SYMBOL]["short"]["price"]
        
        # 롱/숏 모두 있으면 그리드 생성 안 함
        if long_size > 0 and short_size > 0:
            log("ℹ️", "Both positions exist → Skip grid")
            return
        
        cancel_grid_only()
        
        long_grid_price = current_price * (Decimal("1") - GRID_GAP_PCT)
        short_grid_price = current_price * (Decimal("1") + GRID_GAP_PCT)
        
        with balance_lock:
            threshold = INITIAL_BALANCE * THRESHOLD_RATIO
        
        long_above = (long_price * long_size >= threshold and long_size > 0)
        short_above = (short_price * short_size >= threshold and short_size > 0)
        
        log("📈", "Initializing Grid...")
        if long_above:
            counter_qty = max(1, int(long_size * COUNTER_RATIO))
            same_qty = calculate_grid_qty(is_above_threshold=True)
            log("⚖️", f"Asymmetric (Long Main): Counter(Short) {counter_qty}, Same(Long) {same_qty}")
            place_grid_order("short", short_grid_price, counter_qty)
            place_grid_order("long", long_grid_price, same_qty)
        elif short_above:
            counter_qty = max(1, int(short_size * COUNTER_RATIO))
            same_qty = calculate_grid_qty(is_above_threshold=True)
            log("⚖️", f"Asymmetric (Short Main): Counter(Long) {counter_qty}, Same(Short) {same_qty}")
            place_grid_order("long", long_grid_price, counter_qty)
            place_grid_order("short", short_grid_price, same_qty)
        else:
            qty = calculate_grid_qty(is_above_threshold=False)
            log("📐", f"Symmetric Grid: {qty} each")
            place_grid_order("long", long_grid_price, qty)
            place_grid_order("short", short_grid_price, qty)
            
    except Exception as e:
        log("❌", f"Grid init error: {e}")

def hedge_after_grid_fill(filled_side, filled_price, filled_qty):
    """그리드 체결 후 헤징 + 임계값 초과 시 헤징도 추적"""
    try:
        main_side = get_main_side()
        hedge_side = "short" if filled_side == "long" else "long"
        
        with position_lock:
            main_size = position_state[SYMBOL][main_side]["size"] if main_side != "none" else Decimal("0")
            main_price = position_state[SYMBOL][main_side]["price"] if main_side != "none" else Decimal("0")
        
        with balance_lock:
            balance = INITIAL_BALANCE
        
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if not ticker: return
        current_price = Decimal(str(ticker[0].last))
        
        threshold = balance * THRESHOLD_RATIO
        main_value = main_price * main_size
        above_threshold = (main_value >= threshold and main_size > 0)
        
        # 헤징 수량 계산
        if above_threshold:
            if filled_side != main_side:  # 비주력 체결
                qty_10pct = int(main_size * HEDGE_RATIO_MAIN)
                base_qty = round(float((balance * BASE_RATIO) / current_price))
                hedge_qty = max(qty_10pct, base_qty)
            else:  # 주력 체결
                hedge_qty = round(float((balance * BASE_RATIO) / current_price))
        else:
            hedge_qty = round(float((balance * BASE_RATIO) / current_price))
        
        hedge_qty = max(1, hedge_qty)
        size = hedge_qty if hedge_side == "long" else -hedge_qty
        
        log("🛡️", f"Hedging: {hedge_side.upper()} {hedge_qty}")
        order = FuturesOrder(contract=SYMBOL, size=size, price="0", tif='ioc')
        created_order = api.create_futures_order(SETTLE, order)
        
        # ⭐ 수정: 임계값 초과 시 모든 헤징 추적
        if above_threshold:
            time.sleep(1)
            try:
                trades = api.list_my_trades(settle=SETTLE, contract=SYMBOL, limit=10)
                for trade in trades:
                    if str(trade.order_id) == str(created_order.id):
                        trade_qty = abs(Decimal(str(trade.size)))
                        trade_price = Decimal(str(trade.price))
                        log("📝", f"Hedge tracked: {hedge_side.upper()} {trade_qty} @ {trade_price:.4f}")
                        track_entry(hedge_side, trade_qty, trade_price, "hedge")
                        
                        # 실시간 개별 TP 생성
                        if is_above_threshold(hedge_side):
                            tp_id = create_individual_tp(hedge_side, trade_qty, trade_price)
                            if tp_id and post_threshold_entries[SYMBOL][hedge_side]:
                                post_threshold_entries[SYMBOL][hedge_side][-1]["tp_order_id"] = tp_id
                        break
            except Exception as e:
                log("❌", f"Trade fetch error: {e}")
                    
    except Exception as e:
        log("❌", f"Hedging error: {e}")

def create_individual_tp(side, qty, entry_price):
    """개별 TP 생성 - 에러 처리 강화"""
    try:
        if qty <= 0 or entry_price <= 0:
            log("⚠️", f"Invalid TP params: qty={qty}, price={entry_price}")
            return None
        
        tp_price = entry_price * (Decimal("1") + TP_GAP_PCT) if side == "long" else entry_price * (Decimal("1") - TP_GAP_PCT)
        tp_price_str = f"{float(tp_price):.4f}"
        size = -int(qty) if side == "long" else int(qty)
        
        order = FuturesOrder(
            contract=SYMBOL,
            size=size,
            price=tp_price_str,
            tif="gtc",
            reduce_only=True
        )
        
        result = api.create_futures_order(settle=SETTLE, futures_order=order)
        
        if result and hasattr(result, 'id'):
            log("🎯", f"Individual TP: {side.upper()} {abs(int(qty))} @ {tp_price_str}")
            return str(result.id)
        
        return None
        
    except Exception as e:
        log("❌", f"Individual TP error: {e}")
        return None

def create_average_tp(side):
    try:
        with position_lock:
            size = position_state[SYMBOL][side]["size"]
            avg_price = position_state[SYMBOL][side]["price"]
        
        if size <= 0 or avg_price <= 0: return
        
        individual_total = sum(entry["qty"] for entry in post_threshold_entries[SYMBOL][side])
        remaining_qty = int(size - individual_total)
        
        if remaining_qty <= 0: return
        
        tp_price = avg_price * (Decimal("1") + TP_GAP_PCT) if side == "long" else avg_price * (Decimal("1") - TP_GAP_PCT)
        order_size = -remaining_qty if side == "long" else remaining_qty
        
        order = FuturesOrder(contract=SYMBOL, size=order_size, price=str(tp_price), tif="gtc", reduce_only=True)
        result = api.create_futures_order(SETTLE, order)
        if result and hasattr(result, 'id'):
            average_tp_orders[SYMBOL][side] = result.id
            log("🎯", f"Average TP: {side.upper()} {remaining_qty} @ {tp_price:.4f}")
            
    except Exception as e:
        log("❌", f"Average TP error: {e}")

def refresh_all_tp_orders():
    try:
        orders = api.list_futures_orders(SETTLE, SYMBOL, status='open')
        tp_orders = [o for o in orders if o.is_reduce_only]
        if tp_orders:
            log("🗑️", f"Cancelling {len(tp_orders)} TP(s)...")
            for order in tp_orders:
                try: api.cancel_futures_order(SETTLE, order.id); time.sleep(0.05)
                except: pass
        
        average_tp_orders[SYMBOL] = {"long": None, "short": None}
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        log("📈", "Refreshing TPs...")
        
        # 롱 TP
        if long_size > 0:
            if is_above_threshold("long"):
                for entry in post_threshold_entries[SYMBOL]["long"]:
                    tp_id = create_individual_tp("long", entry["qty"], Decimal(str(entry["price"])))
                    if tp_id: entry["tp_order_id"] = tp_id
                create_average_tp("long")
            else:
                # 임계값 미만: 전체 평단 TP
                with position_lock:
                    avg_price = position_state[SYMBOL]["long"]["price"]
                tp_price = avg_price * (Decimal("1") + TP_GAP_PCT)
                order = FuturesOrder(contract=SYMBOL, size=-int(long_size), price=str(tp_price), tif="gtc", reduce_only=True)
                result = api.create_futures_order(SETTLE, order)
                if result and hasattr(result, 'id'):
                    average_tp_orders[SYMBOL]["long"] = result.id
                    log("🎯", f"Full TP: LONG {int(long_size)} @ {tp_price:.4f}")
        
        # 숏 TP
        if short_size > 0:
            if is_above_threshold("short"):
                for entry in post_threshold_entries[SYMBOL]["short"]:
                    tp_id = create_individual_tp("short", entry["qty"], Decimal(str(entry["price"])))
                    if tp_id: entry["tp_order_id"] = tp_id
                create_average_tp("short")
            else:
                with position_lock:
                    avg_price = position_state[SYMBOL]["short"]["price"]
                tp_price = avg_price * (Decimal("1") - TP_GAP_PCT)
                order = FuturesOrder(contract=SYMBOL, size=int(short_size), price=str(tp_price), tif="gtc", reduce_only=True)
                result = api.create_futures_order(SETTLE, order)
                if result and hasattr(result, 'id'):
                    average_tp_orders[SYMBOL]["short"] = result.id
                    log("🎯", f"Full TP: SHORT {int(short_size)} @ {tp_price:.4f}")
                    
    except Exception as e:
        log("❌", f"TP refresh error: {e}")

def close_counter_on_individual_tp(main_side):
    """개별 TP 체결 시 비주력 20% 청산 (스냅샷 기반)"""
    try:
        counter_side = "short" if main_side == "long" else "long"
        
        with position_lock:
            counter_size = position_state[SYMBOL][counter_side]["size"]
        
        if counter_size <= 0: return
        
        # 스냅샷 확인 및 설정
        snapshot = counter_position_snapshot[SYMBOL][main_side]
        if snapshot == Decimal("0"):
            snapshot = counter_size
            counter_position_snapshot[SYMBOL][main_side] = snapshot
            log("📸", f"Snapshot set: {counter_side.upper()} = {snapshot}")
        
        close_qty = max(1, int(snapshot * COUNTER_CLOSE_RATIO))
        if close_qty > counter_size: close_qty = int(counter_size)
        
        size = -close_qty if counter_side == "long" else close_qty
        
        log("🔄", f"Counter close: {counter_side.upper()} {close_qty} (snapshot: {snapshot})")
        order = FuturesOrder(contract=SYMBOL, size=size, price="0", tif='ioc', reduce_only=True)
        api.create_futures_order(SETTLE, order)
        
    except Exception as e:
        log("❌", f"Counter close error: {e}")

# =============================================================================
# 상태 추적
# =============================================================================
def track_entry(side, qty, price, entry_type):
    """임계값 초과 후 진입 추적"""
    if not is_above_threshold(side):
        return
    
    entry_data = {"qty": int(qty), "price": float(price), "entry_type": entry_type, "tp_order_id": None}
    post_threshold_entries[SYMBOL][side].append(entry_data)
    log("📝", f"Entry tracked: {side.upper()} {qty} @ {price:.4f} ({entry_type})")

# =============================================================================
# 시스템 새로고침
# =============================================================================
def full_refresh(event_type):
    log_event_header(f"Full Refresh: {event_type}")
    
    sync_position()
    with position_lock:
        pos = position_state[SYMBOL]
        log("📊", f"Before: Long {pos['long']['size']}@{pos['long']['price']:.4f}, Short {pos['short']['size']}@{pos['short']['price']:.4f}")

    cancel_all_orders()
    time.sleep(0.5)
    
    ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
    if ticker:
        initialize_grid(Decimal(str(ticker[0].last)))
    
    refresh_all_tp_orders()
    
    sync_position()
    with position_lock:
        pos = position_state[SYMBOL]
        log("📊", f"After: Long {pos['long']['size']}@{pos['long']['price']:.4f}, Short {pos['short']['size']}@{pos['short']['price']:.4f}")
    log("✅", f"Refresh complete: {event_type}")

# =============================================================================
# 모니터링 스레드 (수정됨)
# =============================================================================
def grid_fill_monitor():
    """그리드 체결 모니터링"""
    while True:
        try:
            time.sleep(0.5)
            
            for side in ["long", "short"]:
                filled = []
                
                for order_info in list(grid_orders[SYMBOL][side]):
                    try:
                        order = api.get_futures_order(settle=SETTLE, order_id=str(order_info["order_id"]))
                        
                        if order.status == 'finished':
                            log_event_header("Grid Fill")
                            log("🎉", f"{side.upper()} {order_info['qty']} @ {order_info['price']:.4f}")
                            
                            sync_position()
                            track_entry(side, order_info['qty'], order_info['price'], "grid")
                            
                            # 실시간 개별 TP 생성
                            if is_above_threshold(side):
                                tp_id = create_individual_tp(side, order_info['qty'], Decimal(str(order_info['price'])))
                                if tp_id and post_threshold_entries[SYMBOL][side]:
                                    post_threshold_entries[SYMBOL][side][-1]["tp_order_id"] = tp_id
                            
                            hedge_after_grid_fill(side, order_info['price'], order_info['qty'])
                            time.sleep(0.5)
                            full_refresh("Grid_Fill")
                            filled.append(order_info)
                            break
                            
                    except GateApiException as e:
                        if "ORDER_NOT_FOUND" in str(e):
                            filled.append(order_info)
                    except Exception as e:
                        log("❌", f"Grid check error: {e}")
                
                # 체결된 주문 제거
                grid_orders[SYMBOL][side] = [
                    o for o in grid_orders[SYMBOL][side] 
                    if o not in filled
                ]
                
        except Exception as e:
            log("❌", f"Grid monitor error: {e}")
            time.sleep(1)

def tp_monitor():
    """TP 체결 모니터링"""
    while True:
        try:
            time.sleep(3)
            
            # 개별 TP 체결 확인
            for side in ["long", "short"]:
                entries_to_remove = []
                
                for entry in list(post_threshold_entries[SYMBOL][side]):
                    tp_id = entry.get("tp_order_id")
                    if not tp_id:
                        continue
                    
                    try:
                        order = api.get_futures_order(settle=SETTLE, order_id=str(tp_id))
                        
                        if order.status == "finished":
                            log_event_header("Individual TP Hit")
                            log("✅", f"{side.upper()} {entry['qty']} @ entry {entry['price']:.4f}")
                            
                            close_counter_on_individual_tp(side)
                            time.sleep(0.5)
                            
                            entries_to_remove.append(entry)
                            full_refresh("Individual_TP")
                            break
                            
                    except GateApiException as e:
                        if "ORDER_NOT_FOUND" in str(e):
                            entries_to_remove.append(entry)
                    except Exception as e:
                        log("❌", f"Individual TP check error: {e}")
                
                # 체결된 진입 기록 제거
                for entry in entries_to_remove:
                    if entry in post_threshold_entries[SYMBOL][side]:
                        post_threshold_entries[SYMBOL][side].remove(entry)
            
            # 평단 TP 체결 확인
            for side in ["long", "short"]:
                tp_id = average_tp_orders[SYMBOL][side]
                if not tp_id:
                    continue
                
                try:
                    order = api.get_futures_order(settle=SETTLE, order_id=str(tp_id))
                    
                    if order.status == "finished":
                        log_event_header("Average TP Hit")
                        log("✅", f"{side.upper()} position closed")
                        full_refresh("Average_TP")
                        break
                        
                except GateApiException as e:
                    if "ORDER_NOT_FOUND" in str(e):
                        average_tp_orders[SYMBOL][side] = None
                except Exception as e:
                    log("❌", f"Average TP check error: {e}")
                    
        except Exception as e:
            log("❌", f"TP monitor error: {e}")
            time.sleep(1)

def position_monitor():
    prev_long_size = Decimal("-1")
    prev_short_size = Decimal("-1")
    
    while True:
        try:
            time.sleep(1)
            sync_position()
            
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
                long_price = position_state[SYMBOL]["long"]["price"]
                short_price = position_state[SYMBOL]["short"]["price"]
            
            # 포지션 변경 감지
            if long_size != prev_long_size or short_size != prev_short_size:
                if prev_long_size != Decimal("-1"):
                    log("📊", f"Position: Long {prev_long_size}→{long_size}, Short {prev_short_size}→{short_size}")
                prev_long_size = long_size
                prev_short_size = short_size
            
            with balance_lock:
                balance = INITIAL_BALANCE
            
            threshold = balance * THRESHOLD_RATIO
            max_value = balance * MAX_POSITION_RATIO
            long_value = long_price * long_size
            short_value = short_price * short_size
            
            # 최대 보유 한도 체크
            if long_value >= max_value and not max_position_locked["long"]:
                log_event_header("Max Position Limit")
                log("⚠️", f"LONG {long_value:.2f} >= {max_value:.2f}")
                max_position_locked["long"] = True
                cancel_grid_only()
            
            if short_value >= max_value and not max_position_locked["short"]:
                log_event_header("Max Position Limit")
                log("⚠️", f"SHORT {short_value:.2f} >= {max_value:.2f}")
                max_position_locked["short"] = True
                cancel_grid_only()
            
            # 한도 잠금 해제
            if long_value < max_value and max_position_locked["long"]:
                log("✅", f"Max unlock: LONG ({long_value:.2f} < {max_value:.2f})")
                max_position_locked["long"] = False
                full_refresh("Max_Unlock_Long")
                continue
            
            if short_value < max_value and max_position_locked["short"]:
                log("✅", f"Max unlock: SHORT ({short_value:.2f} < {max_value:.2f})")
                max_position_locked["short"] = False
                full_refresh("Max_Unlock_Short")
                continue
            
            # ⭐ 임계값 이하 복귀 시 초기화
            if long_value < threshold:
                if counter_position_snapshot[SYMBOL]["long"] != Decimal("0"):
                    log("🔄", f"Long below threshold ({long_value:.2f} < {threshold:.2f}). Resetting.")
                    counter_position_snapshot[SYMBOL]["long"] = Decimal("0")
                    post_threshold_entries[SYMBOL]["long"].clear()
            
            if short_value < threshold:
                if counter_position_snapshot[SYMBOL]["short"] != Decimal("0"):
                    log("🔄", f"Short below threshold ({short_value:.2f} < {threshold:.2f}). Resetting.")
                    counter_position_snapshot[SYMBOL]["short"] = Decimal("0")
                    post_threshold_entries[SYMBOL]["short"].clear()

        except Exception as e:
            log("❌", f"Position monitor error: {e}")
            time.sleep(1)

# =============================================================================
# Flask 엔드포인트
# =============================================================================
@app.route('/webhook', methods=['POST'])
def webhook():
    """
    TradingView에서 OBV MACD 값을 받아 업데이트
    Gate.io API 호출 없이 단순 데이터 수신만 처리
    """
    global obv_macd_value
    try:
        data = request.get_json(force=True)  # force=True 추가로 Content-Type 무시
        if not data:
            return jsonify({"status": "error", "message": "No data received"}), 400
        
        tt1 = data.get('tt1')
        if tt1 is None:
            return jsonify({"status": "error", "message": "Missing tt1 value"}), 400
        
        obv_macd_value = Decimal(str(tt1))
        log("📨", f"OBV MACD updated: {tt1}")
        
        return jsonify({
            "status": "success",
            "tt1": float(tt1),
            "abs_val": float(abs(obv_macd_value * 1000)),
            "weight": float(calculate_obv_macd_weight(float(obv_macd_value * 1000)))
        }), 200
        
    except ValueError as e:
        log("❌", f"Webhook value error: {e}")
        return jsonify({"status": "error", "message": f"Invalid tt1 value: {e}"}), 400
    except Exception as e:
        log("❌", f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/status', methods=['GET'])
def status():
    with position_lock:
        pos = position_state[SYMBOL]
    with balance_lock:
        bal = float(INITIAL_BALANCE)
    
    return jsonify({
        "balance": bal,
        "obv_macd": float(obv_macd_value),
        "position": {
            "long": {"size": float(pos["long"]["size"]), "price": float(pos["long"]["price"])},
            "short": {"size": float(pos["short"]["size"]), "price": float(pos["short"]["price"])}
        },
        "post_threshold_entries": {
            "long": len(post_threshold_entries[SYMBOL]["long"]),
            "short": len(post_threshold_entries[SYMBOL]["short"])
        },
        "counter_snapshot": {
            "long": float(counter_position_snapshot[SYMBOL]["long"]),
            "short": float(counter_position_snapshot[SYMBOL]["short"])
        },
        "max_locked": max_position_locked
    }), 200

@app.route('/refresh', methods=['POST'])
def manual_refresh():
    try:
        full_refresh("Manual")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# =============================================================================
# 메인 실행
# =============================================================================
def print_startup_summary():
    log_divider("=")
    log("🚀", "ONDO Trading Bot v23.1-FIXED - STARTING")
    log_divider("=")
    log("📜", "SETTINGS:")
    log("🔹", f"Symbol: {SYMBOL}")
    log("🔹", f"Grid/TP Gap: {GRID_GAP_PCT * 100}%")
    log("🔹", f"Base Ratio: {BASE_RATIO * 100}%")
    log("🔹", f"Threshold: {THRESHOLD_RATIO * 100}%")
    log("🔹", f"Max Position: {MAX_POSITION_RATIO * 100}%")
    log("🔹", f"Counter Ratio: {COUNTER_RATIO * 100}%")
    log("🔹", f"Counter Close: {COUNTER_CLOSE_RATIO * 100}%")
    log_divider("-")
    
    # 초기 잔고
    try:
        accounts = unified_api.list_unified_accounts()
        if accounts and hasattr(accounts, 'total') and accounts.total:
            global INITIAL_BALANCE
            INITIAL_BALANCE = Decimal(str(accounts.total))
    except Exception as e:
        log("❌", f"Balance check error: {e}")
    log("💰", f"Initial Balance: {INITIAL_BALANCE:.2f} USDT")
    
    # 기존 포지션
    sync_position()
    with position_lock:
        pos = position_state[SYMBOL]
        log("📊", f"Initial Position: Long {pos['long']['size']}@{pos['long']['price']:.4f}, Short {pos['short']['size']}@{pos['short']['price']:.4f}")
    
    # 초기화
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if ticker:
            current_price = Decimal(str(ticker[0].last))
            log("💹", f"Current Price: {current_price:.4f}")
            
            cancel_all_orders()
            time.sleep(0.5)
            
            initialize_grid(current_price)
            
            if pos['long']['size'] > 0 or pos['short']['size'] > 0:
                refresh_all_tp_orders()
    except Exception as e:
        log("❌", f"Initialization error: {e}")
    
    log_divider("=")
    log("✅", "Initialization Complete. Starting Threads...")
    log_divider("=")

if __name__ == '__main__':
    print_startup_summary()
    
    threading.Thread(target=update_balance_thread, daemon=True).start()
    threading.Thread(target=start_websocket, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=grid_fill_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    
    log("✅", "All threads started.")
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
