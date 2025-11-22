import os
import time
import asyncio
import threading
import logging
import json
import math
from decimal import Decimal, ROUND_DOWN
from collections import deque
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi
import hashlib

try:
    from gate_api.exceptions import ApiException as GateApiException
except ImportError:
    from gate_api import ApiException as GateApiException

import websockets

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# 환경 변수 (Environment Variables)
# =============================================================================
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SYMBOL = os.environ.get("SYMBOL", "BNB_USDT")
SETTLE = "usdt"

# Railway 환경 변수 로그
if API_KEY:
    logger.info(f"API_KEY loaded: {API_KEY[:8]}...")
else:
    logger.error("API_KEY not found in environment variables!")
   
if API_SECRET:
    logger.info(f"API_SECRET loaded: {len(API_SECRET)} characters")
else:
    logger.error("API_SECRET not found in environment variables!")


# =============================================================================
# 전략 설정 (Strategy Configuration)
# =============================================================================
# 기본 비율 설정
INITIALBALANCE = Decimal("50")              # 초기 잔고
BASERATIO = Decimal("0.01")                 # ← 기본 수량 비율 (1%)
MAXPOSITIONRATIO = Decimal("3.0")           # 최대 포지션 비율 (3배)
HEDGE_RATIO_MAIN = Decimal("0.10")          # 주력 헤지 비율 (10%)

# TP 설정 (동적 TP 기준값)
TPMIN = Decimal("0.0021")                   # 최소 TP (0.21%)
TPMAX = Decimal("0.004")                    # 최대 TP (0.4%)

# 시간 설정
IDLE_TIME_SECONDS = 600                      # 아이들 감지 시간 (10분)
IDLE_TIMEOUT = 600                           # 아이들 타임아웃 (10분)
IDLE_ENTRY_COOLDOWN = 10                     # 아이들 진입 쿨다운 (10초)
REBALANCE_SECONDS = 5 * 3600                 # 리밸런싱 시간 (5시간)

# 임계값 설정
OBV_CHANGE_THRESHOLD = Decimal("0.05")       # OBV 변화 임계값 (5%)
TP_CHANGE_THRESHOLD = Decimal("0.01")        # TP 변화 임계값 (0.01%)

# 기능 플래그
ENABLE_AUTO_HEDGE = True                     # 자동 헤지 활성화


# =============================================================================
# API 클라이언트 설정 (API Client Configuration)
# =============================================================================
config = Configuration(key=API_KEY, secret=API_SECRET)
config.host = "https://api.gateio.ws/api/v4"
config.verify_ssl = True
api_client = ApiClient(config)
api = FuturesApi(api_client)
unified_api = UnifiedApi(api_client)

app = Flask(__name__)


def fetch_min_lot(symbol):
    """
    Gate.io 마켓 정보로부터 최소 주문 수량과 정밀도(Precision)를 가져옴
    """
    try:
        contracts = api.list_futures_contracts(SETTLE)
        for c in contracts:
            if c.name == symbol:
                min_qty_str = "0.001"
                
                if hasattr(c, 'order_size_min'):
                    min_qty_str = str(c.order_size_min)
                elif hasattr(c, 'min_base_amount'):
                    min_qty_str = str(c.min_base_amount)
                elif hasattr(c, 'size_min'):
                    min_qty_str = str(c.size_min)
                
                min_qty = Decimal(min_qty_str)
                
                if "." in min_qty_str:
                    precision = len(min_qty_str.split(".")[1])
                else:
                    precision = 0
                
                return min_qty, precision
                
    except Exception as e:
        log("❌ FETCH_MIN_LOT", f"Error fetching contract info: {e}")
    
    log("⚠️ FETCH_MIN_LOT", "Using default fallback values (0.001, 3)")
    return Decimal("0.001"), 3

# 초기 세팅부:
MIN_QUANTITY, step_precision = fetch_min_lot("BNB_USDT")
QUANTITY_STEP = Decimal(str(10 ** -step_precision))


# =============================================================================
# 스레드 동기화 (Thread Locks)
# =============================================================================
balance_lock = threading.Lock()
position_lock = threading.Lock()
initialize_grid_lock = threading.Lock()
refresh_tp_lock = threading.Lock()
hedge_lock = threading.Lock()
idle_entry_progress_lock = threading.Lock()
idle_entry_lock = threading.Lock()


# =============================================================================
# 전역 상태 변수 (Global State Variables)
# =============================================================================
# 계좌 관련
account_balance = INITIALBALANCE
initial_capital = Decimal("0")
CAPITAL_FILE = "initial_capital.json"
last_no_position_time = 0

# 포지션 상태
position_state = {
    SYMBOL: {
        "long": {"size": Decimal("0"), "entry_price": Decimal("0")},
        "short": {"size": Decimal("0"), "entry_price": Decimal("0")}
    }
}

# TP 관련
tp_gap_min = TPMIN
tp_gap_max = TPMAX
tp_gap_long = TPMIN
tp_gap_short = TPMIN
last_tp_hash = ""
last_adjusted_obv = 0
tp_order_hash = {}

# 평단 TP 주문 ID
average_tp_orders = {
    SYMBOL: {"long": None, "short": None}
}

# 그리드 주문 추적
grid_orders = {SYMBOL: {"long": [], "short": []}}

# 최대 포지션 잠금
max_position_locked = {"long": False, "short": False}

# OBV MACD 관련
obv_macd_value = Decimal("0")
kline_history = deque(maxlen=200)

# 아이들 진입 관련
idle_entry_in_progress = False
last_idle_entry_time = 0
last_idle_check = 0
idle_entry_count = 0

