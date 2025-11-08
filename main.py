import os
import time
import asyncio
import threading
import logging
import json
import math
from decimal import Decimal, ROUND_DOWN
from collections import deque
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi
import hashlib  # ← 수정: json 다음에 hashlib (순서 변경 OK)

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
SYMBOL = os.environ.get("SYMBOL", "ARB_USDT")
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
BASERATIO = Decimal("0.02")                 # 기본 수량 비율 (2%)
MAXPOSITIONRATIO = Decimal("3.0")          # 최대 포지션 비율 (3배)
HEDGE_RATIO_MAIN = Decimal("0.10")           # 주력 헤지 비율 (10%)

# TP 설정 (동적 TP) ← 이 부분 추가
TPMIN = Decimal("0.0019")                   # 최소 TP (0.19%)
TPMAX = Decimal("0.004")                    # 최대 TP (0.4%)

# 시간 설정
IDLE_TIME_SECONDS = 600                      # 아이들 감지 시간 (10분)
IDLE_TIMEOUT = 600                           # 아이들 타임아웃 (10분)
IDLE_ENTRY_COOLDOWN = 10                     # 아이들 진입 쿨다운 (10초)

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
    logger.info(f"[{tag}] {msg}")

def log_divider(char="=", length=80):
    logger.info(char * length)

def log_event_header(event_name):
    log_divider("-")
    log("🔔 EVENT", event_name)
    log_divider("-")

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
# 신규 함수 2: handle_non_main_position_tp()  ← ✅ 새로 추가!
# =============================================================================

def handle_non_main_position_tp(non_main_size_at_tp):
    """
    비주력 TP 체결 시 주력 포지션 SL (1배/2배 2단계 전략)
    
    1배 <= 주력 < 2배: 비주력 × 0.8배 SL (새로운 전략!)
    2배 이상: 비주력 × 1.5배 SL (기존 전략)
    """
    try:
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        with balance_lock:
            balance = account_balance
        
        # 주력 포지션 판단
        if long_size >= short_size:
            main_size = long_size
            main_side = "long"
        else:
            main_size = short_size
            main_side = "short"
        
        # ✅ 조건 확인: 주력이 1배 미만이면 작동 안 함
        if main_size < balance * 1:
            log("ℹ️ TP HANDLER", f"Main {main_size} < {balance * 1} (1배) - 스킵")
            return
        
        # ✅ Tier 판단
        if balance * 1 <= main_size < balance * 2:
            # 1배 ~ 2배 미만: 당신의 제안 (0.8배)
            sl_qty = int(non_main_size_at_tp * Decimal("0.8"))
            tier = "Tier-1 (0.8배)"
        else:  # main_size >= balance * 2
            # 2배 이상: 기존 (1.5배)
            sl_qty = int(non_main_size_at_tp * Decimal("1.5"))
            tier = "Tier-2 (1.5배)"
        
        if sl_qty < 1:
            sl_qty = 1
        if sl_qty > main_size:
            sl_qty = int(main_size)
        
        log("💥 TP HANDLER", f"{tier}: 비주력 {non_main_size_at_tp}개 TP → 주력 {main_side.upper()} {sl_qty}개 SL")
        
        # 주력 SL 실행
        if main_side == "long":
            order_size = -sl_qty
        else:
            order_size = sl_qty
        
        order = FuturesOrder(
            contract=SYMBOL,
            size=order_size,
            price="0",
            tif="ioc",
            reduce_only=True,
            text=generate_order_id()
        )
        api.create_futures_order(SETTLE, order)
        log("✅ TP HANDLER", f"{main_side.upper()} {sl_qty}개 SL 처리됨!")
        
        time.sleep(0.5)
        sync_position()
        
    except Exception as e:
        log("❌ TP HANDLER", f"Error: {e}")


# =============================================================================
# OBV MACD 계산 (Pine Script 정확한 변환)
# =============================================================================
def ema(data, period):
    """EMA 계산"""
    if len(data) < period:
        return data[-1] if data else 0
    
    multiplier = 2.0 / (period + 1)
    ema_val = sum(data[:period]) / period
    
    for price in data[period:]:
        ema_val = (price - ema_val) * multiplier + ema_val
    
    return ema_val

def sma(data, period):
    """SMA 계산"""
    if len(data) < period:
        return sum(data) / len(data) if data else 0
    return sum(data[-period:]) / period

def stdev(data, period):
    """표준편차 계산"""
    if len(data) < period:
        period = len(data)
    if period == 0:
        return 0
    
    data_slice = data[-period:]
    mean = sum(data_slice) / period
    variance = sum((x - mean) ** 2 for x in data_slice) / period
    return math.sqrt(variance)

def wma(data, period):
    """WMA (Weighted Moving Average) 계산"""
    if len(data) < period:
        period = len(data)
    if period == 0:
        return 0
    
    weights = list(range(1, period + 1))
    weighted_sum = sum(data[-period:][i] * weights[i] for i in range(period))
    return weighted_sum / sum(weights)

def dema(data, period):
    """DEMA 계산"""
    if len(data) < period * 2:
        return data[-1] if data else 0
    
    ema1 = ema(data, period)
    
    # EMA of EMA 계산을 위해 EMA 시계열 생성
    ema_series = []
    for i in range(period, len(data) + 1):
        ema_series.append(ema(data[:i], period))
    
    if len(ema_series) < period:
        ema2 = ema1
    else:
        ema2 = ema(ema_series, period)
    
    return 2 * ema1 - ema2

# ============================================================================
# 3️⃣ calculate_obv_macd() - 완전 코드 (한 줄도 생략 없음!)
# ============================================================================

