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
SYMBOL = os.environ.get("SYMBOL", "BNB_USDT")  # ← BNB로 변경!
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
BASERATIO = Decimal("0.01")                 # ← 기본 수량 비율 (1%로 변경!)
MAXPOSITIONRATIO = Decimal("3.0")          # 최대 포지션 비율 (3배)
HEDGE_RATIO_MAIN = Decimal("0.10")           # 주력 헤지 비율 (10%)

# TP 설정 (동적 TP)
TPMIN = Decimal("0.0021")                   # 최소 TP (0.21%)
TPMAX = Decimal("0.004")                    # 최대 TP (0.4%)

# 시간 설정
IDLE_TIME_SECONDS = 600                      # 아이들 감지 시간 (10분)
IDLE_TIMEOUT = 600                           # 아이들 타임아웃 (10분)
IDLE_ENTRY_COOLDOWN = 10                     # 아이들 진입 쿨다운 (10초)
REBALANCE_SECONDS = 5 * 3600                 # ← 리밸런싱 시간 (5시간)

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
    contracts = api.list_futures_contracts(SETTLE)
    for c in contracts:
        if c.name == symbol:
            # Gate API의 실제 속성명 사용
            return Decimal(str(c.order_size_min)), int(c.order_size_digits)
    # fallback 기본값
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
last_no_position_time = 0  # ← 리밸런싱용 무포 시점 기록

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
    """
    Initial Capital을 파일에 저장
    """
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
    """
    파일에서 Initial Capital 로드
    서버 재시작 시 호출
    """
    global initial_capital
   
    try:
        if os.path.exists(CAPITAL_FILE):
            with open(CAPITAL_FILE, 'r') as f:
                data = json.load(f)
           
            loaded_capital = Decimal(data.get("initial_capital", "0"))
            saved_symbol = data.get("symbol", "")
            timestamp = data.get("timestamp", 0)
           
            # 심볼이 일치하고, 저장된 자본금이 유효하면 로드
            if saved_symbol == SYMBOL and loaded_capital > 0:
                initial_capital = loaded_capital
                saved_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                log("📂 LOAD", f"Initial Capital loaded: {initial_capital:.2f} USDT (saved at {saved_time})")
                return True
            else:
                log("⚠️ LOAD", f"Invalid saved data (symbol mismatch or zero capital)")
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
    """
    Gate.io 고유한 주문 ID 생성
    - 반드시 't-'로 시작해야 함! (Gate.io API 요구사항)
    - 형식: t-{timestamp}_{sequence}
    """
    global order_sequence_id
   
    order_sequence_id += 1
    timestamp = int(time.time() * 1000)  # 밀리초 단위
    unique_id = f"t-{timestamp}_{order_sequence_id}"  # ← ✅ 't-' 접두사 추가!
   
    return unique_id


# =============================================================================
# 로그
# =============================================================================
def log(tag, msg):
    """로그 출력 (divider 처리 추가)"""
    if tag == "divider":
        logger.info(msg)  # 그대로 출력
    else:
        logger.info(f"[{tag}] {msg}")

def log_divider(char="=", length=80):
    logger.info(char * length)

def log_event_header(event_name):
    log_divider("-")
    log("🔔 EVENT", event_name)
    log_divider("-")