# 이벤트 타임 트래킹
last_event_time = 0
last_grid_time = 0

# 주문 관련
pending_orders = deque(maxlen=100)
order_sequence_id = 0


# =============================================================================
# Initial Capital 저장/로드 함수
# =============================================================================
def save_initial_capital():
    try:
        data = {
            "initial_capital": str(initial_capital),
            "timestamp": time.time(),
            "symbol": SYMBOL
        }
        with open(CAPITAL_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        log("💾 SAVE", f"Initial Capital saved: {initial_capital:.2f} USDT")
    except Exception as e:
        log("❌ SAVE", f"Failed to save capital: {e}")

def load_initial_capital():
    global initial_capital
    try:
        if os.path.exists(CAPITAL_FILE):
            with open(CAPITAL_FILE, 'r') as f:
                data = json.load(f)
           
            loaded_capital = Decimal(data.get("initial_capital", "0"))
            saved_symbol = data.get("symbol", "")
            timestamp = data.get("timestamp", 0)
           
            if saved_symbol == SYMBOL and loaded_capital > 0:
                initial_capital = loaded_capital
                saved_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                log("📂 LOAD", f"Initial Capital loaded: {initial_capital:.2f} USDT (saved at {saved_time})")
                return True
            else:
                log("⚠️ LOAD", "Invalid saved data (symbol mismatch or zero capital)")
                return False
        else:
            log("ℹ️ LOAD", "No saved capital file found")
            return False
    except Exception as e:
        log("❌ LOAD", f"Failed to load capital: {e}")
        return False

# =============================================================================
# 주문 ID 생성
# =============================================================================
def generate_order_id():
    global order_sequence_id
    order_sequence_id += 1
    timestamp = int(time.time() * 1000)
    unique_id = f"t-{timestamp}_{order_sequence_id}"
    return unique_id

# =============================================================================
# 로그
# =============================================================================
def log(tag, msg):
    if tag == "divider":
        logger.info(msg)
    else:
        logger.info(f"[{tag}] {msg}")

def log_divider(char="=", length=80):
    logger.info(char * length)

def log_event_header(event_name):
    log_divider("-")
    log("🔔 EVENT", event_name)
    log_divider("-")

def get_main_side():
    try:
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        if long_size > short_size:
            return "long"
        elif short_size > long_size:
            return "short"
        else:
            return "none"
    except:
        return "none"

def log_position_state():
    with position_lock:
        long_size = position_state[SYMBOL]["long"]["size"]
        long_price = position_state[SYMBOL]["long"]["entry_price"]
        short_size = position_state[SYMBOL]["short"]["size"]
        short_price = position_state[SYMBOL]["short"]["entry_price"]
   
    with balance_lock:
        balance = account_balance
   
    long_value = long_price * long_size
    short_value = short_price * short_size
   
    log("📊 POSITION", f"Long: {long_size} @ {long_price:.4f} (${long_value:.2f})")
    log("📊 POSITION", f"Short: {short_size} @ {short_price:.4f} (${short_value:.2f})")
     
    main = get_main_side()
    if main != "none":
        log("📊 MAIN", f"{main.upper()} (더 큰 포지션)")

# =============================================================================
# 포지션 동기화
# =============================================================================
def sync_position(max_retries=3, retry_delay=2):
    """
    포지션 정보를 동기화합니다. (수량 보정 로직 수정됨)
    Gate.io는 size를 '계약 수(장)'로 줄 때가 많으므로,
    BNB_USDT 기준 1계약 = 0.001 BNB 등으로 변환이 필요할 수 있습니다.
    여기서는 안전하게 모든 값을 0.001 단위로 봅니다.
    """
    for attempt in range(max_retries):
        try:
            positions = api.list_positions(SETTLE)
           
            with position_lock:
                # 초기화
                position_state[SYMBOL]["long"]["size"] = Decimal("0")
                position_state[SYMBOL]["long"]["entry_price"] = Decimal("0")
                position_state[SYMBOL]["short"]["size"] = Decimal("0")
                position_state[SYMBOL]["short"]["entry_price"] = Decimal("0")
           
            if positions:
                for p in positions:
                    if p.contract == SYMBOL:
                        try:
                            raw_size = float(p.size) # 예: 6 (계약 수)
                            
                            # ★ [수정] BNB_USDT는 1계약이 작으므로, 정수로 들어온 값을 코인 개수로 변환
                            # 만약 raw_size가 6인데 실제 0.006 BNB라면 0.001을 곱해야 함.
                            # Gate.io API 특성상 대부분 정수(계약수)로 옴.
                            
                            # 안전 장치: 만약 API가 이미 0.006 처럼 소수로 준다면?
                            # 보통 정수로 오지만, 소수점 체크
                            if abs(raw_size) < 0.1 and raw_size != 0:
                                # 이미 소수점 단위로 온 경우 (매우 드묾)
                                size_dec = Decimal(str(raw_size))
                            else:
                                # 정수(계약수)로 온 경우 -> 0.001 곱해서 BNB 개수로 변환
                                # (주의: 심볼마다 다를 수 있으나 BNB는 보통 0.001 or 0.01)
                                # 여기서는 사용자의 0.006 BNB = 6 contract 가정하에 0.001 적용
                                size_dec = Decimal(str(raw_size)) * Decimal("0.001")
                                
                            log("⚠️ SYNC", f"Raw: {raw_size} -> Corrected: {size_dec}")

                        except Exception as e:
                            log("❌ SYNC", f"Size parse error: {e}")
                            size_dec = Decimal("0")

                        entry_price = abs(Decimal(str(p.entry_price))) if p.entry_price else Decimal("0")
                       
                        if size_dec > 0:
                            with position_lock:
                                position_state[SYMBOL]["long"]["size"] = size_dec
                                position_state[SYMBOL]["long"]["entry_price"] = entry_price
                        elif size_dec < 0:
                            with position_lock:
                                position_state[SYMBOL]["short"]["size"] = abs(size_dec)
                                position_state[SYMBOL]["short"]["entry_price"] = entry_price
            return True
           
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                log("❌ SYNC", f"Error: {e}")
                return False
    return False


# =============================================================================
# 주문 취소
# =============================================================================
def cancel_all_orders():
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
        if not orders:
            return
        
        log("[❌ CANCEL]", f"Cancelling {len(orders)} orders...")
        cancelled_count = 0
        for order in orders:
            try:
                api.cancel_futures_order(SETTLE, order.id)
                cancelled_count += 1
                time.sleep(0.05)
            except:
                pass
       
        if SYMBOL in grid_orders:
            grid_orders[SYMBOL] = {"long": [], "short": []}
        if SYMBOL in average_tp_orders:
            average_tp_orders[SYMBOL] = {"long": None, "short": None}
       
        log("[✅ CANCEL]", f"{cancelled_count}/{len(orders)} orders cancelled")
       
    except Exception as e:
        log("[❌]", f"Order cancellation error: {e}")

def cancel_tp_only():
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
        tp_orders = [o for o in orders if o.is_reduce_only]
       
        if len(tp_orders) == 0:
            return
       
        log("🗑️ TP", f"Cancelling {len(tp_orders)} TP orders")
        for order in tp_orders:
            try:
                api.cancel_futures_order(SETTLE, order.id)
                time.sleep(0.1)
            except:
                pass
    except Exception as e:
        log("❌", f"TP cancel error: {e}")

# ============================================================================
# TP 새로고침 (동적 TP)
# ============================================================================
def refresh_all_tp_orders():
    try:
        sync_position()
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
            long_entry_price = position_state[SYMBOL]["long"]["entry_price"]
            short_entry_price = position_state[SYMBOL]["short"]["entry_price"]
       
        if long_size == 0 and short_size == 0:
            return
       
        tp_result = calculate_dynamic_tp_gap()
        if isinstance(tp_result, (tuple, list)) and len(tp_result) >= 2:
            long_tp = tp_result[0]
            short_tp = tp_result[1]
        else:
            long_tp = TPMIN
            short_tp = TPMIN
            
        if not isinstance(long_tp, Decimal): long_tp = Decimal(str(long_tp))
        if not isinstance(short_tp, Decimal): short_tp = Decimal(str(short_tp))
       
        cancel_tp_only()
        time.sleep(1.0)
       
        if long_size > 0 and long_entry_price > 0:
            tp_price_long = long_entry_price * (Decimal("1") + long_tp)
            tp_price_long = tp_price_long.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            long_qty = adjust_quantity_step(long_size)
            
            if long_qty > 0:
                try:
                    order = FuturesOrder(
                        contract=SYMBOL,
                        size=str(-long_qty),
                        price=str(tp_price_long),
                        tif="gtc",
                        reduce_only=True,
                        text=generate_order_id()
                    )
                    api.create_futures_order(SETTLE, order)
                    log("✅ TP LONG", f"Qty: {long_qty}, Price: {float(tp_price_long):.4f}")
                except Exception as e:
                    log("❌ TP LONG FAIL", f"Qty: {long_qty}, Error: {e}")
       
        time.sleep(0.5)
       
        if short_size > 0 and short_entry_price > 0:
            tp_price_short = short_entry_price * (Decimal("1") - short_tp)
            tp_price_short = tp_price_short.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            short_qty = adjust_quantity_step(short_size)
            
            if short_qty > 0:
                try:
                    order = FuturesOrder(
                        contract=SYMBOL,
                        size=str(short_qty),
                        price=str(tp_price_short),
                        tif="gtc",
                        reduce_only=True,
                        text=generate_order_id()
                    )
                    api.create_futures_order(SETTLE, order)
                    log("✅ TP SHORT", f"Qty: {short_qty}, Price: {float(tp_price_short):.4f}")
                except Exception as e:
                    log("❌ TP SHORT FAIL", f"Qty: {short_qty}, Error: {e}")
       
        log("✅ TP", "TP refresh process completed")
    except Exception as e:
        log("❌ TP REFRESH", f"Critical Error: {e}")


# =============================================================================
# 수량 계산 함수
# =============================================================================
def calculate_obv_macd_weight(obv_value):
    obv_abs = abs(obv_value)
    if obv_abs < 20:
        multiplier = Decimal("1.1")
    elif obv_abs < 30:
        multiplier = Decimal("1.2")
    elif obv_abs < 40:
        multiplier = Decimal("1.3")
    elif obv_abs < 50:
        multiplier = Decimal("1.4")
    elif obv_abs < 60:
        multiplier = Decimal("1.5")
    elif obv_abs < 70:
        multiplier = Decimal("1.6")
    elif obv_abs < 100:
        multiplier = Decimal("1.8")
    else:
        multiplier = Decimal("2.0")
    return multiplier

def safe_order_qty(qty, min_qty=MIN_QUANTITY):
    try:
        qty_float = float(qty)
        safe = max(round(qty_float, 3), float(min_qty))
        return safe
    except Exception as e:
        log("❌ QTY", f"safe_order_qty Exception: {e}")
        return float(min_qty)

def adjust_quantity_step(qty, step=QUANTITY_STEP, min_qty=MIN_QUANTITY):
    qty_dec = Decimal(str(qty))
    step_dec = Decimal(str(step))
    floored = (qty_dec // step_dec) * step_dec
    floored = floored.quantize(step_dec)
    if floored < Decimal(str(min_qty)):
        floored = Decimal(str(min_qty))
    return floored

def calculate_grid_qty():
    """
    initialize_grid에서 내부적으로 사용되므로,
    여기서는 참고용으로만 유지 (또는 삭제해도 무방)
    """
    return MIN_QUANTITY

def get_current_price():
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if ticker and len(ticker) > 0 and ticker[0].last:
            return Decimal(str(ticker[0].last))
        return Decimal("0")
    except Exception as e:
        log("❌", f"Price fetch error: {e}")
        return Decimal("0")

# =============================================================================
# 리밸런싱 로직 (손실 가중치 강화!)
# =============================================================================
def check_rebalancing_condition(tp_profit, current_loss):
    global last_no_position_time
    try:
        if last_no_position_time == 0:
            return False
        elapsed = time.time() - last_no_position_time
        if elapsed < REBALANCE_SECONDS:
            return False
            
        loss_threshold = current_loss * Decimal("0.8")
        if tp_profit > loss_threshold:
            log("🔔 REBALANCE", f"Aggressive Condition met: TP {tp_profit:.2f} > Loss {current_loss:.2f}")
            return True
        return False
    except Exception as e:
        log("❌ REBALANCE", f"Check error: {e}")
        return False

def execute_rebalancing_sl():
    try:
        sync_position()
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        if long_size == 0 and short_size == 0:
            return
        log("🔔 REBALANCE", "Executing SL market orders...")
        if long_size > 0:
            close_qty = adjust_quantity_step(long_size)
            order = FuturesOrder(contract=SYMBOL, size=f"-{str(close_qty)}", price="0", tif="ioc", reduce_only=True, text=generate_order_id())
            api.create_futures_order(SETTLE, order)
            log("✅ REBALANCE", f"LONG {close_qty} SL executed")
        time.sleep(0.3)
        if short_size > 0:
            close_qty = adjust_quantity_step(short_size)
            order = FuturesOrder(contract=SYMBOL, size=str(close_qty), price="0", tif="ioc", reduce_only=True, text=generate_order_id())
            api.create_futures_order(SETTLE, order)
            log("✅ REBALANCE", f"SHORT {close_qty} SL executed")
        time.sleep(0.5)
        sync_position()
        log("✅ REBALANCE", "Complete!")
    except Exception as e:
        log("❌ REBALANCE", f"Execution error: {e}")

def handle_non_main_position_tp(non_main_size_at_tp):
    try:
        sync_position()
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        with balance_lock:
            capital = initial_capital if initial_capital > 0 else account_balance
       
        if long_size > short_size:
            main_size = long_size
            main_side = "long"
        else:
            main_size = short_size
            main_side = "short"
       
        current_price = get_current_price()
        if current_price == 0: return
       
        main_position_value = Decimal(str(main_size)) * current_price
        if main_position_value < capital * Decimal("1.0"): return
       
        if capital * Decimal("1.0") <= main_position_value < capital * Decimal("2.0"):
            sl_qty = Decimal(str(non_main_size_at_tp)) * Decimal("0.8")
            tier = "Tier-1 (0.8x)"
        else:
            sl_qty = Decimal(str(non_main_size_at_tp)) * Decimal("1.5")
            tier = "Tier-2 (1.5x)"
       
        sl_qty = adjust_quantity_step(sl_qty)
        if sl_qty < MIN_QUANTITY: sl_qty = MIN_QUANTITY
        if sl_qty > main_size: sl_qty = main_size
       
        log("💊 TP HANDLER", f"{tier}: {non_main_size_at_tp} TP → {main_side.upper()} {sl_qty} SL")
        
        order_size_str = f"-{str(sl_qty)}" if main_side == "long" else str(sl_qty)
        order = FuturesOrder(contract=SYMBOL, size=order_size_str, price="0", tif="ioc", reduce_only=True, text=generate_order_id())
        api.create_futures_order(SETTLE, order)
        log("✅ TP HANDLER", f"{main_side.upper()} {sl_qty} SL 완료!")
        time.sleep(0.5)
        sync_position()
    except Exception as e:
        log("❌ TP HANDLER", f"Error: {e}")

def update_no_position_time():
    global last_no_position_time
    with position_lock:
        long_size = position_state[SYMBOL]["long"]["size"]
        short_size = position_state[SYMBOL]["short"]["size"]
    if long_size == 0 and short_size == 0:
        if last_no_position_time == 0:
            last_no_position_time = time.time()
            log("📊 NO POSITION", "Time recorded for rebalancing")
    else:
        last_no_position_time = 0

def update_event_time():
    global last_event_time, idle_entry_count
    last_event_time = time.time()
    idle_entry_count = 0

def validate_strategy_consistency():
    try:
        sync_position()
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        current_price = get_current_price()
        if current_price == 0: return
        
        try:
            orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
            # ★ [수정] is_reduce_only 사용
            grid_count = sum(1 for o in orders if not o.is_reduce_only)
        except: return
        
        single_position = (long_size > 0 or short_size > 0) and not (long_size > 0 and short_size > 0)
        if single_position and grid_count == 0:
            log("🔧 VALIDATE", "Single position without grids → Creating grids!")
            initialize_grid(current_price)
    except Exception as e:
        log("❌", f"Validation error: {e}")

def remove_duplicate_orders():
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
        seen_orders = {}
        duplicates = []
        for o in orders:
            # ★ [수정] is_reduce_only 사용
            key = f"{o.size}_{o.price}_{o.is_reduce_only}"
            if key in seen_orders: duplicates.append(o.id)
            else: seen_orders[key] = o.id
        for order_id in duplicates:
            try:
                api.cancel_futures_order(SETTLE, SYMBOL, order_id)
                time.sleep(0.1)
            except: pass
    except: pass

def cancel_stale_orders():
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
        now = time.time()
        for o in orders:
            if hasattr(o, 'create_time') and o.create_time:
                order_age = now - float(o.create_time)
                if order_age > 86400:
                    api.cancel_futures_order(SETTLE, SYMBOL, o.id)
                    time.sleep(0.1)
    except: pass

def initialize_grid(current_price=None):
    global last_grid_time
    if not initialize_grid_lock.acquire(blocking=False):
        log("🔒 GRID", "Already running → skip")
        return
    try:
        now = time.time()
        if now - last_grid_time < 10:
            return
        last_grid_time = now
        price = current_price if current_price and current_price > 0 else get_current_price()
        if price == 0: return

        sync_position()
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]

        base_value = Decimal(str(account_balance)) * BASERATIO
        base_qty = float(Decimal(str(base_value)) / Decimal(str(price)))
        obv_display = float(obv_macd_value) * 100
        obv_multiplier = float(calculate_obv_macd_weight(obv_display))

        # 3️⃣ ★ [수정] 손실 가중치 (Loss Multiplier) 추가
        loss_multiplier = Decimal("1.0")
        try:
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
                long_entry = position_state[SYMBOL]["long"]["entry_price"]
                short_entry = position_state[SYMBOL]["short"]["entry_price"]
                
            main_side = "none"
            if long_size > short_size: main_side = "long"
            elif short_size > long_size: main_side = "short"
                
            if main_side == "long" and price < long_entry:
                loss_rate = (long_entry - price) / long_entry
                loss_multiplier = Decimal("1.0") + (loss_rate * Decimal("2"))
                log("📉 LOSS WEIGHT", f"Main(LONG) Loss {loss_rate*100:.2f}% -> Multiplier {loss_multiplier:.2f}")

            elif main_side == "short" and price > short_entry:
                loss_rate = (price - short_entry) / short_entry
                loss_multiplier = Decimal("1.0") + (loss_rate * Decimal("2"))
                log("📉 LOSS WEIGHT", f"Main(SHORT) Loss {loss_rate*100:.2f}% -> Multiplier {loss_multiplier:.2f}")
                
            if loss_multiplier > Decimal("3.0"): loss_multiplier = Decimal("3.0")
                
        except Exception as e:
            log("⚠️ QTY", f"Loss multiplier error: {e}")
            loss_multiplier = Decimal("1.0")

        if obv_display > 0:
            short_qty = safe_order_qty(base_qty * (1 + obv_multiplier) * float(loss_multiplier))
            long_qty = safe_order_qty(base_qty * float(loss_multiplier))
        elif obv_display < 0:
            long_qty = safe_order_qty(base_qty * (1 + obv_multiplier) * float(loss_multiplier))
            short_qty = safe_order_qty(base_qty * float(loss_multiplier))
        else:
            long_qty = safe_order_qty(base_qty * float(loss_multiplier))
            short_qty = safe_order_qty(base_qty * float(loss_multiplier))

        long_qty = adjust_quantity_step(long_qty)
        short_qty = adjust_quantity_step(short_qty)

        log("INFO", f"[GRID] init, LONG={long_qty}, SHORT={short_qty}, OBV={obv_macd_value:.4f}, mult={obv_multiplier:.2f}, loss={loss_multiplier:.2f}")

        try:
            order = FuturesOrder(contract=SYMBOL, size=str(long_qty), price="0", tif="ioc", reduce_only=False, text=generate_order_id())
            api.create_futures_order(SETTLE, order)
            log("✅GRID", f"long {long_qty}")
        except Exception as e: log("❌", f"long grid entry error: {e}")

        time.sleep(0.2)
        try:
            order = FuturesOrder(contract=SYMBOL, size=f"-{str(short_qty)}", price="0", tif="ioc", reduce_only=False, text=generate_order_id())
            api.create_futures_order(SETTLE, order)
            log("✅GRID", f"short {short_qty}")
        except Exception as e: log("❌", f"short grid entry error: {e}")

        log("✅ GRID", "Grid orders entry completed")
        
        # ★ [핵심 수정] 주문 완료 후 이벤트 타임 갱신 (아이들 리셋)
        update_event_time()
        
        time.sleep(1.0)
        sync_position()
        refresh_all_tp_orders()
        log("✅ GRID", "Initial TP orders created")

    except Exception as e:
        log("❌ GRID", f"Init error: {e}")
    finally:
        initialize_grid_lock.release()

def full_refresh(event_type, skip_grid=False):
    log_event_header(f"FULL REFRESH: {event_type}")
    log("🔄 SYNC", "Syncing position...")
    sync_position()
    update_no_position_time()
    log_position_state()
    cancel_all_orders()
    time.sleep(0.5)
    if not skip_grid:
        current_price = get_current_price()
        if current_price > 0:
            initialize_grid(current_price)
    refresh_all_tp_orders()
    sync_position()
    log_position_state()
    log("✅ REFRESH", f"Complete: {event_type}")

def calculate_obv_macd():
    global obv_macd_value
    try:
        if len(kline_history) < 60: return
        closes = [k['close'] for k in kline_history]
        volumes = [k['volume'] for k in kline_history]
        obv = [0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]: obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i-1]: obv.append(obv[-1] - volumes[i])
            else: obv.append(obv[-1])
        
        def ema(data, period):
            ema_vals = []
            k = 2 / (period + 1)
            ema_vals.append(sum(data[:period]) / period)
            for price in data[period:]:
                ema_vals.append(price * k + ema_vals[-1] * (1 - k))
            return ema_vals
        
        if len(obv) >= 60:
            ema_12 = ema(obv[-60:], 12)
            ema_26 = ema(obv[-60:], 26)
            if len(ema_12) > 0 and len(ema_26) > 0:
                macd_line = ema_12[-1] - ema_26[-1]
                max_obv = max(abs(max(obv[-60:])), abs(min(obv[-60:])))
                if max_obv > 0:
                    normalized = macd_line / max_obv / 100
                    obv_macd_value = Decimal(str(normalized))
                    display_value = float(obv_macd_value) * 100
                    if abs(display_value) > 0.1:
                        log("📊 OBV-MACD", f"{display_value:.2f}")
    except Exception as e:
        log("❌ OBV-MACD", f"Calculation error: {e}")