def calculate_obv_macd():
    global obv_macd_value
    
    try:
        if len(kline_history) < 60:
            if len(kline_history) > 0:
                log("⚠️ OBV", f"Not enough kline data: {len(kline_history)}/60")
            return
        
        closes = [k['close'] for k in kline_history]
        highs = [k['high'] for k in kline_history]
        lows = [k['low'] for k in kline_history]
        volumes = [k['volume'] for k in kline_history]
        
        window_len = 28
        v_len = 14
        
        hl_diff = [highs[i] - lows[i] for i in range(len(highs))]
        price_spread = stdev(hl_diff, window_len)
        
        if price_spread == 0:
            return
        
        obv_values = [0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv_values.append(obv_values[-1] + volumes[i])
            elif closes[i] < closes[i-1]:
                obv_values.append(obv_values[-1] - volumes[i])
            else:
                obv_values.append(obv_values[-1])
        
        if len(obv_values) < v_len + window_len:
            return
        
        smooth = sma(obv_values, v_len)
        
        v_diff = [obv_values[i] - smooth for i in range(len(obv_values))]
        v_spread = stdev(v_diff, window_len)
        
        if v_spread == 0:
            return
        
        shadow = (obv_values[-1] - smooth) / v_spread * price_spread
        
        out = highs[-1] + shadow if shadow > 0 else lows[-1] + shadow
        
        obvema = out
        
        ma = obvema
        slow_ma = ema(closes, 26)
        macd = ma - slow_ma
        
        if len(kline_history) >= 2:
            macd_prev = ma - ema(closes[:-1], 26) if len(closes) > 26 else 0
            macd_history = [macd_prev, macd]
            
            len5 = 2
            sumX = 3.0
            sumY = sum(macd_history)
            sumXSqr = 5.0
            sumXY = macd_history[0] * 1 + macd_history[1] * 2
            
            try:
                slope = (len5 * sumXY - sumX * sumY) / (len5 * sumXSqr - sumX * sumX)
            except ZeroDivisionError:
                slope = 0
            
            average = sumY / len5
            intercept = average - slope * sumX / len5 + slope
            
            tt1 = intercept + slope * len5
        else:
            tt1 = macd
        
        current_price = closes[-1]
        if current_price <= 0:
            return
        
        normalized = (tt1 / current_price) / 100.0
        
        avg_volume = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else 1
        if avg_volume > 0:
            normalized = normalized / (avg_volume / 1000000.0)
        
        obv_macd_value = Decimal(str(normalized * 100))
        
        obv_raw = float(obv_macd_value) * 100
        log("✅ OBV CALC", f"Value: {obv_raw:.8f} | Multiplier check")
        
    except Exception as e:
        log("❌ OBV", f"Calculation error: {e}")
        obv_macd_value = Decimal("0")

def get_obv_macd_value():
    """항상 Decimal 반환!"""
    global obv_macd_value
    
    if obv_macd_value is None or obv_macd_value == 0:
        return Decimal("0")
    
    # ✅ 타입 검증
    if not isinstance(obv_macd_value, Decimal):
        return Decimal(str(obv_macd_value))
    
    return obv_macd_value
          

# =============================================================================
# 캔들 데이터 수집
# =============================================================================
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

# =============================================================================
# 포지션 동기화 - 에러 시 재시도 간격 증가
# =============================================================================
def sync_position(max_retries=3, retry_delay=2):
    """포지션 동기화 (재시도 로직 포함) - WebSocket 독립적"""
    for attempt in range(max_retries):
        try:
            # ✅ REST API는 WebSocket과 독립적으로 작동!
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
                        entry_price = abs(Decimal(str(p.entry_price))) if p.entry_price else Decimal("0")
                        
                        if size_dec > 0:
                            with position_lock:
                                position_state[SYMBOL]["long"]["size"] = size_dec
                                position_state[SYMBOL]["long"]["entry_price"] = entry_price  # ← 변경!
                        elif size_dec < 0:
                            with position_lock:
                                position_state[SYMBOL]["short"]["size"] = abs(size_dec)
                                position_state[SYMBOL]["short"]["entry_price"] = entry_price
            
            return True  # ✅ 성공
            
        except GateApiException as e:
            if attempt < max_retries - 1:
                log("⚠️ RETRY", f"Position sync attempt {attempt + 1}/{max_retries} failed, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                log("❌ SYNC", f"Position sync error after {max_retries} attempts: {e}")
                return False  # ✅ 실패
        except Exception as e:
            if attempt < max_retries - 1:
                log("⚠️ RETRY", f"Position sync attempt {attempt + 1}/{max_retries} failed, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                log("❌ SYNC", f"Position sync error after {max_retries} attempts: {e}")
                return False  # ✅ 실패
    
    return False  # ✅ 기본 실패

def get_tp_orders_hash(tp_orders_list):
    '''TP 주문들의 해시값 계산 (변화 감지용)'''
    if not tp_orders_list:
        return None
    
    tp_info = []
    for o in sorted(tp_orders_list, key=lambda x: x.order_id):
        tp_info.append({
            'order_id': str(o.order_id),
            'size': float(o.size),
            'price': float(o.price),
            'status': o.status,
        })
    
    tp_str = json.dumps(tp_info, sort_keys=True)
    return hashlib.md5(tp_str.encode()).hexdigest()
    
# =============================================================================
# API 접근
# =============================================================================
def get_api():
    """API 인스턴스 반환"""
    return api


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
    try:
        sync_position()
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
            long_entry_price = position_state[SYMBOL]["long"]["entry_price"]
            short_entry_price = position_state[SYMBOL]["short"]["entry_price"]
        
        if long_size == 0 and short_size == 0:
            return
        
        # ✅ tp_gap 값 조회
        tp_result = calculate_dynamic_tp_gap()
        
        if isinstance(tp_result, (tuple, list)) and len(tp_result) >= 2:
            long_tp = tp_result[0]
            short_tp = tp_result[1]
        else:
            long_tp = tp_gap_long
            short_tp = tp_gap_short
        
        # ✅ 타입 검증
        if not isinstance(long_tp, Decimal):
            long_tp = Decimal(str(long_tp))
        if not isinstance(short_tp, Decimal):
            short_tp = Decimal(str(short_tp))
        
        cancel_tp_only()
        time.sleep(0.5)
        
        # ✅ LONG TP (Decimal × Decimal만!)
        if long_size > 0:
            tp_price_long = long_entry_price * (Decimal("1") + long_tp)
            tp_price_long = tp_price_long.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)
            
            order = FuturesOrder(
                contract=SYMBOL,
                size=-int(long_size),
                price=str(tp_price_long),
                reduce_only=True,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ TP LONG", f"Qty: {int(long_size)}, Price: {float(tp_price_long):.4f}")
        
        time.sleep(0.3)
        
        # ✅ SHORT TP (Decimal × Decimal만!)
        if short_size > 0:
            tp_price_short = short_entry_price * (Decimal("1") - short_tp)
            tp_price_short = tp_price_short.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)
            
            order = FuturesOrder(
                contract=SYMBOL,
                size=int(short_size),
                price=str(tp_price_short),
                reduce_only=True,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ TP SHORT", f"Qty: {int(short_size)}, Price: {float(tp_price_short):.4f}")
        
        log("✅ TP", "All TP orders created successfully")
    
    except Exception as e:
        log("❌ TP REFRESH", f"Error: {e}")
        

# =============================================================================
# 수량 계산
# =============================================================================
def calculate_obv_macd_weight(obv_value):
    """
    OBV MACD 수치에 따른 진입 비율 계산 (사용자 지정)
    
    OBV 절댓값이 클수록 추세가 강함 → 더 많이 진입!
    """
    obv_abs = abs(obv_value)
    
    # ★ 사용자 지정 가중치
    if obv_abs <= 20:
        multiplier = Decimal("0.1")
    elif obv_abs <= 25:
        multiplier = Decimal("0.11")
    elif obv_abs <= 30:
        multiplier = Decimal("0.12")
    elif obv_abs <= 40:
        multiplier = Decimal("0.13")
    elif obv_abs <= 50:
        multiplier = Decimal("0.15")
    elif obv_abs <= 60:
        multiplier = Decimal("0.16")
    elif obv_abs <= 70:
        multiplier = Decimal("0.17")
    elif obv_abs <= 100:
        multiplier = Decimal("0.19")
    else:
        multiplier = Decimal("0.2")
    
    return multiplier

def get_current_price():
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if ticker and len(ticker) > 0 and ticker[0] and hasattr(ticker[0], 'last') and ticker[0].last:
            return Decimal(str(ticker[0].last))
        return Decimal("0")
    except (GateApiException, IndexError, AttributeError, ValueError) as e:
        log("❌", f"Price fetch error: {e}")
        return Decimal("0")

def calculate_grid_qty():
    with balance_lock:
        base_qty = int(Decimal(str(account_balance)) * BASERATIO)
        if base_qty <= 0:
            base_qty = 1
       
    # OBV MACD (tt1) 값 기준 동적 수량 조절
    obv_value = abs(float(obv_macd_value) * 100)  # 절댓값 추가
    if obv_value <= 20:
        multiplier = 1.0
    elif obv_value <= 25:
        multiplier = 1.1
    elif obv_value <= 30:
        multiplier = 1.2
    elif obv_value <= 40:
        multiplier = 1.3
    elif obv_value <= 50:
        multiplier = 1.5
    elif obv_value <= 60:
        multiplier = 1.6
    elif obv_value <= 70:
        multiplier = 1.7
    elif obv_value <= 100:
        multiplier = 1.9
    else:
        multiplier = 2.0
    
    return max(1, int(base_qty * multiplier))

def calculate_entry_ratio_by_loss(loss_pct: Decimal) -> Decimal:
    """
    손실도에 따른 동적 진입 비율 (loss_pct × 0.5)
    공식: entry_ratio = loss_pct / 200
    """
    try:
        entry_ratio = loss_pct / Decimal("200")
        
        MIN_RATIO = Decimal("0.01")
        if entry_ratio < MIN_RATIO:
            entry_ratio = MIN_RATIO
        
        MAX_RATIO = Decimal("0.5")
        if entry_ratio > MAX_RATIO:
            entry_ratio = MAX_RATIO
        
        return entry_ratio
    
    except Exception as e:
        log("❌ CALC_RATIO", f"Error: {e}")
        return Decimal("0.1")


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

def get_counter_side(main_side):
    """주력의 반대 방향 반환"""
    return "short" if main_side == "long" else "long"
    
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
        
        long_value = Decimal(str(long_size)) * Decimal(str(current_price))
        short_value = Decimal(str(short_size)) * Decimal(str(current_price))
        
        try:
            orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
            grid_count = sum(1 for o in orders if not o.reduce_only)
        except Exception as e:
            log("❌", f"List orders error: {e}")
            return
        
        # ❌ 삭제: 양방향 + 그리드 존재 체크 (시장가 전략에서는 불필요!)
        # 시장가 전략에서는 양방향 포지션이 정상 상태이므로 이 검증 제거
        
        # ✅ 검증 1: 단일 포지션 + 그리드 없음 → 그리드 생성!
        single_position = (long_size > 0 or short_size > 0) and not (long_size > 0 and short_size > 0)
        
        if single_position and grid_count == 0:
            log("🔧 VALIDATE", "Single position without grids → Creating grids!")
            initialize_grid(current_price)
            return
        
        # ✅ 검증 2: 최대 한도 초과 (완화: 20%)
        with balance_lock:
            max_value = Decimal(str(account_balance)) * MAXPOSITIONRATIO
        
        if long_value > max_value * Decimal("1.2"):
            log("🚨 EMERGENCY", f"LONG {float(long_value):.2f} > {float(max_value * 1.2):.2f}")
            emergency_close("long", long_size)
        
        if short_value > max_value * Decimal("1.2"):
            log("🚨 EMERGENCY", f"SHORT {float(short_value):.2f} > {float(max_value * 1.2):.2f}")
            emergency_close("short", short_size)
        
    except Exception as e:
        log("❌", f"Validation error: {e}")

def emergency_close(side, size):
    """긴급 청산 (최대 한도 초과 시)"""
    try:
        if size < 1:
            return
        
        order_size = int(size) if side == "long" else -int(size)
        
        order = FuturesOrder(
            contract=SYMBOL,
            size=order_size,
            price="0",
            tif="ioc",
            close=True,
            reduce_only=True
        )
        
        api.create_futures_order(SETTLE, order)
        log("🚨 EMERGENCY", f"{side.upper()} {abs(order_size)} emergency closed!")
        
    except Exception as e:
        log("❌", f"Emergency close error: {e}")

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
    역추세 전략 (수정됨!)
    
    OBV > 0 (롱 강세) → SHORT 주력 (더 많이!)
    OBV < 0 (숏 강세) → LONG 주력 (더 많이!)
    """
    global last_grid_time  
      
    if not initialize_grid_lock.acquire(blocking=False):
        log("🔵 GRID", "Already running → Skipping")
        return
    
    try:
        now = time.time()
        if now - last_grid_time < 10:
            log("🔵 GRID", f"Too soon ({now - last_grid_time:.1f}s) → Skipping")
            return
        
        last_grid_time = now
        
        if current_price is None or current_price == 0:
            current_price = get_current_price()
        
        if current_price == 0:
            log("❌", "Cannot get current price")
            return
        
        sync_position()
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        with balance_lock:
            max_value = account_balance * MAXPOSITIONRATIO
        
        current_price_dec = Decimal(str(current_price))
        long_value = Decimal(str(long_size)) * current_price_dec
        short_value = Decimal(str(short_size)) * current_price_dec
        
        if long_value >= max_value or short_value >= max_value:
            log("⚠️ LIMIT", "Max position reached")
            return
        
        obv_display = float(obv_macd_value) * 100
        obv_multiplier = calculate_obv_macd_weight(obv_display)
        
        with balance_lock:
            base_value = account_balance * BASERATIO
        
        base_qty = int(base_value / current_price_dec)
        
        if base_qty < 1:
            log("❌", f"Insufficient quantity")
            return
        
        # ✅ 수정: 강세 방향의 반대를 (1 + 배수)배로!
        if obv_display > 0:  # 롱 강세 → SHORT 주력 (더 많이!)
            short_qty = int(base_qty * (1 + obv_multiplier))  # ← 수정!
            long_qty = base_qty
            log("📊", f"OBV+ (롱 강세): SHORT {short_qty} (주력 x{float(1+obv_multiplier):.2f}) | LONG {long_qty} (헤징)")
        
        elif obv_display < 0:  # 숏 강세 → LONG 주력 (더 많이!)
            long_qty = int(base_qty * (1 + obv_multiplier))  # ← 수정!
            short_qty = base_qty
            log("📊", f"OBV- (숏 강세): LONG {long_qty} (주력 x{float(1+obv_multiplier):.2f}) | SHORT {short_qty} (헤징)")
        
        else:  # 중립
            long_qty = base_qty
            short_qty = base_qty
            log("📊", f"OBV 중립: LONG {long_qty} | SHORT {short_qty}")
        
        log("📊 QUANTITY", f"Long: {long_qty}, Short: {short_qty}, OBV={obv_display:.1f}, Multiplier={float(obv_multiplier):.2f}")
        
        try:
            order = FuturesOrder(
                contract=SYMBOL,
                size=long_qty,
                price="0",
                tif="ioc",
                reduce_only=False,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ ENTRY", f"LONG {long_qty} market")
        except GateApiException as e:
            log("❌", f"LONG entry error: {e}")
            return
        
        time.sleep(0.1)
        
        try:
            order = FuturesOrder(
                contract=SYMBOL,
                size=-short_qty,
                price="0",
                tif="ioc",
                reduce_only=False,
                text=generate_order_id()
            )
            api.create_futures_order(SETTLE, order)
            log("✅ ENTRY", f"SHORT {short_qty} market")
        except GateApiException as e:
            log("❌", f"SHORT entry error: {e}")
            return
        
        time.sleep(0.2)
        sync_position()
        refresh_all_tp_orders()
        
        log("🎉 GRID", "Market entry complete!")
        
    finally:
        initialize_grid_lock.release()


# ============================================================================
# 2️⃣ calculate_dynamic_tp_gap() - TP 정방향 수정 (5단계)
# ============================================================================

def calculate_dynamic_tp_gap():
    global last_tp_hash, tp_gap_long, tp_gap_short
    
    try:
        obv_value = get_obv_macd_value()
        
        # ✅ None/0 체크
        if obv_value is None or obv_value == 0:
            tp_gap_long = TPMIN
            tp_gap_short = TPMIN
            return (TPMIN, TPMIN, 0)
        
        # ✅ 안전한 변환
        try:
            obv_float = float(obv_value)
        except (ValueError, TypeError):
            obv_float = 0
        
        obv_display = obv_float * 100
        obv_abs = abs(obv_display)
        
        # ✅ 모두 Decimal로!
        if obv_abs < 10:
            tp_strength = Decimal("0.0019")
        elif obv_abs < 20:
            tp_strength = Decimal("0.0026")
        elif obv_abs < 30:
            tp_strength = Decimal("0.0031")
        elif obv_abs < 40:
            tp_strength = Decimal("0.0036")
        else:
            tp_strength = Decimal("0.0040")
        
        # ✅ 부호 확인 (float로만!)
        if obv_display > 0:
            tp_gap_long = tp_strength
            tp_gap_short = TPMIN
        elif obv_display < 0:
            tp_gap_long = TPMIN
            tp_gap_short = tp_strength
        else:
            tp_gap_long = TPMIN
            tp_gap_short = TPMIN
        
        tp_hash_new = hashlib.md5(f"{tp_gap_long}_{tp_gap_short}_{obv_display}".encode()).hexdigest()
        
        if tp_hash_new != last_tp_hash:
            log("📊 TP GAP", f"OBV={obv_display:.2f} | LONG={float(tp_gap_long)*100:.2f}% | SHORT={float(tp_gap_short)*100:.2f}%")
            last_tp_hash = tp_hash_new
        
        return (tp_gap_long, tp_gap_short)
        
    except Exception as e:
        log("❌ TP GAP", f"Error: {e}")
        return (TPMIN, TPMIN)


# ============================================================================
# ✅ 수정된 check_idle_and_enter() - 완전한 코드 (한 줄도 생략 없음!)
# ============================================================================

def check_idle_and_enter():
    """
    10분 아이들 진입 (손실 기반 가중치 적용!)
    
    당신의 요청:
    - base_qty = account_balance × BASERATIO / current_price (USDT 기반)
    - 손실도에 따른 추가 가중치: base_qty × (1 + loss_pct × 0.5 / 100)
    - OBV 가중치: main_qty = adjusted_qty × (1 + OBV_multiplier)
    """
    global last_event_time
        
    try:
        elapsed = time.time() - last_event_time
        if elapsed < IDLE_TIMEOUT:
            return
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
            long_entry_price = position_state[SYMBOL]["long"]["entry_price"]
            short_entry_price = position_state[SYMBOL]["short"]["entry_price"]
        
        # ✅ 양방향 포지션 있는지 확인
        if long_size == 0 or short_size == 0:
            log("⚠️ IDLE", "Not both sides → Skipping")
            return
        
        # ✅ 현재가 조회
        current_price = get_current_price()
        if current_price == 0:
            return
        
        # ✅ 최대 포지션 한도 체크
        with balance_lock:
            max_value = account_balance * MAXPOSITIONRATIO
        
        current_price_dec = Decimal(str(current_price))
        long_value = Decimal(str(long_size)) * current_price_dec
        short_value = Decimal(str(short_size)) * current_price_dec
        
        if long_value >= max_value or short_value >= max_value:
            log("⚠️ IDLE", "Max position reached")
            return
        
        # ✅ OBV MACD 가중치 계산
        obv_display = float(obv_macd_value) * 100
        obv_weight = calculate_obv_macd_weight(obv_display)
        
        log_event_header("IDLE ENTRY")
        log("⏱️ IDLE", f"Entry after {elapsed:.0f}s, OBV={obv_display:.1f}")
        log("📊 POSITION", f"Long: {long_size}, Short: {short_size}")
        
        # ========================================================================
        # 1️⃣ 주력 포지션 결정
        # ========================================================================
        if long_size >= short_size:
            main_size = long_size
            main_entry_price = long_entry_price
            is_long_main = True
            log("📊 MAIN", f"LONG is main: {main_size}")
        else:
            main_size = short_size
            main_entry_price = short_entry_price
            is_long_main = False
            log("📊 MAIN", f"SHORT is main: {main_size}")
        
        # ========================================================================
        # 2️⃣ 손실도 계산 (현재가 vs 평단가)
        # ========================================================================
        loss_pct = Decimal("0")
        if main_entry_price > 0:
            if is_long_main:
                # LONG 주력: 평단가 > 현재가 = 손실
                loss_pct = ((main_entry_price - current_price_dec) / main_entry_price) * Decimal("100")
            else:
                # SHORT 주력: 현재가 > 평단가 = 손실
                loss_pct = ((current_price_dec - main_entry_price) / main_entry_price) * Decimal("100")
        
        # 음수 손실(수익)은 0으로 처리
        if loss_pct < 0:
            loss_pct = Decimal("0")
        
        log("📊 LOSS", f"Main position loss: {float(loss_pct):.4f}%")
        
        # ========================================================================
        # 3️⃣ 기본 수량 계산 (USDT 기반!)
        # ========================================================================
        with balance_lock:
            base_usdt = account_balance * BASERATIO  # 720 × 0.02 = 14.4 USDT
        
        base_qty = int(base_usdt / current_price_dec)  # 14.4 / 0.2667 = 54개
        
        if base_qty < 1:
            base_qty = 1
        
        log("📊 BASE_QTY", f"Account {account_balance:.2f} × {BASERATIO} / {current_price:.4f} = {base_qty}")
        
        # ========================================================================
        # 4️⃣ 손실도 기반 가중치 적용 (핵심!)
        # ========================================================================
        # 공식: adjusted_qty = base_qty × (1 + loss_pct × 1.0 / 100)
        
        loss_multiplier = Decimal("1") + (loss_pct * Decimal("1.0") / Decimal("100"))
        adjusted_qty = int(Decimal(str(base_qty)) * loss_multiplier)
        
        log("📊 LOSS_WEIGHT", f"Base {base_qty} × (1 + {float(loss_pct):.2f}% × 1.0) = {adjusted_qty}")
        
        # ========================================================================
        # 5️⃣ OBV 가중치 적용
        # ========================================================================
        # main_qty (역방향 강화): adjusted_qty × (1 + OBV)
        # hedge_qty (주력방향): adjusted_qty
        
        main_qty = int(Decimal(str(adjusted_qty)) * (Decimal("1") + obv_weight))
        hedge_qty = adjusted_qty
        
        log("📊 CALC", f"Main: {adjusted_qty} × (1 + {float(obv_weight):.2f} OBV) = {main_qty}")
        log("📊 CALC", f"Hedge: {hedge_qty}")
        
        # ========================================================================
        # 6️⃣ 최소값 체크
        # ========================================================================
        if main_qty < 1:
            main_qty = 1
        if hedge_qty < 1:
            hedge_qty = 1
        
        log("📊 FINAL", f"Main: {main_qty}, Hedge: {hedge_qty}")
        
        # ========================================================================
        # 7️⃣ 양방향 진입 (시장가)
        # ========================================================================
        try:
            if is_long_main:
                # LONG 주력 → SHORT 역방향 강화 + LONG 헤징
                short_order = FuturesOrder(
                    contract=SYMBOL,
                    size=-main_qty,  # 음수 = SHORT
                    price="0",
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, short_order)
                log("✅ IDLE", f"SHORT {main_qty} (역방향 × OBV)")
                time.sleep(0.5)
                
                long_order = FuturesOrder(
                    contract=SYMBOL,
                    size=hedge_qty,  # 양수 = LONG
                    price="0",
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, long_order)
                log("✅ IDLE", f"LONG {hedge_qty} (주력방향)")
            
            else:
                # SHORT 주력 → LONG 역방향 강화 + SHORT 헤징
                long_order = FuturesOrder(
                    contract=SYMBOL,
                    size=main_qty,  # 양수 = LONG
                    price="0",
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, long_order)
                log("✅ IDLE", f"LONG {main_qty} (역방향 × OBV)")
                time.sleep(0.5)
                
                short_order = FuturesOrder(
                    contract=SYMBOL,
                    size=-hedge_qty,  # 음수 = SHORT
                    price="0",
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, short_order)
                log("✅ IDLE", f"SHORT {hedge_qty} (주력방향)")
        
        except GateApiException as e:
            log("❌", f"IDLE entry error: {e}")
            return
        
        # ========================================================================
        # 8️⃣ 마무리
        # ========================================================================
        time.sleep(0.5)
        sync_position()
        refresh_all_tp_orders()
        update_event_time()
        log("🎉 IDLE", "Complete!")
        
    except Exception as e:
        log("❌", f"Idle entry error: {e}")

def market_entry_when_imbalanced():
    """
    포지션 불균형 시 OBV MACD 가중치로 시장가 진입
    
    상황:
    1️⃣ 포지션 없음 (L=0, S=0) → 양방향 진입
    2️⃣ LONG만 있음 → SHORT 헤징
    3️⃣ SHORT만 있음 → LONG 헤징
    """
    global obv_macd_value
    
    try:
        sync_position()
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        has_position = long_size > 0 or short_size > 0
        balanced = long_size > 0 and short_size > 0
        
        # 불균형만 처리
        if not has_position or (has_position and not balanced):
            
            calculate_obv_macd()
            obv_display = float(obv_macd_value) * 100
            obv_multiplier = calculate_obv_macd_weight(obv_display)
            
            with balance_lock:
                current_price = get_current_price()
                if current_price == 0:
                    return
                base_qty = int(account_balance * BASERATIO / current_price)
                if base_qty <= 0:
                    base_qty = 1
            
            log("📊 MARKET", f"Imbalanced - Long: {long_size}, Short: {short_size}, OBV: {obv_display:.1f}")
            
            # ════════════════════════════════════════════════════════════════
            # 1️⃣ 포지션 없음: 양방향 진입
            # ════════════════════════════════════════════════════════════════
            if not has_position:
                log("💰 MARKET", "No position → Entering both sides!")
                
                # ✅ OBV 가중치 기본 적용
                entry_qty = int(base_qty * obv_multiplier)
                
                log("📊 QTY", f"LONG {entry_qty} | SHORT {entry_qty} (OBV x{float(obv_multiplier):.2f})")
                
                try:
                    # ✅ LONG 진입
                    long_order = FuturesOrder(
                        contract=SYMBOL,
                        size=entry_qty,
                        price="0",
                        tif="ioc",
                        reduce_only=False,  # ← 추가: 새로 진입
                        text=generate_order_id()
                    )
                    api.create_futures_order(SETTLE, long_order)
                    log("✅ LONG", f"Market: {entry_qty}")
                    time.sleep(0.5)
                    
                    # ✅ SHORT 진입 (수정!)
                    short_order = FuturesOrder(
                        contract=SYMBOL,
                        size=-entry_qty,  # ← 음수 (SHORT)
                        price="0",
                        tif="ioc",
                        reduce_only=False,  # ← 추가: 새로 진입
                        text=generate_order_id()
                    )
                    api.create_futures_order(SETTLE, short_order)
                    log("✅ SHORT", f"Market: {entry_qty}")
                
                except GateApiException as e:
                    log("❌ MARKET", f"Entry error: {e}")
                    return
            
            # ════════════════════════════════════════════════════════════════
            # 2️⃣ LONG만 있음: SHORT 헤징
            # ════════════════════════════════════════════════════════════════
            elif long_size > 0 and short_size == 0:
                log("💰 MARKET", "Only LONG → Adding SHORT hedge!")
                
                # ✅ OBV 가중치로 헤징 수량 결정
                hedge_qty = int(base_qty * obv_multiplier)
                
                # ✅ 기본 수량보다 작으면 조정
                if hedge_qty < base_qty:
                    log("📊 ADJUST", f"Hedge qty {hedge_qty} < base {base_qty} → Using base qty")
                    hedge_qty = base_qty
                
                log("📊 QTY", f"SHORT {hedge_qty} (OBV x{float(obv_multiplier):.2f})")
                
                try:
                    short_order = FuturesOrder(
                        contract=SYMBOL,
                        size=-hedge_qty,  # ← 음수 (SHORT)
                        price="0",
                        tif="ioc",
                        reduce_only=False,  # ← 추가: 새로 진입
                        text=generate_order_id()
                    )
                    api.create_futures_order(SETTLE, short_order)
                    log("✅ SHORT", f"Hedge: {hedge_qty}")
                except GateApiException as e:
                    log("❌ MARKET", f"SHORT error: {e}")
                    return
            
            # ════════════════════════════════════════════════════════════════
            # 3️⃣ SHORT만 있음: LONG 헤징
            # ════════════════════════════════════════════════════════════════
            elif short_size > 0 and long_size == 0:
                log("💰 MARKET", "Only SHORT → Adding LONG hedge!")
                
                # ✅ OBV 가중치로 헤징 수량 결정
                hedge_qty = int(base_qty * obv_multiplier)
                
                # ✅ 기본 수량보다 작으면 조정
                if hedge_qty < base_qty:
                    log("📊 ADJUST", f"Hedge qty {hedge_qty} < base {base_qty} → Using base qty")
                    hedge_qty = base_qty
                
                log("📊 QTY", f"LONG {hedge_qty} (OBV x{float(obv_multiplier):.2f})")
                
                try:
                    long_order = FuturesOrder(
                        contract=SYMBOL,
                        size=hedge_qty,  # ← 양수 (LONG)
                        price="0",
                        tif="ioc",
                        reduce_only=False,  # ← 추가: 새로 진입
                        text=generate_order_id()
                    )
                    api.create_futures_order(SETTLE, long_order)
                    log("✅ LONG", f"Hedge: {hedge_qty}")
                except GateApiException as e:
                    log("❌ MARKET", f"LONG error: {e}")
                    return
    
    except Exception as e:
        log("❌ MARKET", f"Imbalanced entry error: {e}")


# =============================================================================
# 시스템 새로고침
# =============================================================================
def full_refresh(event_type, skip_grid=False):
    """
    시스템 새로고침 + 물량 누적 방지 로직
    
    주력 > 2배 AND TP 체결 → 반대쪽 50% 청산 (시장가)
    """
    log_event_header(f"FULL REFRESH: {event_type}")
    
    log("🔄 SYNC", "Syncing position...")
    sync_position()
    log_position_state()

    cancel_all_orders()
    time.sleep(0.5)
      
    # 기존 로직
    if not skip_grid:
        current_price = get_current_price()
        if current_price > 0:
            initialize_grid(current_price)
    
    refresh_all_tp_orders()
    
    sync_position()
    log_position_state()
    log("✅ REFRESH", f"Complete: {event_type}")


# ✅ 헬퍼 함수 추가 (이미 있으면 스킵)
def get_counter_side(side):
    """주력의 반대 방향 반환"""
    return "short" if side == "long" else "long"


# =============================================================================
# 모니터링 스레드
# =============================================================================
async def grid_fill_monitor():
    """
    WebSocket으로 TP 체결 모니터링
    
    기능:
    1. TP 체결 감지
    2. 비주력 TP 물량 기록
    3. handle_non_main_position_tp(tp_qty) 호출 ← 신규!
    4. 양방향 TP 체결 → Full Refresh
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
                                
                                log("🔍 WS RAW", f"id={order_data.get('id')}, status={order_data.get('status')}, finish_as={order_data.get('finish_as')}, size={order_data.get('size')}")
                                
                                finish_as = order_data.get("finish_as", "")
                                status = order_data.get("status", "")
                                
                                is_filled = (
                                    finish_as in ["filled", "ioc"] or 
                                    status in ["finished", "closed"]
                                )
                                
                                if not is_filled:
                                    continue
                                
                                log("🔍 DEBUG", f"Order filled detected: id={order_data.get('id')}, finish_as={finish_as}, status={status}")
                                
                                is_reduce_only = order_data.get("is_reduce_only", False)
                                order_id = order_data.get("id")
                                size = order_data.get("size", 0)
                                price = float(order_data.get("price", 0))
                                
                                # ✅ TP 체결만 처리!
                                if is_reduce_only:
                                    side = "long" if size < 0 else "short"
                                    tp_qty = abs(int(size))
                                    
                                    log("🎯 TP FILLED", f"{side.upper()} {tp_qty}개 @ {price:.4f}")
                                    
                                    time.sleep(0.5)
                                    sync_position()
                                    
                                    # ✅ 신규: 물량 누적 방지 함수 호출!
                                    handle_non_main_position_tp(tp_qty)
                                    
                                    time.sleep(0.5)
                                    
                                    with position_lock:
                                        long_size = position_state[SYMBOL]["long"]["size"]
                                        short_size = position_state[SYMBOL]["short"]["size"]
                                    
                                    # ✅ 양방향 TP 체결 감지: LONG & SHORT 모두 0
                                    if long_size == 0 and short_size == 0:
                                        log("🎯 BOTH CLOSED", "Both sides closed → Full refresh")
                                        update_event_time()
                                        
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
                        
                        # ✅ 수정: skip_grid=False (그리드도 생성!)
                        full_refresh("Average_TP", skip_grid=False)

                        update_event_time()  # 이벤트 시간 갱신
                        
                        break
                except:
                    pass
        
        except Exception as e:
            log("❌", f"TP monitor error: {e}")
            time.sleep(1)

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

def periodic_health_check():
    """
    2분마다 실행되는 헬스 체크
    """
    global last_idle_check, obv_macd_value, tp_gap_min, tp_gap_max, last_adjusted_obv, tp_order_hash, account_balance
    
    while True:
        try:
            time.sleep(120)
            log("💊 HEALTH", "Starting health check...")
            
            # 계좌 잔고 조회
            try:
                # 방법 1: Unified Account (USDT)
                try:
                    unified_account = unified_api.list_unified_accounts(currency="USDT")
                    
                    if unified_account and len(unified_account) > 0:
                        usdt_account = unified_account[0]
                        available_str = getattr(usdt_account, 'available', None)
                        
                        if available_str:
                            balance_dec = Decimal(str(available_str))
                            if balance_dec > 0:
                                with balance_lock:
                                    old_balance = account_balance
                                    account_balance = balance_dec
                                
                                if old_balance != account_balance:
                                    log("💰 BALANCE", f"{account_balance:.2f} USDT")
                                
                                max_position = account_balance * MAXPOSITIONRATIO
                                log("📊 MAX POSITION", f"{max_position:.2f} USDT")
                        else:
                            raise ValueError("No available balance")
                    else:
                        raise ValueError("No USDT account")
                
                except Exception:
                    # 방법 2: Futures Account
                    futures_account = api.list_futures_accounts(SETTLE)
                    if futures_account:
                        available_str = getattr(futures_account, 'available', None)
                        if available_str:
                            balance_dec = Decimal(str(available_str))
                            if balance_dec > 0:
                                with balance_lock:
                                    account_balance = balance_dec
                                log("💰 BALANCE", f"{account_balance:.2f} USDT (Futures)")
                                
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
                            
                            current_TPMIN = float(tp_gap_min)
                            new_TPMIN = float(new_tp_long)
                            TPMIN_change = abs(new_TPMIN - current_TPMIN)
                            
                            if TPMIN_change >= 0.0001:  # 0.01% 이상 변화
                                log("🔄 TP ADJUST", f"OBV: {current_obv:.6f}, New TP: {new_TPMIN*100:.2f}%")
                                
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
    global account_balance
    
    # 스타트업 로그
    log("divider", "=" * 80)
    log("🚀 START", "ARB Trading Bot v26.0")
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
    
    # ★ 계좌 잔고 조회 (Total Balance = 가용 + 포지션 + 미실현손익)
    try:
        log("💰 BALANCE", "Fetching account balance...")
        
        # Futures Account 조회
        futures_account = api.list_futures_accounts(SETTLE)
        
        if futures_account:
            # total 필드 우선 (전체 자산)
            total_str = getattr(futures_account, 'total', None)
            
            if total_str:
                balance_dec = Decimal(str(total_str))
                if balance_dec > 0:
                    with balance_lock:
                        account_balance = balance_dec
                    
                    # available도 표시 (참고용)
                    available_str = getattr(futures_account, 'available', None)
                    if available_str:
                        available_dec = Decimal(str(available_str))
                        log("💰 BALANCE", f"Total: {account_balance:.2f} USDT (Available: {available_dec:.2f})")
                    else:
                        log("💰 BALANCE", f"Total: {account_balance:.2f} USDT")
                    
                    # unrealized_pnl 표시 (참고용)
                    unrealized_pnl_str = getattr(futures_account, 'unrealised_pnl', None)
                    if unrealized_pnl_str:
                        pnl_dec = Decimal(str(unrealized_pnl_str))
                        log("📊 UNREALIZED PNL", f"{pnl_dec:.2f} USDT")
                    
                    # MAX POSITION 계산
                    max_position = account_balance * MAXPOSITIONRATIO
                    log("📊 MAX POSITION", f"{max_position:.2f} USDT")
                else:
                    log("⚠️ WARNING", f"Total balance is 0. Using default {INITIALBALANCE} USDT")
                    with balance_lock:
                        account_balance = INITIALBALANCE
            else:
                # total이 없으면 available 사용 (fallback)
                log("ℹ️ INFO", "Total field not found. Using available...")
                available_str = getattr(futures_account, 'available', None)
                if available_str:
                    balance_dec = Decimal(str(available_str))
                    if balance_dec > 0:
                        with balance_lock:
                            account_balance = balance_dec
                        log("💰 BALANCE", f"{account_balance:.2f} USDT (Available only)")
                        
                        max_position = account_balance * MAXPOSITIONRATIO
                        log("📊 MAX POSITION", f"{max_position:.2f} USDT")
                    else:
                        log("⚠️ WARNING", f"Available balance is 0. Using default {INITIALBALANCE} USDT")
                        with balance_lock:
                            account_balance = INITIALBALANCE
                else:
                    log("❌ ERROR", "No balance fields found")
                    with balance_lock:
                        account_balance = INITIALBALANCE
        else:
            log("❌ ERROR", "Could not fetch Futures Account")
            with balance_lock:
                account_balance = INITIALBALANCE
    
    except GateApiException as ex:
        log("❌ ERROR", f"Gate API Error: {ex.label} - {ex.message}")
        with balance_lock:
            account_balance = INITIALBALANCE
    except Exception as e:
        log("❌ ERROR", f"Balance check failed: {e}")
        with balance_lock:
            account_balance = INITIALBALANCE
    
    log("divider", "-" * 80)
        
    # 포지션 동기화
    sync_position()
    log_position_state()
    
    # 포지션 동기화
    sync_position()
    log_position_state()
    
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

def start_grid_monitor():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(grid_fill_monitor())
    
if __name__ == '__main__':
    # API 키 확인
    if not API_KEY or not API_SECRET:
        log("❌ FATAL", "Cannot start without API credentials!")
        log("ℹ️ INFO", "Set Railway environment variables:")
        log("", "  - API_KEY")
        log("", "  - API_SECRET")
        log("", "  - SYMBOL (optional, default: ARB_USDT)")
        exit(1)
    
    # 이벤트 타임 초기화
    update_event_time()
    
    # API 연결 테스트
    try:
        test_ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if test_ticker:
            log("✅ API", "Connection test successful")
    except GateApiException as e:
        log("❌ API", f"Connection test failed: {e}")
        log("⚠️ WARNING", "Check API key permissions:")
        log("", "  - Futures: Read + Trade")
        log("", "  - Unified Account: Read")
    except Exception as e:
        log("❌ API", f"Connection test error: {e}")
    
    # 스타트업 요약 출력 (잔고 조회 + 포지션 동기화 + 초기화)
    print_startup_summary()
    
    # 스레드 시작
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
    log("📊 OBV MACD", "Self-calculating from 3min candles")
    log("📡 WEBHOOK", "Optional: TradingView webhook at /webhook")
    log("💊 HEALTH", "Health check every 2 minutes")
    
    log("divider", "=" * 80)
    
    # Flask 서버 시작
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