def get_main_side():
    """주력 포지션 판별 (더 큰 쪽)"""
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
    """현재 포지션 상태 로그"""
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
# 포지션 동기화 - 에러 시 재시도 간격 증가
# =============================================================================
def sync_position(max_retries=3, retry_delay=2):
    """포지션 동기화 (재시도 로직 포함)"""
    for attempt in range(max_retries):
        try:
            positions = api.list_positions(SETTLE)
           
            with position_lock:
                position_state[SYMBOL]["long"]["size"] = Decimal("0")
                position_state[SYMBOL]["long"]["entry_price"] = Decimal("0")
                position_state[SYMBOL]["short"]["size"] = Decimal("0")
                position_state[SYMBOL]["short"]["entry_price"] = Decimal("0")
           
            if positions:
                for p in positions:
                    if p.contract == SYMBOL:
                        size_dec = Decimal(str(p.size))
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
           
        except GateApiException as e:
            if "INVALID_PARAM_VALUE" in str(e):  # ← 명확한 오류 구분
                log("❌ SYNC", f"API parameter error: {e}")
                return False
            elif attempt < max_retries - 1:
                log("⚠️ RETRY", f"Position sync attempt {attempt + 1}/{max_retries} failed, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                log("❌ SYNC", f"Position sync failed after {max_retries} attempts: {e}")
                return False
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                log("❌ SYNC", f"Unexpected error: {e}")
                return False
   
    return False


# =============================================================================
# 주문 취소
# =============================================================================
def cancel_all_orders():
    """모든 오픈 주문 취소 + grid_orders 초기화"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
       
        if not orders:
            log("[ℹ️]", "No open orders to cancel")
            return
       
        log("[❌ CANCEL]", f"Cancelling {len(orders)} orders...")
       
        cancelled_count = 0
        for order in orders:
            try:
                api.cancel_futures_order(SETTLE, order.id)
                cancelled_count += 1
                time.sleep(0.05)  # API 제한 고려
            except GateApiException as e:
                if "ORDER_NOT_FOUND" not in str(e) and "CANCEL_IN_PROGRESS" not in str(e):
                    log("[⚠️]", f"Cancel order {order.id}: {e}")
            except Exception as e:
                log("[⚠️]", f"Cancel order {order.id}: {e}")
       
        # ✅ grid_orders 초기화
        if SYMBOL in grid_orders:
            grid_orders[SYMBOL] = {"long": [], "short": []}
       
        if SYMBOL in average_tp_orders:
            average_tp_orders[SYMBOL] = {"long": None, "short": None}
       
        log("[✅ CANCEL]", f"{cancelled_count}/{len(orders)} orders cancelled")
       
    except GateApiException as e:
        if "400" in str(e):
            log("[❌]", "Cancel orders: API authentication error")
        else:
            log("[❌]", f"Order cancellation error: {e}")
    except Exception as e:
        log("[❌]", f"Order cancellation error: {e}")

def cancel_tp_only():
    """TP 주문만 취소 (그리드는 유지)"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
       
        tp_orders = [o for o in orders if o.is_reduce_only]
       
        if len(tp_orders) == 0:
            log("ℹ️ TP", "No TP orders to cancel")
            return
       
        log("🗑️ TP", f"Cancelling {len(tp_orders)} TP orders")
       
        for order in tp_orders:
            try:
                api.cancel_futures_order(SETTLE, order.id)
                time.sleep(0.1)
            except GateApiException as e:
                if "ORDER_NOT_FOUND" not in str(e):
                    log("⚠️", f"TP cancel error: {e}")
            except:
                pass
   
    except GateApiException as e:
        if "400" in str(e):
            log("⚠️", "Cancel TP: API authentication error")
        else:
            log("❌", f"TP cancel error: {e}")
    except Exception as e:
        log("❌", f"TP cancel error: {e}")


# ============================================================================
# TP 새로고침 (동적 TP)
# ============================================================================

def refresh_all_tp_orders():
    """TP 주문 새로고침 (동적 TP 적용)"""
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
       
        if not isinstance(long_tp, Decimal):
            long_tp = Decimal(str(long_tp))
        if not isinstance(short_tp, Decimal):
            short_tp = Decimal(str(short_tp))
       
        cancel_tp_only()
        time.sleep(0.5)
       
        # LONG TP
        if long_size > 0 and long_entry_price > 0:
            tp_price_long = long_entry_price * (Decimal("1") + long_tp)
            tp_price_long = tp_price_long.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            
            # ✅ 수정: long_size 사용, adjust_quantity_step 적용
            long_qty = adjust_quantity_step(long_size)
            
            order = FuturesOrder(
                contract=SYMBOL,
                size=str(-long_qty),  # TP는 마이너스
                price=str(tp_price_long),
                tif="gtc",
                reduce_only=True,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ TP LONG", f"Qty: {long_qty}, Price: {float(tp_price_long):.4f}")
       
        time.sleep(0.3)
       
        # SHORT TP
        if short_size > 0 and short_entry_price > 0:
            tp_price_short = short_entry_price * (Decimal("1") - short_tp)
            tp_price_short = tp_price_short.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            
            # ✅ 수정: short_size 사용, adjust_quantity_step 적용
            short_qty = adjust_quantity_step(short_size)
            
            order = FuturesOrder(
                contract=SYMBOL,
                size=str(short_qty),  # SHORT TP는 플러스
                price=str(tp_price_short),
                tif="gtc",
                reduce_only=True,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ TP SHORT", f"Qty: {short_qty}, Price: {float(tp_price_short):.4f}")
       
        log("✅ TP", "All TP orders created successfully")
   
    except Exception as e:
        log("❌ TP REFRESH", f"Error: {e}")


# =============================================================================
# 수량 계산 함수 수정
# =============================================================================
def calculate_obv_macd_weight(obv_value):
    """
    ← 가중치 강화! (기존 대비 multiplier 범위 확대)
    OBV 절댓값이 클수록 추세가 강함 → 더 많이 진입!
    """
    obv_abs = abs(obv_value)
   
    # ★ 가중치 강화 (20 이상일 때 1.1부터 시작, 최대 2.0)
    if obv_abs < 20:
        multiplier = Decimal("1.0")
    elif obv_abs < 30:
        multiplier = Decimal("1.1")   # ← 추가!
    elif obv_abs < 40:
        multiplier = Decimal("1.3")
    elif obv_abs < 50:
        multiplier = Decimal("1.5")
    elif obv_abs < 60:
        multiplier = Decimal("1.6")
    elif obv_abs < 70:
        multiplier = Decimal("1.7")
    elif obv_abs < 100:
        multiplier = Decimal("1.9")
    else:
        multiplier = Decimal("2.0")
   
    return multiplier


def safe_order_qty(qty, min_qty=MIN_QUANTITY):
    """
    Gate.io BNBUSDT 선물에서 주문 수량을 최소 단위 이상으로 변환
    마켓 규칙에 따라 step/min_qty 반영
    """
    try:
        qty_float = float(qty)
        # min_qty 이상, 3자리 반올림(실거래에선 반내림·내림 권장→adjust_quantity_step 병행)
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
    """BNB 수량 계산 (최소 단위 적용)"""
    with balance_lock:
        base_value = Decimal(str(account_balance)) * BASERATIO
        base_qty = base_value / get_current_price() if get_current_price() > 0 else Decimal("0")
        
        if base_qty < MIN_QUANTITY:
            base_qty = MIN_QUANTITY
   
    # OBV MACD 값 기준 동적 수량 조절
    obv_value = abs(float(obv_macd_value) * 100)
    multiplier = calculate_obv_macd_weight(obv_value)
   
    # ✅ Decimal 연산 유지
    final_qty = base_qty * multiplier
    
    # ✅ adjust_quantity_step 적용
    final_qty = adjust_quantity_step(final_qty)
    
    # 최종 검증
    if final_qty < MIN_QUANTITY:
        log("⚠️ QTY", f"Calculated qty {final_qty} < MIN {MIN_QUANTITY}, using MIN")
        final_qty = MIN_QUANTITY
    
    return final_qty


def get_current_price():
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if ticker and len(ticker) > 0 and ticker[0] and hasattr(ticker[0], 'last') and ticker[0].last:
            return Decimal(str(ticker[0].last))
        return Decimal("0")
    except (GateApiException, IndexError, AttributeError, ValueError) as e:
        log("❌", f"Price fetch error: {e}")
        return Decimal("0")

# =============================================================================
# 리밸런싱 로직 추가
# =============================================================================
def check_rebalancing_condition(tp_profit, current_loss):
    """
    리밸런싱 조건 체크:
    - 무포(리밸런싱 포함) 이후 최초 진입 5시간 경과
    - TP 체결 시 TP 수익 > 현재 손실
    → SL 시장가 처리
    """
    global last_no_position_time
    
    try:
        if last_no_position_time == 0:
            return False
            
        elapsed = time.time() - last_no_position_time
        
        # 5시간 경과 여부
        if elapsed < REBALANCE_SECONDS:
            return False
            
        # TP 수익 > 현재 손실
        if tp_profit > current_loss:
            log("🔔 REBALANCE", f"Condition met: TP {tp_profit:.2f} > Loss {current_loss:.2f} after {elapsed/3600:.1f}h")
            return True
            
        return False
        
    except Exception as e:
        log("❌ REBALANCE", f"Check error: {e}")
        return False

def execute_rebalancing_sl():
    """리밸런싱 SL 시장가 주문 실행"""
    try:
        sync_position()
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
            
        if long_size == 0 and short_size == 0:
            return
            
        log("🔔 REBALANCE", "Executing SL market orders...")
        
        # 롱 포지션 청산
        if long_size > 0:
            # ✅ adjust_quantity_step 적용
            close_qty = adjust_quantity_step(long_size)
            
            order = FuturesOrder(
                contract=SYMBOL,
                size=f"-{str(close_qty)}",
                price="0",
                tif="ioc",
                reduce_only=True,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ REBALANCE", f"LONG {close_qty} SL executed")
            
        time.sleep(0.3)
        
        # 숏 포지션 청산
        if short_size > 0:
            # ✅ adjust_quantity_step 적용
            close_qty = adjust_quantity_step(short_size)
            
            order = FuturesOrder(
                contract=SYMBOL,
                size=str(close_qty),
                price="0",
                tif="ioc",
                reduce_only=True,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ REBALANCE", f"SHORT {close_qty} SL executed")
            
        time.sleep(0.5)
        sync_position()
        log("✅ REBALANCE", "Complete!")
        
    except Exception as e:
        log("❌ REBALANCE", f"Execution error: {e}")


# =============================================================================
# 티어 계산 수정 (Initial Capital 기반 명확화)
# =============================================================================
def handle_non_main_position_tp(non_main_size_at_tp):
    """TP 체결 완료 시 물량 누적 방지 (Tier-1/2)"""
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
        if current_price == 0:
            log("❌ TP HANDLER", "Price fetch failed")
            return
       
        main_position_value = Decimal(str(main_size)) * current_price
       
        if main_position_value < capital * Decimal("1.0"):
            log("💊 TP HANDLER", f"Main {main_position_value:.2f} < {capital:.2f} (1배 미만) - skip")
            return
       
        # Tier 판정
        if capital * Decimal("1.0") <= main_position_value < capital * Decimal("2.0"):
            sl_qty = Decimal(str(non_main_size_at_tp)) * Decimal("0.8")
            tier = "Tier-1 (0.8x)"
        else:
            sl_qty = Decimal(str(non_main_size_at_tp)) * Decimal("1.5")
            tier = "Tier-2 (1.5x)"
       
        # ✅ adjust_quantity_step 적용
        sl_qty = adjust_quantity_step(sl_qty)
        
        if sl_qty < MIN_QUANTITY:
            sl_qty = MIN_QUANTITY
        if sl_qty > main_size:
            sl_qty = main_size
       
        log("💊 TP HANDLER", f"{tier}: {non_main_size_at_tp} TP → {main_side.upper()} {sl_qty} SL")
       
        # 시장가 청산 실행
        if main_side == "long":
            order_size_str = f"-{str(sl_qty)}"
        else:
            order_size_str = str(sl_qty)
       
        order = FuturesOrder(
            contract=SYMBOL,
            size=order_size_str,
            price="0",
            tif="ioc",
            reduce_only=True,
            text=generate_order_id()
        )
       
        api.create_futures_order(SETTLE, order)
        log("✅ TP HANDLER", f"{main_side.upper()} {sl_qty} SL 완료!")
       
        time.sleep(0.5)
        sync_position()
        log_position_state()
   
    except Exception as e:
        log("❌ TP HANDLER", f"Error: {e}")


# =============================================================================
# 무포 시점 기록 (리밸런싱용)
# =============================================================================
def update_no_position_time():
    """양방향 포지션이 모두 0일 때 시간 기록"""
    global last_no_position_time
    
    with position_lock:
        long_size = position_state[SYMBOL]["long"]["size"]
        short_size = position_state[SYMBOL]["short"]["size"]
    
    if long_size == 0 and short_size == 0:
        if last_no_position_time == 0:
            last_no_position_time = time.time()
            log("📊 NO POSITION", "Time recorded for rebalancing")
    else:
        last_no_position_time = 0  # 포지션 있으면 리셋

def update_event_time():
    """마지막 이벤트 시간 갱신 + 아이들 카운트 리셋"""
    global last_event_time, idle_entry_count
    last_event_time = time.time()
    idle_entry_count = 0  # ← 추가: 이벤트 발생 시 카운트 리셋

# =============================================================================
# TITLE 17-1. 전략 일관성 검증
# =============================================================================
def validate_strategy_consistency():
    """전략 일관성 검증 + 그리드 생성"""
    try:
        sync_position()
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        current_price = get_current_price()
        if current_price == 0:
            return
        
        try:
            orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
            grid_count = sum(1 for o in orders if not o.reduce_only)
        except Exception as e:
            log("❌", f"List orders error: {e}")
            return
        
        # ✅ 단일 포지션 + 그리드 없음 → 그리드 생성
        single_position = (long_size > 0 or short_size > 0) and not (long_size > 0 and short_size > 0)
        
        if single_position and grid_count == 0:
            log("🔧 VALIDATE", "Single position without grids → Creating grids!")
            initialize_grid(current_price)
            return
       
    except Exception as e:
        log("❌", f"Validation error: {e}")

# =============================================================================
# TITLE 17-2. 중복/오래된 주문 제거
# =============================================================================

def remove_duplicate_orders():
    """중복 주문 제거 (동일 가격/수량)"""
    try:
        # ✅ 수정: 명시적 키워드 인자
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
       
        seen_orders = {}
        duplicates = []
       
        for o in orders:
            key = f"{o.size}_{o.price}_{o.reduce_only}"
           
            if key in seen_orders:
                duplicates.append(o.id)
                log("🚨 DUPLICATE", f"Order {o.id}: {o.size} @ {o.price}")
            else:
                seen_orders[key] = o.id
       
        # 중복 주문 취소
        for order_id in duplicates:
            try:
                api.cancel_futures_order(SETTLE, SYMBOL, order_id)
                log("🗑️ DUPLICATE", f"Canceled order {order_id}")
                time.sleep(0.1)
            except:
                pass
               
    except Exception as e:
        log("❌", f"Remove duplicates error: {e}")

def cancel_stale_orders():
    """24시간 이상 오래된 주문 취소"""
    try:
        # ✅ 수정: 명시적 키워드 인자
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
        now = time.time()
       
        for o in orders:
            if hasattr(o, 'create_time') and o.create_time:
                order_age = now - float(o.create_time)
               
                if order_age > 86400:  # 24시간
                    api.cancel_futures_order(SETTLE, SYMBOL, o.id)
                    log("🗑️ STALE", f"Canceled order {o.id} (age: {order_age/3600:.1f}h)")
                    time.sleep(0.1)
    except Exception as e:
        log("❌", f"Cancel stale orders error: {e}")

# ============================================================================
# 1️⃣ initialize_grid() - 완전 코드 (한 줄도 생략 없음!)
# ============================================================================

def initialize_grid(current_price=None):
    """
    최초/리셋 시 양방향 grid진입 (0.001 단위 안전처리 포함)
    """
    global last_grid_time

    if not initialize_grid_lock.acquire(blocking=False):
        log("🔒 GRID", "Already running → skip")
        return

    try:
        now = time.time()
        if now - last_grid_time < 10:
            log("⏱️ GRID", f"Too soon ({now-last_grid_time:.1f}s) → skip")
            return

        last_grid_time = now

        price = current_price if current_price and current_price > 0 else get_current_price()
        if price == 0:
            log("❌ GRID", "Cannot get price")
            return

        sync_position()

        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]

        base_value = Decimal(str(account_balance)) * BASERATIO
        base_qty = float(Decimal(str(base_value)) / Decimal(str(price)))

        obv_display = float(obv_macd_value) * 100
        obv_multiplier = float(calculate_obv_macd_weight(obv_display))

        if obv_display > 0:
            short_qty = safe_order_qty(base_qty * (1 + obv_multiplier))
            long_qty = safe_order_qty(base_qty)
        elif obv_display < 0:
            long_qty = safe_order_qty(base_qty * (1 + obv_multiplier))
            short_qty = safe_order_qty(base_qty)
        else:
            long_qty = safe_order_qty(base_qty)
            short_qty = safe_order_qty(base_qty)

        # **여기 추가: 0.001 단위로 버림 처리**
        long_qty = adjust_quantity_step(long_qty)
        short_qty = adjust_quantity_step(short_qty)

        log("INFO", f"[GRID] init, LONG={long_qty}, SHORT={short_qty}, OBV={obv_macd_value}, mult={obv_multiplier}")

        # 주문 진입(Decimal, string으로 전달)
        try:
            order = FuturesOrder(
                contract=SYMBOL,
                size=str(long_qty),       # 반드시 str
                price="0",
                tif="ioc",
                reduce_only=False,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅GRID", f"long {long_qty}")
        except Exception as e:
            log("❌", f"long grid entry error: {e}")

        time.sleep(0.2)
        try:
            order = FuturesOrder(
                contract=SYMBOL,
                size=f"-{str(short_qty)}",
                price="0",
                tif="ioc",
                reduce_only=False,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅GRID", f"short {short_qty}")
        except Exception as e:
            log("❌", f"short grid entry error: {e}")

        sync_position()
        log("✅ GRID", "Grid orders entry completed")

    except Exception as e:
        log("❌ GRID", f"Init error: {e}")
    finally:
        initialize_grid_lock.release()


# =============================================================================
# OBV MACD 계산
# =============================================================================
def calculate_obv_macd():
    """
    OBV-MACD 계산 (3분봉 기준)
    """
    global obv_macd_value
    
    try:
        if len(kline_history) < 60:
            return
        
        closes = [k['close'] for k in kline_history]
        volumes = [k['volume'] for k in kline_history]
        
        # OBV 계산
        obv = [0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i-1]:
                obv.append(obv[-1] - volumes[i])
            else:
                obv.append(obv[-1])
        
        # EMA 계산 함수
        def ema(data, period):
            ema_vals = []
            k = 2 / (period + 1)
            ema_vals.append(sum(data[:period]) / period)
            for price in data[period:]:
                ema_vals.append(price * k + ema_vals[-1] * (1 - k))
            return ema_vals
        
        # MACD 계산
        if len(obv) >= 60:
            ema_12 = ema(obv[-60:], 12)
            ema_26 = ema(obv[-60:], 26)
            
            if len(ema_12) > 0 and len(ema_26) > 0:
                macd_line = ema_12[-1] - ema_26[-1]
                
                # 정규화 (-0.01 ~ 0.01 범위)
                max_obv = max(abs(max(obv[-60:])), abs(min(obv[-60:])))
                if max_obv > 0:
                    normalized = macd_line / max_obv / 100
                    obv_macd_value = Decimal(str(normalized))
                    
                    # 로그 (100배 스케일로 표시)
                    display_value = float(obv_macd_value) * 100
                    if abs(display_value) > 0.1:
                        log("📊 OBV-MACD", f"{display_value:.2f}")
        
    except Exception as e:
        log("❌ OBV-MACD", f"Calculation error: {e}")


def calculate_dynamic_tp_gap():
    """
    OBV MACD 값 기반 동적 TP 계산
    OBV가 클수록 → TP를 높게 (더 오래 보유)
    """
    try:
        obv_display = float(obv_macd_value) * 100
        obv_abs = abs(obv_display)
        
        # OBV 값에 따라 TP 조정
        if obv_abs < 10:
            tp_ratio = Decimal("0.3")  # 30%
        elif obv_abs < 20:
            tp_ratio = Decimal("0.5")  # 50%
        elif obv_abs < 30:
            tp_ratio = Decimal("0.7")  # 70%
        elif obv_abs < 50:
            tp_ratio = Decimal("0.85")  # 85%
        else:
            tp_ratio = Decimal("1.0")  # 100%
        
        # TP 범위 내에서 조정
        tp_range = TPMAX - TPMIN
        dynamic_tp = TPMIN + (tp_range * tp_ratio)
        
        # LONG과 SHORT에 동일 적용
        return (dynamic_tp, dynamic_tp)
        
    except Exception as e:
        log("❌ TP GAP", f"Calculation error: {e}")
        return (TPMIN, TPMIN)


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
                candles = api.list_futures_candlesticks(
                    SETTLE,
                    contract=SYMBOL,
                    interval='3m',
                    limit=200
                )
               
                if candles and len(candles) > 0:
                    kline_history.clear()
                    for candle in candles:
                        kline_history.append({
                            'close': float(candle.c),
                            'high': float(candle.h),
                            'low': float(candle.l),
                            'volume': float(candle.v) if hasattr(candle, 'v') and candle.v else 0,
                        })
                   
                    calculate_obv_macd()
                   
                    if len(kline_history) >= 60 and obv_macd_value != Decimal("0"):
                        log("✅ OBV", "OBV MACD calculation started!")
                   
                    last_fetch = current_time
                   
            except GateApiException as e:
                if "400" not in str(e):
                    log("❌", f"Kline API error: {e}")
                time.sleep(10)
            except Exception as e:
                log("❌", f"Kline API error: {e}")
                time.sleep(10)
               
        except Exception as e:
            log("❌", f"Kline fetch error: {e}")
            time.sleep(10)

# =============================================================================
# WebSocket 포지션 모니터링
# =============================================================================
async def watch_positions():
    """WebSocket으로 가격 모니터링 (안정성 개선)"""
    global last_price
   
    max_reconnect_attempts = 5
    reconnect_delay = 5
    ping_count = 0
   
    while True:
        for attempt in range(max_reconnect_attempts):
            try:
                url = f"wss://fx-ws.gateio.ws/v4/ws/usdt"
               
                # ✅ 수정: ping_timeout 120으로 증가
                async with websockets.connect(
                    url,
                    ping_interval=60,
                    ping_timeout=120,  # 90 → 120
                    close_timeout=10
                ) as ws:
                    subscribe_msg = {
                        "time": int(time.time()),
                        "channel": "futures.tickers",
                        "event": "subscribe",
                        "payload": [SYMBOL]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    log("✅ WS", f"Connected to WebSocket (attempt {attempt + 1})")
                   
                    ping_count = 0
                   
                    while True:
                        try:
                            # ✅ 수정: timeout 150으로 증가
                            msg = await asyncio.wait_for(ws.recv(), timeout=150)  # 120 → 150
                            data = json.loads(msg)
                           
                            if data.get("event") == "update" and data.get("channel") == "futures.tickers":
                                result = data.get("result")
                                if result and isinstance(result, dict):
                                    price = float(result.get("last", 0))
                                    if price > 0:
                                        last_price = price
                                        ping_count = 0
                       
                        except asyncio.TimeoutError:
                            ping_count += 1
                            # ✅ 수정: 로그 빈도 감소 (20번마다 → 40번마다)
                            if ping_count % 40 == 1:
                                log("⚠️ WS", f"No price update for {ping_count * 150}s")
                            continue
                           
            except Exception as e:
                if attempt < max_reconnect_attempts - 1:
                    log("⚠️ WS", f"Reconnecting in {reconnect_delay}s (attempt {attempt + 1}/{max_reconnect_attempts})...")
                    await asyncio.sleep(reconnect_delay)
                else:
                    log("❌", f"WebSocket error after {max_reconnect_attempts} attempts: {e}")
                    await asyncio.sleep(30)
                    break

def start_websocket():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(watch_positions())

def position_monitor():
    prev_long_size = Decimal("-1")
    prev_short_size = Decimal("-1")
    api_error_count = 0
    last_error_log = 0
   
    while True:
        try:
            time.sleep(5)
           
            success = sync_position()
           
            if not success:
                api_error_count += 1
                if time.time() - last_error_log > 10:
                    log("⚠️", f"Position sync failed ({api_error_count} times) - Check API credentials")
                    last_error_log = time.time()
                continue
            else:
                if api_error_count > 0:
                    log("✅", f"Position sync recovered after {api_error_count} errors")
                    api_error_count = 0
           
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
                long_price = position_state[SYMBOL]["long"]["entry_price"]
                short_price = position_state[SYMBOL]["short"]["entry_price"]
           
            # ✅ 포지션 변경 로그만 (그리드 체결 처리 제거!)
            if long_size != prev_long_size or short_size != prev_short_size:
                if prev_long_size != Decimal("-1"):
                    log("🔄 CHANGE", f"Long {prev_long_size}→{long_size} | Short {prev_short_size}→{short_size}")
               
                prev_long_size = long_size
                prev_short_size = short_size
           
            with balance_lock:
                balance = account_balance
           
            max_value = balance * MAXPOSITIONRATIO
            long_value = long_price * long_size
            short_value = short_price * short_size
           
            # 최대 보유 한도 체크
            if long_value >= max_value and not max_position_locked["long"]:
                log_event_header("MAX POSITION LIMIT")
                log("⚠️ LIMIT", f"LONG ${long_value:.2f} >= ${max_value:.2f}")
                max_position_locked["long"] = True
                cancel_all_orders()  # ✅ 수정
           
            if short_value >= max_value and not max_position_locked["short"]:
                log_event_header("MAX POSITION LIMIT")
                log("⚠️ LIMIT", f"SHORT ${short_value:.2f} >= ${max_value:.2f}")
                max_position_locked["short"] = True
                cancel_all_orders()  # ✅ 수정
           
            # 한도 잠금 해제
            if long_value < max_value and max_position_locked["long"]:
                log("✅ UNLOCK", f"LONG ${long_value:.2f} < ${max_value:.2f}")
                max_position_locked["long"] = False
                full_refresh("Max_Unlock_Long")
                continue
           
            if short_value < max_value and max_position_locked["short"]:
                log("✅ UNLOCK", f"SHORT ${short_value:.2f} < ${max_value:.2f}")
                max_position_locked["short"] = False
                full_refresh("Max_Unlock_Short")
                continue
       
        except Exception as e:
            log("❌", f"Position monitor error: {e}")
            time.sleep(5)

def start_grid_monitor():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(grid_fill_monitor())

def tp_monitor():
    """TP 체결 모니터링 (개별 TP + 평단 TP)"""
    while True:
        try:
            time.sleep(3)
                       
            # ===== 평단 TP 체결 확인 =====
            for side in ["long", "short"]:
                tp_id = average_tp_orders[SYMBOL].get(side)
                if not tp_id:
                    continue
               
                try:
                    order = api.get_futures_order(SETTLE, str(tp_id))
                    if not order:
                        continue
                   
                    if hasattr(order, 'status') and order.status in ["finished", "closed"]:
                        log_event_header("AVERAGE TP HIT")
                        log("🎯 TP", f"{side.upper()} average position closed")
                        average_tp_orders[SYMBOL][side] = None
                       
                        time.sleep(0.5)
                        sync_position()  # 포지션 동기화
                       
                        # ★ skip_grid=False: TP 체결 후 양방향 재진입을 위해 그리드도 다시 생성!
                        full_refresh("Average_TP", skip_grid=False)

                        update_event_time()  # 이벤트 시간 갱신
                       
                        break
                except:
                    pass
       
        except Exception as e:
            log("❌", f"TP monitor error: {e}")
            time.sleep(1)


def check_idle_and_enter():
    """
    무포지션 아이들 상태 체크 및 진입
    - 최근 이벤트 없고, 포지션 없으면 시장가 진입
    """
    global idle_entry_in_progress, last_idle_entry_time, idle_entry_count
    
    try:
        # 이미 진행 중이면 스킵
        with idle_entry_progress_lock:
            if idle_entry_in_progress:
                return
        
        current_time = time.time()
        
        # 쿨다운 체크 (최근 진입 후 10초 이내면 스킵)
        if current_time - last_idle_entry_time < IDLE_ENTRY_COOLDOWN:
            return
        
        # 최근 이벤트 시간 체크
        elapsed = current_time - last_event_time
        
        if elapsed < IDLE_TIME_SECONDS:
            return
        
        # 포지션 확인
        sync_position()
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        # 포지션이 있으면 리턴
        if long_size > 0 or short_size > 0:
            return
        
        # 아이들 진입 시작
        with idle_entry_progress_lock:
            idle_entry_in_progress = True
        
        try:
            idle_entry_count += 1
            log_event_header(f"IDLE ENTRY #{idle_entry_count}")
            log("⏰ IDLE", f"No activity for {elapsed/60:.1f} min → Market entry")
            
            # 시장가 양방향 진입
            current_price = get_current_price()
            if current_price > 0:
                initialize_grid(current_price)
                last_idle_entry_time = current_time
                update_event_time()
                
        finally:
            with idle_entry_progress_lock:
                idle_entry_in_progress = False
        
    except Exception as e:
        log("❌ IDLE", f"Error: {e}")
        with idle_entry_progress_lock:
            idle_entry_in_progress = False


def idle_monitor():
    global last_idle_check  # ← 전역 선언
   
    while True:
        try:
            time.sleep(60)
            current_time = time.time()
            if current_time - last_idle_check < 120:
                continue
           
            last_idle_check = current_time  # ← 이제 안전
            check_idle_and_enter()
           
        except Exception as e:
            log("❌", f"Idle monitor error: {e}")
            time.sleep(10)

def get_tp_orders_hash(tp_orders):
    """TP 주문 리스트의 해시값 계산"""
    try:
        if not tp_orders:
            return ""
        
        # 주문 정보를 문자열로 변환
        order_strings = []
        for o in tp_orders:
            order_str = f"{o.size}_{o.price}_{o.reduce_only}"
            order_strings.append(order_str)
        
        # 정렬 후 해시
        order_strings.sort()
        combined = "_".join(order_strings)
        
        return hashlib.md5(combined.encode()).hexdigest()
        
    except Exception as e:
        log("❌ HASH", f"Error: {e}")
        return ""


def periodic_health_check():
    """2분마다 실행되는 헬스 체크"""
    global last_idle_check, obv_macd_value, tp_gap_min, tp_gap_max, last_adjusted_obv
    global tp_order_hash, account_balance, initial_capital, tp_gap_long, tp_gap_short
   
    while True:
        try:
            time.sleep(120)
            log("💊 HEALTH", "Starting health check...")
           
            # ★ 계좌 잔고 조회
            try:
                futures_account = api.list_futures_accounts(SETTLE)
               
                if futures_account:
                    available_str = getattr(futures_account, 'available', None)
                   
                    if available_str:
                        current_available = Decimal(str(available_str))
                       
                        if current_available > 0:
                            # 포지션 동기화
                            sync_position()
                           
                            with position_lock:
                                long_size = position_state[SYMBOL]["long"]["size"]
                                short_size = position_state[SYMBOL]["short"]["size"]
                           
                            # ★ 포지션 없으면 → Initial Capital 갱신
                            if long_size == 0 and short_size == 0:
                                with balance_lock:
                                    old_initial = initial_capital
                                    initial_capital = current_available
                                    account_balance = initial_capital
                                   
                                # ★ 파일에 저장
                                save_initial_capital()
                               
                                if old_initial != initial_capital and old_initial > 0:
                                    profit = initial_capital - old_initial
                                    profit_rate = (profit / old_initial) * 100
                                    log("💰 BALANCE", f"{account_balance:.2f} USDT (이전: {old_initial:.2f}, 수익: {profit:+.2f}, {profit_rate:+.2f}%)")
                                else:
                                    log("💰 BALANCE", f"{account_balance:.2f} USDT (초기 자본금 설정)")
                            else:
                                # 포지션 있으면 → 저장된 초기 자본금 사용
                                with balance_lock:
                                    account_balance = initial_capital
                               
                                log("📊 CURRENT AVAILABLE", f"{current_available:.2f} USDT")
                           
                            # MAX POSITION 계산
                            max_position = account_balance * MAXPOSITIONRATIO
                            log("📊 MAX POSITION", f"{max_position:.2f} USDT")
           
            except Exception as e:
                log("❌ ERROR", f"Balance check: {e}")
               
            # 2️⃣ 포지션 동기화
            sync_position()
           
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
           
            if long_size == 0 and short_size == 0:
                log("💊 HEALTH", "No position")
                continue
           
            # 3️⃣ 주문 상태 확인
            try:
                orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
                grid_count = sum(1 for o in orders if not o.reduce_only)
                tp_count = sum(1 for o in orders if o.reduce_only)
                log("📊 ORDERS", f"Grid: {grid_count}, TP: {tp_count}")
            except Exception as e:
                log("❌ HEALTH", f"List orders error: {e}")
                continue
           
            # 4️⃣ TP 해시값 검증
            if long_size > 0 or short_size > 0:
                tp_orders_list = [o for o in orders if o.reduce_only]
                current_hash = get_tp_orders_hash(tp_orders_list)
                previous_hash = tp_order_hash.get(SYMBOL)
               
                tp_long_qty = sum(abs(o.size) for o in tp_orders_list if o.size > 0)
                tp_short_qty = sum(abs(o.size) for o in tp_orders_list if o.size < 0)
               
                tp_mismatch = False
               
                if tp_count == 0 and (long_size > 0 or short_size > 0):
                    log("🔧 HEALTH", "❌ TP CRITICAL: No TP at all!")
                    tp_mismatch = True
                elif long_size > 0 and tp_long_qty < long_size * 0.3:
                    tp_mismatch = True
                elif short_size > 0 and tp_short_qty < short_size * 0.3:
                    tp_mismatch = True
               
                if tp_mismatch and current_hash != previous_hash:
                    log("🔧 HEALTH", "⚠️ TP changed + problem detected → Refreshing!")
                    time.sleep(0.5)
                    try:
                        refresh_all_tp_orders()
                        tp_order_hash[SYMBOL] = current_hash
                        log("✅ HEALTH", "TP refreshed and hash updated")
                    except Exception as e:
                        log("❌ HEALTH", f"TP refresh error: {e}")
                else:
                    log("✅ HEALTH", "TP orders stable")
                    tp_order_hash[SYMBOL] = current_hash
           
            # ★ 5️⃣ OBV MACD 체크 후 TP % 변동시 갱신! (핵심!)
            try:
                calculate_obv_macd()
                current_obv = float(obv_macd_value) * 100
               
                if last_adjusted_obv == 0:
                    last_adjusted_obv = current_obv
                    log("💊 HEALTH", f"OBV initialized: {current_obv:.6f}")
                else:
                    obv_change = abs(current_obv - last_adjusted_obv)
                   
                    if obv_change >= 10:  # OBV 변화 감지!
                        log("🔔 HEALTH", f"OBV changed: {obv_change:.6f} → Recalculating TP...")
                       
                        tp_result = calculate_dynamic_tp_gap()
                       
                        try:
                            if isinstance(tp_result, (tuple, list)) and len(tp_result) == 3:
                                new_tp_long, new_tp_short = tp_result[0], tp_result[2]
                            elif isinstance(tp_result, (tuple, list)) and len(tp_result) >= 2:
                                new_tp_long, new_tp_short = tp_result[0], tp_result[1]
                            else:
                                new_tp_long = Decimal(str(tp_result))
                                new_tp_short = new_tp_long
                           
                            current_tp_min = float(tp_gap_min)
                            new_tp_min = float(new_tp_long)
                            tp_min_change = abs(new_tp_min - current_tp_min)
                           
                            if tp_min_change >= 0.0001:  # 0.01% 이상 변화
                                log("🔄 TP ADJUST", f"OBV: {current_obv:.6f}, New TP: {new_tp_min*100:.2f}%")
                               
                                try:
                                    cancel_tp_only()
                                    time.sleep(0.5)
                                   
                                    # ✅ 핵심: position_lock 없음!
                                    tp_gap_min = new_tp_long
                                    tp_gap_max = new_tp_short
                                   
                                    refresh_all_tp_orders()
                                    last_adjusted_obv = current_obv
                                   
                                    log("✅ TP ADJUST", "Success! New TP applied!")
                                except Exception as e:
                                    log("❌ TP ADJUST", f"Failed: {e}")
                       
                        except Exception as e:
                            log("❌ HEALTH", f"TP calculation error: {e}")
           
            except Exception as e:
                log("❌ HEALTH", f"OBV MACD check error: {e}")
           
            # ★ 6️⃣ 불균형 포지션 자동 진입 (SHORT 익절 → LONG 헤징)
            try:
                market_entry_when_imbalanced()
            except Exception as e:
                log("❌ HEALTH", f"Market entry error: {e}")
           
            # 7️⃣ 단일 포지션 그리드 체크
            try:
                single_position = (long_size > 0 or short_size > 0) and not (long_size > 0 and short_size > 0)
                if single_position and grid_count == 0:
                    current_price = get_current_price()
                    if current_price > 0:
                        log("⚠️ SINGLE", "Creating grid from single position...")
                        initialize_grid(current_price)
            except Exception as e:
                log("❌ HEALTH", f"Grid error: {e}")
           
            # 8️⃣ 전략 일관성 검증
            try:
                validate_strategy_consistency()
            except Exception as e:
                log("❌ HEALTH", f"Consistency error: {e}")
           
            # 9️⃣ 중복/오래된 주문 정리
            try:
                remove_duplicate_orders()
                cancel_stale_orders()
            except Exception as e:
                log("❌ HEALTH", f"Order cleanup error: {e}")
           
            log("✅ HEALTH", "Health check complete")
       
        except Exception as e:
            log("❌ HEALTH", f"Health check error: {e}")
            time.sleep(5)

def full_refresh(event_type, skip_grid=False):
    """
    시스템 새로고침 + 리밸런싱 체크
    """
    log_event_header(f"FULL REFRESH: {event_type}")
   
    log("🔄 SYNC", "Syncing position...")
    sync_position()
    
    # ★ 무포 시점 기록
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


async def grid_fill_monitor():
    """
    WebSocket으로 TP 체결 모니터링
    + 리밸런싱 조건 체크 추가!
    """
    global last_grid_time, idle_entry_count
   
    uri = f"wss://fx-ws.gateio.ws/v4/ws/{SETTLE}"
    ping_count = 0
    reconnect_attempt = 0
    max_reconnect = 5
   
    while True:
        try:
            async with websockets.connect(
                uri,
                ping_interval=60,
                ping_timeout=120,
                close_timeout=10
            ) as ws:
                auth_msg = {
                    "time": int(time.time()),
                    "channel": "futures.orders",
                    "event": "subscribe",
                    "payload": [API_KEY, API_SECRET, SYMBOL]
                }
                await ws.send(json.dumps(auth_msg))
                log("✅ WS", f"Connected to WebSocket (attempt {reconnect_attempt + 1})")
                reconnect_attempt = 0
                ping_count = 0
               
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=150)
                        data = json.loads(msg)
                       
                        if data.get("event") == "update" and data.get("channel") == "futures.orders":
                            ping_count = 0
                           
                            for order_data in data.get("result", []):
                                contract = order_data.get("contract")
                                if contract != SYMBOL:
                                    continue
                               
                                finish_as = order_data.get("finish_as", "")
                                status = order_data.get("status", "")
                               
                                is_filled = (
                                    finish_as in ["filled", "ioc"] or
                                    status in ["finished", "closed"]
                                )
                               
                                if not is_filled:
                                    continue
                               
                                is_reduce_only = order_data.get("is_reduce_only", False)
                                size = order_data.get("size", 0)
                                price = float(order_data.get("price", 0))
                               
                                # TP 체결만 처리
                                if is_reduce_only:
                                    side = "long" if size > 0 else "short"
                                    tp_qty = abs(int(size))
                                    tp_profit = Decimal(str(tp_qty)) * Decimal(str(price))
                                   
                                    log("✅ TP FILLED", f"{side.upper()} {tp_qty} @ {price:.4f}")
                                   
                                    time.sleep(0.5)
                                    sync_position()
                                   
                                    # ★ 리밸런싱 조건 체크!
                                    with position_lock:
                                        if side == "long":
                                            remaining_loss = position_state[SYMBOL]["short"]["size"] * get_current_price()
                                        else:
                                            remaining_loss = position_state[SYMBOL]["long"]["size"] * get_current_price()
                                    
                                    if check_rebalancing_condition(tp_profit, remaining_loss):
                                        execute_rebalancing_sl()
                                   
                                    # 물량 누적 방지
                                    try:
                                        handle_non_main_position_tp(tp_qty)
                                        log("💊 TP HANDLER", "Tier check completed")
                                    except Exception as e:
                                        log("❌ TP HANDLER", f"Failed: {e}")
                                       
                                    time.sleep(0.5)
                                   
                                    with position_lock:
                                        long_size = position_state[SYMBOL]["long"]["size"]
                                        short_size = position_state[SYMBOL]["short"]["size"]
                                   
                                    # 양방향 TP 체결
                                    if long_size == 0 and short_size == 0:
                                        log("🎯 BOTH CLOSED", "Both sides closed → Full refresh")
                                        update_event_time()
                                        update_no_position_time()  # ★ 무포 시점 기록
                                       
                                        threading.Thread(
                                            target=full_refresh,
                                            args=("Average_TP",),
                                            daemon=True
                                        ).start()
                   
                    except asyncio.TimeoutError:
                        ping_count += 1
                        if ping_count % 40 == 1:
                            log("⚠️ WS", f"No order update for {ping_count * 150}s")
                        continue
       
        except Exception as e:
            reconnect_attempt += 1
            if reconnect_attempt <= max_reconnect:
                log("❌ WS", f"Error: {e}")
                log("⚠️ WS", f"Reconnecting in 5s (attempt {reconnect_attempt}/{max_reconnect})...")
                await asyncio.sleep(5)
            else:
                log("❌ WS", f"Max reconnect attempts reached. Waiting 30s...")
                await asyncio.sleep(30)
                reconnect_attempt = 0

def market_entry_when_imbalanced():
    """
    포지션 불균형 시 OBV 가중치로 시장가 진입 (0.001 단위 안정화)
    """
    try:
        sync_position()

        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]

        current_price = get_current_price()
        if current_price <= 0:
            log("❌ ENTRY", "Price fetch failed")
            return

        base_qty = float(account_balance * BASERATIO / current_price)
        base_qty = safe_order_qty(base_qty)

        obv_display = float(obv_macd_value) * 100
        obv_multiplier = float(calculate_obv_macd_weight(obv_display))

        if obv_display > 0:
            short_qty = safe_order_qty(base_qty * (1 + obv_multiplier))
            long_qty = safe_order_qty(base_qty)
        elif obv_display < 0:
            long_qty = safe_order_qty(base_qty * (1 + obv_multiplier))
            short_qty = safe_order_qty(base_qty)
        else:
            long_qty = safe_order_qty(base_qty)
            short_qty = safe_order_qty(base_qty)

        # **여기 추가: 0.001 단위로 버림 처리**
        long_qty = adjust_quantity_step(long_qty)
        short_qty = adjust_quantity_step(short_qty)

        log("INFO", f"[IMBALANCED ENTRY] LONG={long_qty}, SHORT={short_qty}")

        try:
            order = FuturesOrder(
                contract=SYMBOL,
                size=str(long_qty),
                price="0",
                tif="ioc",
                reduce_only=False,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ENTRY", f"long {long_qty}")
        except Exception as e:
            log("❌", f"long entry error: {e}")

        time.sleep(0.2)
        try:
            order = FuturesOrder(
                contract=SYMBOL,
                size=f"-{str(short_qty)}",
                price="0",
                tif="ioc",
                reduce_only=False,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ENTRY", f"short {short_qty}")
        except Exception as e:
            log("❌", f"short entry error: {e}")

        log("✅ ENTRY", "Market entry completed")

    except Exception as e:
        log("❌ ENTRY", f"Imbalanced entry error: {e}")


# =============================================================================
# Flask 엔드포인트
# =============================================================================
@app.route('/webhook', methods=['POST'])
def webhook():
    """TradingView webhook (선택사항 - 자체 계산도 가능)"""
    global obv_macd_value
    try:
        data = request.get_json()
        tt1 = data.get('tt1', 0)
        # TradingView에서 온 값은 이미 -10 ~ 10 범위라고 가정
        # 내부적으로 /1000 저장 (-0.01 ~ 0.01)
        obv_macd_value = Decimal(str(tt1 / 1000.0))
        log("📨 WEBHOOK", f"OBV MACD updated from TradingView: {tt1:.2f} (stored as {float(obv_macd_value):.6f})")
        return jsonify({"status": "success", "tt1": float(tt1), "stored": float(obv_macd_value)}), 200
    except Exception as e:
        log("❌", f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """헬스 체크"""
    obv_display = float(obv_macd_value) * 100
    return jsonify({
        "status": "running",
        "obv_macd_display": obv_display,
        "obv_macd_internal": float(obv_macd_value) * 100,
        "api_configured": bool(API_KEY and API_SECRET)
    }), 200

@app.route('/status', methods=['GET'])
def status():
    """상세 상태 조회"""
    with position_lock:
        pos = position_state[SYMBOL]
    with balance_lock:
        bal = float(account_balance)
   
    obv_display = float(obv_macd_value) * 100
   
    return jsonify({
        "balance": bal,
        "obv_macd_display": obv_display,
        "obv_macd_internal": float(obv_macd_value) * 100,
        "position": {
            "long": {"size": float(pos["long"]["size"]), "entry_price": float(pos["long"]["entry_price"])},
            "short": {"size": float(pos["short"]["size"]), "entry_price": float(pos["short"]["entry_price"])}
        }
    }), 200
   
@app.route('/refresh', methods=['POST'])
def manual_refresh():
    """수동 새로고침"""
    try:
        full_refresh("Manual")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/reset', methods=['POST'])
def reset_tracking():
    """아이들 카운트 리셋"""
    global idle_entry_count
    try:
        idle_entry_count = 0
        log("🔄 RESET", "Idle entry count reset to 0")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# =============================================================================
# 메인 실행
# =============================================================================
def print_startup_summary():
    """
    서버 시작 시 요약 정보 출력 + 계좌 잔고 조회
    """
    global account_balance, initial_capital
   
    # 스타트업 로그
    log("divider", "=" * 80)
    log("🚀 START", "GATE Trading Bot v26.0")
    log("divider", "=" * 80)
    log("📡 API", f"Key: {API_KEY[:8]}...")
    log("📡 API", f"Secret: {len(API_SECRET)} characters")
    log("✅ API", "Connection test successful")
    log("divider", "-" * 80)
    log("⚙️ CONFIG", "Settings:")
    log("", f"  📊 Symbol: {SYMBOL}")
    log("", f"  🎯 TP Gap: {float(TPMIN)*100:.2f}%-{float(TPMAX)*100:.2f}% (동적)")
    log("", f"  💰 Base Ratio: {float(BASERATIO)*100:.2f}%")
    log("", f"  📈 Max Position: {float(MAXPOSITIONRATIO)*100:.1f}%")
    log("divider", "-" * 80)
   
    # ★ 저장된 Initial Capital 로드
    capital_loaded = load_initial_capital()
   
    # 계좌 잔고 조회
    try:
        log("💰 BALANCE", "Fetching account balance...")
       
        futures_account = api.list_futures_accounts(SETTLE)
       
        if futures_account:
            available_str = getattr(futures_account, 'available', None)
            unrealised_pnl_str = getattr(futures_account, 'unrealised_pnl', None)
           
            if available_str:
                current_available = Decimal(str(available_str))
               
                if current_available > 0:
                    # 포지션 동기화
                    sync_position()
                   
                    with position_lock:
                        long_size = position_state[SYMBOL]["long"]["size"]
                        short_size = position_state[SYMBOL]["short"]["size"]
                   
                    # ★ 포지션 없으면 → Initial Capital 갱신
                    if long_size == 0 and short_size == 0:
                        with balance_lock:
                            old_capital = initial_capital
                            initial_capital = current_available
                            account_balance = initial_capital
                       
                        # 파일에 저장
                        save_initial_capital()
                       
                        if old_capital > 0 and old_capital != initial_capital:
                            profit = initial_capital - old_capital
                            profit_rate = (profit / old_capital) * 100
                            log("🔄 INIT CAPITAL", f"{initial_capital:.2f} USDT (이전: {old_capital:.2f}, 수익: {profit:+.2f}, {profit_rate:+.2f}%)")
                        else:
                            log("🔄 INIT CAPITAL", f"{initial_capital:.2f} USDT (포지션 없음 → 갱신)")
                    else:
                        # 포지션 있으면 → 로드된 Initial Capital 사용
                        if capital_loaded and initial_capital > 0:
                            with balance_lock:
                                account_balance = initial_capital
                            log("💰 BALANCE", f"{account_balance:.2f} USDT (저장된 초기 자본금)")
                            log("📊 CURRENT AVAILABLE", f"{current_available:.2f} USDT")
                        else:
                            # 저장된 자본금이 없으면 → 현재 Available 사용 (최초 시작)
                            with balance_lock:
                                initial_capital = current_available
                                account_balance = initial_capital
                           
                            # 파일에 저장
                            save_initial_capital()
                            log("🔄 INIT CAPITAL", f"{initial_capital:.2f} USDT (첫 설정)")
                   
                    # Unrealized PNL 표시
                    if unrealised_pnl_str:
                        pnl_dec = Decimal(str(unrealised_pnl_str))
                        log("📊 UNREALIZED PNL", f"{pnl_dec:+.2f} USDT")
                   
                    # MAX POSITION 계산
                    max_position = account_balance * MAXPOSITIONRATIO
                    log("📊 MAX POSITION", f"{max_position:.2f} USDT")
                else:
                    log("⚠️ WARNING", f"Balance is 0. Using default {INITIALBALANCE} USDT")
                    with balance_lock:
                        account_balance = INITIALBALANCE
                        initial_capital = INITIALBALANCE
            else:
                log("❌ ERROR", "Available field not found")
                with balance_lock:
                    account_balance = INITIALBALANCE
                    initial_capital = INITIALBALANCE
        else:
            log("❌ ERROR", "Could not fetch Futures Account")
            with balance_lock:
                account_balance = INITIALBALANCE
                initial_capital = INITIALBALANCE
   
    except Exception as e:
        log("❌ ERROR", f"Balance check failed: {e}")
        with balance_lock:
            account_balance = INITIALBALANCE
            initial_capital = INITIALBALANCE
   
    log("divider", "-" * 80)

# 현재 가격 조회 및 초기 그리드 생성
    try:
        current_price = get_current_price()
        if current_price > 0:
            log("💵 PRICE", f"{current_price:.4f}")
           
            # 기존 주문 취소
            cancel_all_orders()
            time.sleep(0.5)
           
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
           
            # 포지션이 있으면 그리드 생성
            if long_size > 0 and short_size > 0:
                log("🔄 INIT", "Both sides exist → TP only (No new entry)")
                time.sleep(0.5)
                refresh_all_tp_orders()
            elif long_size > 0 or short_size > 0:
                log("🔄 INIT", "Single position → Creating grids for hedging")
                initialize_grid(current_price)
                time.sleep(0.5)
                refresh_all_tp_orders()
            else:
                log("ℹ️ INIT", "No position → Creating initial grids")
                initialize_grid(current_price)
        else:
            log("❌ ERROR", "Could not fetch current price")
    except Exception as e:
        log("❌ ERROR", f"Initialization error: {e}")
   
    log("divider", "-" * 80)
    log("✅ INIT", "Complete. Starting threads...")
    log("divider", "-" * 80)


if __name__ == '__main__':
    if not API_KEY or not API_SECRET:
        log("❌ FATAL", "Cannot start without API credentials!")
        exit(1)
   
    update_event_time()
   
    try:
        test_ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if test_ticker:
            log("✅ API", "Connection test successful")
    except Exception as e:
        log("❌ API", f"Connection test error: {e}")
   
    print_startup_summary()
   
    log("🧵 THREADS", "Starting monitoring threads...")
    threading.Thread(target=fetch_kline_thread, daemon=True).start()
    threading.Thread(target=start_websocket, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=start_grid_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    threading.Thread(target=idle_monitor, daemon=True).start()
    threading.Thread(target=periodic_health_check, daemon=True).start()
   
    log("✅ THREADS", "All monitoring threads started")
    log("🌐 FLASK", "Starting server on port 8080...")
   
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