def calculate_dynamic_tp_gap():
    try:
        obv_display = float(obv_macd_value) * 100
        obv_abs = abs(obv_display)
        if obv_abs < 10: tp_ratio = Decimal("0.3")
        elif obv_abs < 20: tp_ratio = Decimal("0.5")
        elif obv_abs < 30: tp_ratio = Decimal("0.7")
        elif obv_abs < 50: tp_ratio = Decimal("0.85")
        else: tp_ratio = Decimal("1.0")
        
        tp_range = TPMAX - TPMIN
        dynamic_tp = TPMIN + (tp_range * tp_ratio)
        return (dynamic_tp, dynamic_tp)
    except: return (TPMIN, TPMIN)

def fetch_kline_thread():
    global obv_macd_value
    last_fetch = 0
    while True:
        try:
            current_time = time.time()
            if current_time - last_fetch < 60:
                time.sleep(5)
                continue
            try:
                candles = api.list_futures_candlesticks(SETTLE, contract=SYMBOL, interval='3m', limit=200)
                if candles and len(candles) > 0:
                    kline_history.clear()
                    for candle in candles:
                        kline_history.append({
                            'close': float(candle.c), 'high': float(candle.h),
                            'low': float(candle.l), 'volume': float(candle.v) if hasattr(candle, 'v') and candle.v else 0,
                        })
                    calculate_obv_macd()
                    if len(kline_history) >= 60 and obv_macd_value != Decimal("0"):
                        log("✅ OBV", "OBV MACD calculation started!")
                    last_fetch = current_time
            except: time.sleep(10)
        except: time.sleep(10)

async def watch_positions():
    global last_price
    while True:
        try:
            url = f"wss://fx-ws.gateio.ws/v4/ws/usdt"
            async with websockets.connect(url, ping_interval=60, ping_timeout=120, close_timeout=10) as ws:
                await ws.send(json.dumps({"time": int(time.time()), "channel": "futures.tickers", "event": "subscribe", "payload": [SYMBOL]}))
                log("✅ WS", "Connected to WebSocket")
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=150)
                    data = json.loads(msg)
                    if data.get("event") == "update" and data.get("channel") == "futures.tickers":
                        result = data.get("result")
                        if result and isinstance(result, dict):
                            price = float(result.get("last", 0))
                            if price > 0: last_price = price
        except Exception as e:
            log("⚠️ WS", f"Reconnecting: {e}")
            await asyncio.sleep(5)

def start_websocket():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(watch_positions())

def position_monitor():
    prev_long_size = Decimal("-1")
    prev_short_size = Decimal("-1")
    while True:
        try:
            time.sleep(5)
            sync_position()
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
                long_price = position_state[SYMBOL]["long"]["entry_price"]
                short_price = position_state[SYMBOL]["short"]["entry_price"]
            
            if long_size != prev_long_size or short_size != prev_short_size:
                if prev_long_size != Decimal("-1"):
                    log("🔄 CHANGE", f"Long {prev_long_size}→{long_size} | Short {prev_short_size}→{short_size}")
                prev_long_size = long_size
                prev_short_size = short_size
            
            with balance_lock: balance = account_balance
            max_value = balance * MAXPOSITIONRATIO
            long_value = long_price * long_size
            short_value = short_price * short_size
            
            if long_value >= max_value and not max_position_locked["long"]:
                log_event_header("MAX POSITION LIMIT")
                log("⚠️ LIMIT", f"LONG ${long_value:.2f} >= ${max_value:.2f}")
                max_position_locked["long"] = True
                cancel_all_orders()
            
            if short_value >= max_value and not max_position_locked["short"]:
                log_event_header("MAX POSITION LIMIT")
                log("⚠️ LIMIT", f"SHORT ${short_value:.2f} >= ${max_value:.2f}")
                max_position_locked["short"] = True
                cancel_all_orders()
            
            if long_value < max_value and max_position_locked["long"]:
                log("✅ UNLOCK", f"LONG ${long_value:.2f} < ${max_value:.2f}")
                max_position_locked["long"] = False
            if short_value < max_value and max_position_locked["short"]:
                log("✅ UNLOCK", f"SHORT ${short_value:.2f} < ${max_value:.2f}")
                max_position_locked["short"] = False
        except: time.sleep(5)

async def grid_fill_monitor():
    uri = f"wss://fx-ws.gateio.ws/v4/ws/{SETTLE}"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=60, ping_timeout=120, close_timeout=10) as ws:
                await ws.send(json.dumps({"time": int(time.time()), "channel": "futures.orders", "event": "subscribe", "payload": [API_KEY, API_SECRET, SYMBOL]}))
                log("✅ WS", "Connected to WebSocket")
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=150)
                    data = json.loads(msg)
                    if data.get("event") == "update" and data.get("channel") == "futures.orders":
                        for order_data in data.get("result", []):
                            if order_data.get("contract") != SYMBOL: continue
                            is_filled = (order_data.get("finish_as") in ["filled", "ioc"] or order_data.get("status") in ["finished", "closed"])
                            if not is_filled: continue
                            
                            is_reduce_only = order_data.get("is_reduce_only", False)
                            size = order_data.get("size", 0)
                            price = float(order_data.get("price", 0))
                            
                            if is_reduce_only:
                                side = "long" if size > 0 else "short"
                                tp_qty = abs(int(size))
                                tp_profit = Decimal(str(tp_qty)) * Decimal(str(price))
                                log("✅ TP FILLED", f"{side.upper()} {tp_qty} @ {price:.4f}")
                                time.sleep(0.5)
                                sync_position()
                                
                                with position_lock:
                                    if side == "long": remaining_loss = position_state[SYMBOL]["short"]["size"] * get_current_price()
                                    else: remaining_loss = position_state[SYMBOL]["long"]["size"] * get_current_price()
                                if check_rebalancing_condition(tp_profit, remaining_loss): execute_rebalancing_sl()
                                
                                try: handle_non_main_position_tp(tp_qty)
                                except: pass
                                time.sleep(0.5)
                                with position_lock:
                                    long_size = position_state[SYMBOL]["long"]["size"]
                                    short_size = position_state[SYMBOL]["short"]["size"]
                                update_event_time()
                                
                                if long_size == 0 and short_size == 0:
                                    log("🎯 BOTH CLOSED", "Both sides closed → Full refresh")
                                    update_no_position_time()
                                    threading.Thread(target=full_refresh, args=("Average_TP", False), daemon=True).start()
                                else:
                                    log("🎯 SIDE CLOSED", "One side closed → Re-initializing Grid/Hedge")
                                    threading.Thread(target=full_refresh, args=("Side_TP", False), daemon=True).start()
        except: await asyncio.sleep(5)

def start_grid_monitor():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(grid_fill_monitor())

def tp_monitor():
    while True:
        try:
            time.sleep(3)
            for side in ["long", "short"]:
                tp_id = average_tp_orders[SYMBOL].get(side)
                if not tp_id: continue
                try:
                    order = api.get_futures_order(SETTLE, str(tp_id))
                    if order and order.status in ["finished", "closed"]:
                        log_event_header("AVERAGE TP HIT")
                        log("🎯 TP", f"{side.upper()} average position closed")
                        average_tp_orders[SYMBOL][side] = None
                        time.sleep(0.5)
                        sync_position()
                        full_refresh("Average_TP", skip_grid=False)
                        update_event_time()
                        break
                except: pass
        except: time.sleep(1)

def check_idle_and_enter():
    global idle_entry_in_progress, last_idle_entry_time, idle_entry_count
    try:
        with idle_entry_progress_lock:
            if idle_entry_in_progress:
                return

        current_time = time.time()
        elapsed = current_time - last_event_time

        if current_time - last_idle_entry_time < IDLE_ENTRY_COOLDOWN:
            log("IDLE-DEBUG", f"cooldown block: {current_time - last_idle_entry_time:.1f}s < {IDLE_ENTRY_COOLDOWN}")
            return

        if elapsed < IDLE_TIME_SECONDS:
            log("IDLE-DEBUG", f"elapsed block: {elapsed:.1f}s < {IDLE_TIME_SECONDS}")
            return

        sync_position()
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]

        with balance_lock:
            balance = account_balance
        current_price = get_current_price()
        if current_price == 0:
            log("IDLE-DEBUG", "price == 0")
            return

        total_position_value = (long_size + short_size) * current_price
        max_allowed_value = balance * MAXPOSITIONRATIO
        if total_position_value >= max_allowed_value:
            log("IDLE-DEBUG", f"max-pos block: pos={total_position_value:.2f}, limit={max_allowed_value:.2f}")
            return

        # 아이들 진입 시작
        with idle_entry_progress_lock:
            idle_entry_in_progress = True
        
        try:
            idle_entry_count += 1
            log_event_header(f"IDLE ENTRY #{idle_entry_count}")
            log("⏰ IDLE", f"No activity for {elapsed/60:.1f} min → Adding Grid/Hedge")
            
            # 시장가 양방향 진입 (물타기/헷징)
            if current_price > 0:
                initialize_grid(current_price)
                last_idle_entry_time = current_time
                update_event_time() # 이벤트 시간 갱신하여 연속 진입 방지
                
        finally:
            with idle_entry_progress_lock:
                idle_entry_in_progress = False
        
    except Exception as e:
        log("❌ IDLE", f"Error: {e}")
        with idle_entry_progress_lock:
            idle_entry_in_progress = False

def idle_monitor():
    global last_idle_check
    while True:
        try:
            time.sleep(60)
            current_time = time.time()
            if current_time - last_idle_check < 120: continue
            last_idle_check = current_time
            check_idle_and_enter()
        except: time.sleep(10)

def get_tp_orders_hash(tp_orders):
    try:
        if not tp_orders: return ""
        order_strings = []
        for o in tp_orders: 
            # ★ [수정] is_reduce_only 사용
            order_strings.append(f"{o.size}_{o.price}_{o.is_reduce_only}")
        order_strings.sort()
        return hashlib.md5("_".join(order_strings).encode()).hexdigest()
    except: return ""

def periodic_health_check():
    """2분마다 실행되는 헬스 체크 (아이들 시간 디버그 포함)"""
    global last_adjusted_obv, tp_gap_min, tp_gap_max, last_event_time
    while True:
        try:
            time.sleep(120)
            
            # ★ 아이들 시간 디버그 로그 (소수점 정리)
            current_time = time.time()
            idle_time = current_time - last_event_time
            log("💊 HEALTH", f"Starting check... (Idle: {idle_time:.1f}s / {IDLE_TIME_SECONDS}s)")
            
            try:
                futures_account = api.list_futures_accounts(SETTLE)
                if futures_account and getattr(futures_account, 'available', None):
                    avail = Decimal(str(futures_account.available))
                    sync_position()
                    with position_lock:
                        l_s = position_state[SYMBOL]["long"]["size"]
                        s_s = position_state[SYMBOL]["short"]["size"]
                    if l_s == 0 and s_s == 0 and avail > 0:
                        with balance_lock:
                            global initial_capital, account_balance
                            initial_capital = avail
                            account_balance = avail
                        save_initial_capital()
                        log("💰 BALANCE", f"{avail:.2f} USDT (Init Cap Updated)")
            except: pass

            sync_position()
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
            if long_size == 0 and short_size == 0: continue

            try:
                orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
                # ★ is_reduce_only 사용
                grid_count = sum(1 for o in orders if not o.is_reduce_only)
                tp_orders_list = [o for o in orders if o.is_reduce_only]
                
                tp_count = len(tp_orders_list)
                log("📊 ORDERS", f"Grid(Open): {grid_count}, TP: {tp_count}")

                current_hash = get_tp_orders_hash(tp_orders_list)
                previous_hash = tp_order_hash.get(SYMBOL)
                
                tp_mismatch = False
                if (long_size > 0 or short_size > 0) and len(tp_orders_list) < 2:
                    log("🔧 HEALTH", "TP Count Mismatch")
                    tp_mismatch = True
                
                if tp_mismatch or (current_hash != previous_hash):
                    log("🔧 HEALTH", "TP Refreshing...")
                    refresh_all_tp_orders()
                    tp_order_hash[SYMBOL] = current_hash
            except: pass
            
            try:
                calculate_obv_macd()
                current_obv = float(obv_macd_value) * 100
                if last_adjusted_obv == 0: last_adjusted_obv = current_obv
                else:
                    if abs(current_obv - last_adjusted_obv) >= 10:
                        log("🔔 HEALTH", "OBV changed → Recalculating TP")
                        tp_result = calculate_dynamic_tp_gap()
                        new_tp_long = tp_result[0] if isinstance(tp_result, tuple) else Decimal(str(tp_result))
                        if abs(float(new_tp_long) - float(tp_gap_min)) >= 0.0001:
                            tp_gap_min = new_tp_long
                            refresh_all_tp_orders()
                            last_adjusted_obv = current_obv
            except: pass

            try:
                single_position = (long_size > 0 or short_size > 0) and not (long_size > 0 and short_size > 0)
                
                # 좀비 그리드 제거 로직
                if single_position and grid_count > 0:
                    log("⚠️ SINGLE", f"Zombie grid detected ({grid_count}) → Clearing...")
                    cancel_all_orders()
                    time.sleep(0.5)
                    refresh_all_tp_orders()
                    grid_count = 0

                if single_position and grid_count == 0:
                    current_price = get_current_price()
                    if current_price > 0:
                        log("⚠️ SINGLE", "Creating grid from single position...")
                        initialize_grid(current_price)
            except: pass

            validate_strategy_consistency()
            remove_duplicate_orders()
            cancel_stale_orders()
            log("✅ HEALTH", "Complete")
        except: time.sleep(5)

@app.route('/webhook', methods=['POST'])
def webhook():
    global obv_macd_value
    try:
        data = request.get_json()
        tt1 = data.get('tt1', 0)
        obv_macd_value = Decimal(str(tt1 / 1000.0))
        return jsonify({"status": "success"}), 200
    except: return jsonify({"status": "error"}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "running"}), 200

def print_startup_summary():
    global account_balance, initial_capital
    log("🚀 START", "GATE Trading Bot")
    load_initial_capital()
    try:
        futures_account = api.list_futures_accounts(SETTLE)
        if futures_account and getattr(futures_account, 'available', None):
            avail = Decimal(str(futures_account.available))
            if avail > 0:
                sync_position()
                with position_lock:
                    l_s = position_state[SYMBOL]["long"]["size"]
                    s_s = position_state[SYMBOL]["short"]["size"]
                if l_s == 0 and s_s == 0:
                    with balance_lock:
                        initial_capital = avail
                        account_balance = avail
                    save_initial_capital()
                else:
                    if initial_capital > 0:
                        with balance_lock: account_balance = initial_capital
                    else:
                        with balance_lock:
                            initial_capital = avail
                            account_balance = avail
                        save_initial_capital()
    except: pass
    
    try:
        current_price = get_current_price()
        if current_price > 0:
            log("💵 PRICE", f"{current_price:.4f}")
            cancel_all_orders()
            time.sleep(0.5)
            with position_lock:
                l_s = position_state[SYMBOL]["long"]["size"]
                s_s = position_state[SYMBOL]["short"]["size"]
            if l_s > 0 or s_s > 0:
                refresh_all_tp_orders()
                if (l_s > 0 and s_s == 0) or (l_s == 0 and s_s > 0):
                    initialize_grid(current_price)
            else:
                initialize_grid(current_price)
    except: pass

if __name__ == '__main__':
    if not API_KEY or not API_SECRET: exit(1)
    update_event_time()
    print_startup_summary()
    threading.Thread(target=fetch_kline_thread, daemon=True).start()
    threading.Thread(target=start_websocket, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=start_grid_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    threading.Thread(target=idle_monitor, daemon=True).start()
    threading.Thread(target=periodic_health_check, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
