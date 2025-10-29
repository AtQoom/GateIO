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
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SYMBOL = os.environ.get("SYMBOL", "ONDO_USDT")
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
# Lock (중복 방지)
# =============================================================================
initialize_grid_lock = threading.Lock()
refresh_tp_lock = threading.Lock()
hedge_lock = threading.Lock()

GRID_GAP_PCT = Decimal("0.0019")  # 0.19%
TP_GAP_PCT = Decimal("0.0019")    # 0.19%
BASE_RATIO = Decimal("0.1")       # 기본 수량 비율
THRESHOLD_RATIO = Decimal("0.8")  # 임계값
COUNTER_RATIO = Decimal("0.30")   # 비주력 30%
COUNTER_CLOSE_RATIO = Decimal("0.20")  # 비주력 20% 청산
MAX_POSITION_RATIO = Decimal("5.0")    # 최대 5배
HEDGE_RATIO_MAIN = Decimal("0.10")     # 주력 10%
POSITION_SCALE_RATIO = Decimal("0.20")  # ✅ 새로 추가! 포지션 비례 20%

# =============================================================================
# API 설정
# =============================================================================
config = Configuration(key=API_KEY, secret=API_SECRET)
# Host 명시적 설정 및 검증 비활성화
config.host = "https://api.gateio.ws/api/v4"
config.verify_ssl = True
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

# 임계값 초과 후 진입 추적
post_threshold_entries = {
    SYMBOL: {
        "long": deque(maxlen=100),
        "short": deque(maxlen=100)
    }
}

# 비주력 포지션 스냅샷
counter_position_snapshot = {
    SYMBOL: {"long": Decimal("0"), "short": Decimal("0")}
}

# 평단 TP 주문 ID
average_tp_orders = {
    SYMBOL: {"long": None, "short": None}
}

# 최대 보유 한도 잠금
max_position_locked = {"long": False, "short": False}

# 그리드 주문 추적
grid_orders = {SYMBOL: {"long": [], "short": []}}

# OBV MACD 값 (자체 계산)
obv_macd_value = Decimal("0")
last_grid_time = 0

# OBV MACD 계산용 히스토리
kline_history = deque(maxlen=200)

account_balance = INITIAL_BALANCE  # 추가
ENABLE_AUTO_HEDGE = True
last_event_time = 0  # 마지막 이벤트 시간 (그리드 체결 또는 TP 체결)
IDLE_TIMEOUT = 1800  # 30분 (초 단위)
idle_entry_count = 0  # 아이들 진입 횟수 ← 추가

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
        long_price = position_state[SYMBOL]["long"]["price"]
        short_size = position_state[SYMBOL]["short"]["size"]
        short_price = position_state[SYMBOL]["short"]["price"]
    
    with balance_lock:
        balance = account_balance
    
    threshold = balance * THRESHOLD_RATIO
    long_value = long_price * long_size
    short_value = short_price * short_size
    
    log("📊 POSITION", f"Long: {long_size} @ {long_price:.4f} (${long_value:.2f})")
    log("📊 POSITION", f"Short: {short_size} @ {short_price:.4f} (${short_value:.2f})")
    log("📊 THRESHOLD", f"${threshold:.2f} | Long {'✅' if long_value >= threshold else '❌'} | Short {'✅' if short_value >= threshold else '❌'}")
    
    main = get_main_side()
    if main != "none":
        log("📊 MAIN", f"{main.upper()} (더 큰 포지션)")

def log_threshold_info():
    """임계값 정보 로그"""
    with balance_lock:
        balance = account_balance  # 실시간 잔고
    with position_lock:
        long_size = position_state[SYMBOL]["long"]["size"]
        long_price = position_state[SYMBOL]["long"]["price"]
        short_size = position_state[SYMBOL]["short"]["size"]
        short_price = position_state[SYMBOL]["short"]["price"]
    
    threshold = balance * THRESHOLD_RATIO  # account_balance 기준
    long_value = long_price * long_size
    short_value = short_price * short_size
    
    log("💰 THRESHOLD", f"${threshold:.2f} | Long: ${long_value:.2f} {'✅' if long_value >= threshold else '❌'} | Short: ${short_value:.2f} {'✅' if short_value >= threshold else '❌'}")
    log("💰 BALANCE", f"Current: ${balance:.2f} USDT")

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

def calculate_obv_macd():
    """
    OBV MACD 계산 - TradingView 범위에 맞게 정규화
    반환값: -0.01 ~ 0.01 범위 (로그 표시 시 *1000)
    """
    if len(kline_history) < 60:
        return 0
    
    try:
        # 데이터 추출
        closes = [k['close'] for k in kline_history]
        highs = [k['high'] for k in kline_history]
        lows = [k['low'] for k in kline_history]
        volumes = [k['volume'] for k in kline_history]
        
        window_len = 28
        v_len = 14
        
        # price_spread 계산
        hl_diff = [highs[i] - lows[i] for i in range(len(highs))]
        price_spread = stdev(hl_diff, window_len)
        
        if price_spread == 0:
            return 0
        
        # OBV 계산 (누적)
        obv_values = [0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv_values.append(obv_values[-1] + volumes[i])
            elif closes[i] < closes[i-1]:
                obv_values.append(obv_values[-1] - volumes[i])
            else:
                obv_values.append(obv_values[-1])
        
        if len(obv_values) < v_len + window_len:
            return 0
        
        # OBV smooth
        smooth = sma(obv_values, v_len)
        
        # v_spread 계산
        v_diff = [obv_values[i] - smooth for i in range(len(obv_values))]
        v_spread = stdev(v_diff, window_len)
        
        if v_spread == 0:
            return 0
        
        # shadow 계산 (정규화) - Pine Script와 동일
        if len(obv_values) == 0 or len(obv_values) <= smooth:
            return 0
        shadow = (obv_values[-1] - smooth) / v_spread * price_spread
        
        # out 계산
        out = highs[-1] + shadow if shadow > 0 else lows[-1] + shadow
        
        # obvema (len10=1이므로 그대로)
        obvema = out
        
        # DEMA 계산 (len=9) - Pine Script 정확히 구현
        ma = obvema
        
        # MACD 계산
        slow_ma = ema(closes, 26)
        macd = ma - slow_ma
        
        # Slope 계산 (len5=2)
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
        
        # 현재가 기준 정규화
        current_price = closes[-1]
        if current_price <= 0:
            return 0
        
        # 가격 대비 퍼센트로 변환 후 추가 스케일링
        normalized = (tt1 / current_price) / 100.0
        
        # 볼륨 기반 추가 정규화
        avg_volume = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else 1
        if avg_volume > 0:
            normalized = normalized / (avg_volume / 1000000.0)
        
        # -0.01 ~ 0.01 범위로 반환 (내부 저장용)
        return normalized
        
    except Exception as e:
        log("❌", f"OBV MACD calculation error: {e}")
        return 0

# =============================================================================
# 잔고 업데이트
# =============================================================================
def update_balance_thread():
    global account_balance  # INITIAL_BALANCE 대신 account_balance 사용
    first_run = True
    
    while True:
        try:
            if not first_run:
                time.sleep(3600)  # 1시간마다
            first_run = False
            
            # Unified Account total 잔고 조회
            try:
                accounts = unified_api.list_unified_accounts()
                if accounts and hasattr(accounts, 'total') and accounts.total:
                    old_balance = account_balance
                    account_balance = Decimal(str(accounts.total))
                    if old_balance != account_balance:
                        log("💰 BALANCE", f"Updated: {old_balance:.2f} → {account_balance:.2f} USDT (Unified Total)")
                else:
                    # Futures 계좌 available로 대체
                    futures_accounts = api.list_futures_accounts(SETTLE)
                    if futures_accounts and hasattr(futures_accounts, 'available') and futures_accounts.available:
                        old_balance = account_balance
                        account_balance = Decimal(str(futures_accounts.available))
                        if old_balance != account_balance:
                            log("💰 BALANCE", f"Futures: {old_balance:.2f} → {account_balance:.2f} USDT")
            except Exception as e:
                log("⚠️", f"Balance fetch error: {e}")
                
        except GateApiException as e:
            log("⚠️", f"Balance update: API error - {e}")
            time.sleep(60)
        except Exception as e:
            log("❌", f"Balance update error: {e}")
            time.sleep(60)


# =============================================================================
# 캔들 데이터 수집
# =============================================================================
def fetch_kline_thread():
    """1분봉 데이터 수집 및 OBV MACD 계산"""
    global obv_macd_value
    last_fetch = 0
    
    while True:
        try:
            current_time = time.time()
            if current_time - last_fetch < 60:  # 1분마다
                time.sleep(5)
                continue
            
            # 1분봉 데이터 가져오기
            try:
                candles = api.list_futures_candlesticks(
                    SETTLE, 
                    contract=SYMBOL, 
                    interval='1m',
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
                    
                    # OBV MACD 계산 (로그는 그리드 생성 시에만 출력)
                    calculated_value = calculate_obv_macd()
                    if calculated_value != 0 or obv_macd_value != 0:
                        obv_macd_value = Decimal(str(calculated_value))
                    
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
                                position_state[SYMBOL]["long"]["price"] = entry_price
                        elif size_dec < 0:
                            with position_lock:
                                position_state[SYMBOL]["short"]["size"] = abs(size_dec)
                                position_state[SYMBOL]["short"]["price"] = entry_price
            
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
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
        if not orders: 
            return
        log("🗑️ CANCEL", f"{len(orders)} orders")
        for order in orders:
            try:
                api.cancel_futures_order(SETTLE, order.id)
                time.sleep(0.1)
            except GateApiException as e:
                if "ORDER_NOT_FOUND" not in str(e):
                    log("⚠️", f"Cancel order {order.id}: {e}")
            except:
                pass
        grid_orders[SYMBOL] = {"long": [], "short": []}
        average_tp_orders[SYMBOL] = {"long": None, "short": None}
    except GateApiException as e:
        if "400" in str(e):
            log("⚠️", "Cancel orders: API authentication error")
        else:
            log("❌", f"Order cancellation error: {e}")
    except Exception as e:
        log("❌", f"Order cancellation error: {e}")

def cancel_grid_only():
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
        grid_orders_to_cancel = [o for o in orders if not o.is_reduce_only]
        if not grid_orders_to_cancel: 
            return
        log("🗑️ CANCEL", f"{len(grid_orders_to_cancel)} grid orders")
        for order in grid_orders_to_cancel:
            try:
                api.cancel_futures_order(SETTLE, order.id)
                time.sleep(0.1)
            except GateApiException as e:
                if "ORDER_NOT_FOUND" not in str(e):
                    log("⚠️", f"Cancel grid {order.id}: {e}")
            except:
                pass
        grid_orders[SYMBOL] = {"long": [], "short": []}
    except GateApiException as e:
        if "400" in str(e):
            log("⚠️", "Cancel grids: API authentication error")
        else:
            log("❌", f"Grid cancellation error: {e}")
    except Exception as e:
        log("❌", f"Grid cancellation error: {e}")


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


# =============================================================================
# 수량 계산
# =============================================================================
def calculate_obv_macd_weight(tt1_value):
    """OBV MACD 값에 따른 동적 배수 (*1000 적용된 값 기준)"""
    abs_val = abs(tt1_value)
    if abs_val < 5:
        return Decimal("0.10")
    elif abs_val < 10:
        return Decimal("0.11")
    elif abs_val < 15:
        return Decimal("0.12")
    elif abs_val < 20:
        return Decimal("0.13")
    elif abs_val < 30:
        return Decimal("0.15")
    elif abs_val < 40:
        return Decimal("0.16")
    elif abs_val < 50:
        return Decimal("0.17")
    elif abs_val < 70:
        return Decimal("0.18")
    elif abs_val < 100:
        return Decimal("0.19")
    else:
        return Decimal("0.20")

def get_current_price():
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if ticker and len(ticker) > 0 and ticker[0] and hasattr(ticker[0], 'last') and ticker[0].last:
            return Decimal(str(ticker[0].last))
        return Decimal("0")
    except (GateApiException, IndexError, AttributeError, ValueError) as e:
        log("❌", f"Price fetch error: {e}")
        return Decimal("0")

def calculate_grid_qty(is_above_threshold):
    with balance_lock:
        base_qty = int(Decimal(str(account_balance)) * BASE_RATIO)
        if base_qty <= 0:
            base_qty = 1
    
    if is_above_threshold:
        return base_qty
    
    # OBV MACD (tt1) 값 기준 동적 수량 조절
    obv_value = abs(float(obv_macd_value) * 1000)  # 절댓값 추가
    if obv_value <= 5:
        multiplier = 0.1
    elif obv_value <= 10:
        multiplier = 0.11
    elif obv_value <= 15:
        multiplier = 0.12
    elif obv_value <= 20:
        multiplier = 0.13
    elif obv_value <= 30:
        multiplier = 0.15
    elif obv_value <= 40:
        multiplier = 0.16
    elif obv_value <= 50:
        multiplier = 0.17
    elif obv_value <= 70:
        multiplier = 0.18
    elif obv_value <= 100:
        multiplier = 0.19
    else:
        multiplier = 0.2
    
    return max(1, int(base_qty * multiplier))

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

def get_counter_side(side):
    """반대 방향 포지션 가져오기"""
    return "short" if side == "long" else "long"

def update_event_time():
    """마지막 이벤트 시간 갱신 + 아이들 카운트 리셋"""
    global last_event_time, idle_entry_count
    last_event_time = time.time()
    idle_entry_count = 0  # ← 추가: 이벤트 발생 시 카운트 리셋
    
def is_above_threshold(side):
    """포지션이 임계값을 초과했는지 확인"""
    with position_lock:
        size = position_state[SYMBOL][side]["size"]
        price = position_state[SYMBOL][side]["price"]
    
    with balance_lock:
        threshold = account_balance * THRESHOLD_RATIO  # account_balance 기준
    
    value = price * size
    return value >= threshold

# =============================================================================
# 주문 실행
# =============================================================================
def place_grid_order(side, price, qty, is_counter=False, base_qty=2):
    try:
        if qty <= 0:
            log("⚠️ GRID", f"Invalid quantity: {qty}")
            return None
        
        # ✅ 추가: side 검증
        if side not in ["long", "short"]:
            log("❌ GRID", f"Invalid side: {side}")
            return None
            
        size = qty if side == "long" else -qty
        order = FuturesOrder(
            contract=SYMBOL, 
            size=int(size), 
            price=str(price), 
            tif="gtc"
        )
        result = api.create_futures_order(SETTLE, order)
        if result and hasattr(result, 'id'):
            # ✅ 추가: 안전한 접근
            if SYMBOL not in grid_orders:
                grid_orders[SYMBOL] = {"long": [], "short": []}
            if side not in grid_orders[SYMBOL]:
                grid_orders[SYMBOL][side] = []
            
            grid_orders[SYMBOL][side].append({
                "order_id": result.id,
                "price": float(price),
                "qty": int(qty),
                "is_counter": is_counter,
                "base_qty": int(base_qty)
            })
            tag = "Counter(30%)" if is_counter else "Same"
            log("📐 GRID", f"{tag} {side.upper()} {qty} @ {price:.4f}")
        return result
    except GateApiException as e:
        if "400" in str(e):
            log("❌", f"Grid order ({side}): API authentication error - {e}")
        else:
            log("❌", f"Grid order ({side}): {e}")
        return None
    except Exception as e:
        log("❌", f"Grid order error ({side}): {e}")
        return None

# =============================================================================
# TITLE 17-1. 전략 일관성 검증
# =============================================================================

def validate_strategy_consistency():
    """전략 일관성 검증 (상태 검증)"""
    
    try:
        log("🔍 VALIDATE", "Strategy consistency check...")
        
        sync_position()
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        current_price = get_current_price()
        if current_price == 0:
            return
        
        long_value = Decimal(str(long_size)) * Decimal(str(current_price))
        short_value = Decimal(str(short_size)) * Decimal(str(current_price))
        
        # 1. 그리드 검증
        grid_count = 0
        
        try:
            # ✅ 수정: status 파라미터 제거 (기본값 'open' 사용)
            orders = api.list_futures_orders(SETTLE, SYMBOL)
            for o in orders:
                if o.status == 'open' and o.reduce_only == False:
                    grid_count += 1
        except Exception as e:
            log("❌", f"List orders error: {e}")
            return
        
        # ✅ 검증 1: 양방향 포지션 + 그리드 존재
        if long_size > 0 and short_size > 0 and grid_count > 0:
            log("🚨 INVALID", f"Both positions with {grid_count} grids → Canceling!")
            cancel_grid_only()
        
        # ✅ 검증 2: 단일 포지션 + 그리드 없음
        if (long_size > 0 or short_size > 0) and long_size * short_size == 0:
            if grid_count == 0:
                log("🚨 INVALID", "Single position with no grid → Creating!")
                time.sleep(0.5)
                initialize_grid(current_price)
        
        # ✅ 검증 3: 최대 한도 초과
        with balance_lock:
            max_value = Decimal(str(account_balance)) * MAX_POSITION_RATIO
        
        if long_value > max_value * Decimal("1.1"):
            log("🚨 EMERGENCY", f"LONG {float(long_value):.2f} > {float(max_value * 1.1):.2f} → Market close!")
            emergency_close("long", long_size)
        
        if short_value > max_value * Decimal("1.1"):
            log("🚨 EMERGENCY", f"SHORT {float(short_value):.2f} > {float(max_value * 1.1):.2f} → Market close!")
            emergency_close("short", short_size)
        
        # ✅ 검증 4: TP 수량 불일치
        tp_orders_list = []
        try:
            # ✅ 수정: status 파라미터 제거
            orders = api.list_futures_orders(SETTLE, SYMBOL)
            tp_orders_list = [o for o in orders if o.status == 'open' and o.reduce_only == True]
        except:
            pass
        
        tp_long_qty = sum(abs(o.size) for o in tp_orders_list if o.size > 0)
        tp_short_qty = sum(abs(o.size) for o in tp_orders_list if o.size < 0)
        
        if (long_size > 0 and abs(tp_long_qty - long_size) > 0.1) or \
           (short_size > 0 and abs(tp_short_qty - short_size) > 0.1):
            log("🚨 INVALID", f"TP mismatch (L:{long_size} vs {tp_long_qty}, S:{short_size} vs {tp_short_qty}) → Refreshing!")
            time.sleep(0.5)
            refresh_all_tp_orders()
        
        log("✅ VALIDATE", "Strategy consistency OK")
        
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
            price="0",  # 시장가
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
        # ✅ 수정: status 파라미터 제거
        orders = api.list_futures_orders(SETTLE, SYMBOL)
        orders = [o for o in orders if o.status == 'open']
        
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
        # ✅ 수정: status 파라미터 제거
        orders = api.list_futures_orders(SETTLE, SYMBOL)
        orders = [o for o in orders if o.status == 'open']
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

def initialize_grid(current_price):
    """그리드 주문 생성 (중복 방지 강화)"""
    global last_grid_time
    
    # ✅ Lock으로 동시 실행 방지
    if not initialize_grid_lock.acquire(blocking=False):
        log("⏸️ GRID", "Already running → Skipping")
        return
    
    try:
        # ✅ 디버깅 로그
        log("🔍 DEBUG", f"initialize_grid called at {current_price:.4f}")
        
        # ✅ 현재 그리드 상태 로그
        if SYMBOL in grid_orders:
            long_grids = len(grid_orders[SYMBOL].get("long", []))
            short_grids = len(grid_orders[SYMBOL].get("short", []))
            log("🔍 DEBUG", f"Current grids: Long={long_grids}, Short={short_grids}")
        
        now = time.time()
        
        # ✅ 시간 체크 강화 (10초 → 3초)
        if now - last_grid_time < 3:
            log("⏸️ GRID", f"Too soon ({now - last_grid_time:.1f}s) → Skipping")
            return
        
        last_grid_time = now
        
        sync_position()
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        # 양방향 포지션 체크
        if long_size > 0 and short_size > 0:
            log("ℹ️ GRID", "Both positions exist → Canceling grids")
            cancel_grid_only()  # ✅ 그리드 취소
            return
        
        # 단일 포지션 또는 포지션 없음
        if long_size == 0 and short_size == 0:
            log("🔄 GRID", "No position → Creating both side grids")
        else:
            log("🔄 GRID", f"Single position → Creating grids (Long: {long_size}, Short: {short_size})")
        
        # 최대 한도 체크
        with balance_lock:
            balance = account_balance
        
        max_value = Decimal(str(balance)) * MAX_POSITION_RATIO
        
        long_value = Decimal(str(long_size)) * Decimal(str(current_price))
        short_value = Decimal(str(short_size)) * Decimal(str(current_price))
        
        if long_value >= max_value or short_value >= max_value:
            log("🚫 GRID", f"Max position reached (Long: ${float(long_value):.2f}, Short: ${float(short_value):.2f}) → No grid")
            return
        
        # 임계값 확인
        threshold_value = Decimal(str(balance)) * THRESHOLD_RATIO
        
        above_threshold_long = long_value >= threshold_value
        above_threshold_short = short_value >= threshold_value
        
        # 주력 포지션 결정
        main_side = None
        main_side_quantity = Decimal("0")
        
        if above_threshold_long or above_threshold_short:
            with position_lock:
                if long_size > short_size:
                    main_side = "long"
                    main_side_quantity = position_state[SYMBOL]["long"]["size"]
                elif short_size > long_size:
                    main_side = "short"
                    main_side_quantity = position_state[SYMBOL]["short"]["size"]
            
            log("🚫 ASYMMETRIC", f"Above threshold | Main: {main_side.upper() if main_side else 'none'}")
            
            # 임계값 초과 시 수량 계산
            with balance_lock:
                base_qty = int(Decimal(str(balance)) * BASE_RATIO)
            
            if main_side == "long":
                # 주력이 롱
                long_qty_proportional = int(Decimal(str(main_side_quantity)) * POSITION_SCALE_RATIO)
                long_qty = max(base_qty, long_qty_proportional)
                
                short_qty = int(Decimal(str(main_side_quantity)) * COUNTER_RATIO)
                if short_qty < 1:
                    short_qty = 1
                
                log("📊 POSITION SCALE", f"Main qty: {long_qty} (base: {base_qty}, scale 20%: {long_qty_proportional})")
                log("📊 POSITION SCALE", f"Counter qty: {short_qty} (30% of {main_side_quantity})")
                
            elif main_side == "short":
                # 주력이 숏
                long_qty = int(Decimal(str(main_side_quantity)) * COUNTER_RATIO)
                if long_qty < 1:
                    long_qty = 1
                
                short_qty_proportional = int(Decimal(str(main_side_quantity)) * POSITION_SCALE_RATIO)
                short_qty = max(base_qty, short_qty_proportional)
                
                log("📊 POSITION SCALE", f"Counter qty: {long_qty} (30% of {main_side_quantity})")
                log("📊 POSITION SCALE", f"Main qty: {short_qty} (base: {base_qty}, scale 20%: {short_qty_proportional})")
            else:
                # 주력 없음
                long_qty = base_qty
                short_qty = base_qty
        else:
            # 임계값 이전: OBV MACD 기반 수량
            obv_macd_value = get_obv_macd_value()
            obv_weight = calculate_obv_macd_weight(float(obv_macd_value) * 1000)
            
            with balance_lock:
                base_size = int(Decimal(str(balance)) * BASE_RATIO)
            
            weighted_qty = int(Decimal(str(base_size)) * Decimal(str(obv_weight)))
            long_qty = max(1, weighted_qty)
            short_qty = max(1, weighted_qty)
            
            log("🔄 SYMMETRIC", f"Below threshold | Weight: {int(obv_weight*100)}%")
            log("📊 QUANTITY", f"Both sides: {long_qty} (OBV:{obv_macd_value:.1f}, x{obv_weight:.2f})")
        
        # 그리드 가격 계산
        gap = GRID_GAP_PCT
        
        long_price = current_price * (Decimal("1") - gap)
        short_price = current_price * (Decimal("1") + gap)
        
        # 정밀도 조정
        long_price = adjust_price_precision(long_price)
        short_price = adjust_price_precision(short_price)
        
        log("🔄 GRID INIT", f"Price: {current_price:.4f}")
        log("🔄 OBV MACD", f"Value: {get_obv_macd_value():.2f}")
        
        # 포지션 잠금 확인
        long_locked = long_size > 0
        short_locked = short_size > 0
        
        # 그리드 주문 생성
        grid_orders[SYMBOL] = {"long": [], "short": []}
        
        created_long = False
        created_short = False
        
        # 롱 그리드
        if not long_locked:
            try:
                order = FuturesOrder(
                    contract=SYMBOL,
                    size=long_qty,
                    price=str(long_price),
                    tif="gtc"
                )
                result = api.create_futures_order(SETTLE, order)
                
                grid_orders[SYMBOL]["long"].append({
                    "id": result.id,
                    "price": long_price,
                    "size": long_qty
                })
                
                log("🚫 GRID", f"Same LONG {long_qty} @ {float(long_price):.4f}")
                created_long = True
                
            except GateApiException as e:
                log("❌", f"LONG grid order error: {e}")
        
        # 숏 그리드
        if not short_locked:
            try:
                order = FuturesOrder(
                    contract=SYMBOL,
                    size=-short_qty,
                    price=str(short_price),
                    tif="gtc"
                )
                result = api.create_futures_order(SETTLE, order)
                
                grid_orders[SYMBOL]["short"].append({
                    "id": result.id,
                    "price": short_price,
                    "size": short_qty
                })
                
                log("🚫 GRID", f"Same SHORT {short_qty} @ {float(short_price):.4f}")
                created_short = True
                
            except GateApiException as e:
                log("❌", f"SHORT grid order error: {e}")
        
        if created_long or created_short:
            log("✅ GRID", f"Grid created (Long: {created_long}, Short: {created_short})")
        else:
            log("⚪ GRID", "No grids created (positions locked or errors)")
            
    finally:
        initialize_grid_lock.release()

def hedge_after_grid_fill(side, grid_price, grid_qty, was_counter, base_qty):
    """그리드 체결 후 헤징 + 후속 처리 (포지션 동기화 개선)"""
    if not ENABLE_AUTO_HEDGE:
        return
    
    try:
        # 1. 모든 주문 취소
        cancel_all_orders()
        time.sleep(0.5)
        
        # 2. 포지션 동기화
        sync_position()
        time.sleep(0.3)
        
        current_price = get_current_price()
        if current_price <= 0:
            return
        
        counter_side = get_counter_side(side)
        with position_lock:
            main_size = position_state[SYMBOL][side]["size"]
            counter_size = position_state[SYMBOL][counter_side]["size"]
        
        obv_display = float(obv_macd_value) * 1000
        
        # ✅ main_side가 "none"인지 체크!
        main_side = get_main_side()
        if main_side != "none" and is_above_threshold(main_side) and side == main_side:
            post_threshold_entries[SYMBOL][side].append({
                "qty": int(grid_qty),
                "price": float(grid_price),
                "entry_type": "grid",
                "tp_order_id": None
            })
            log("📝 TRACKED", f"{side.upper()} grid {grid_qty} @ {grid_price:.4f} (MAIN, above threshold)")
        
        # 3. 헤징 수량 계산
        if was_counter:
            hedge_qty = max(base_qty, int(main_size * 0.1))
            hedge_side = side
            log("🔄 HEDGE", f"Counter grid filled → Main hedge: {hedge_side.upper()} {hedge_qty} (OBV:{obv_display:.1f})")
        else:
            hedge_qty = base_qty
            hedge_side = counter_side
            log("🔄 HEDGE", f"Main grid filled → Counter hedge: {hedge_side.upper()} {hedge_qty} (base={base_qty})")
        
        # 4. 헤징 주문 실행
        hedge_order_data = {
            "contract": SYMBOL,
            "size": int(hedge_qty * (1 if hedge_side == "long" else -1)),
            "price": "0",
            "tif": "ioc",
            "close": False
        }
        
        try:
            order = api.create_futures_order(SETTLE, FuturesOrder(**hedge_order_data))
            
            if order and hasattr(order, 'id'):
                order_id = order.id
                log("✅ HEDGE", f"{hedge_side.upper()} {hedge_qty} @ market (ID: {order_id})")
            else:
                log("✅ HEDGE", f"{hedge_side.upper()} {hedge_qty} @ market (IOC filled immediately)")
        except GateApiException as e:
            log("❌", f"Hedge order API error: {e}")
            return
        
        # ✅ 5. 헤징 후 포지션 재동기화 (중요!)
        time.sleep(0.5)  # Gate.io API 반영 대기
        sync_position()  # 재동기화
        time.sleep(0.3)
        
        # ✅ 6. 헤징 후 main_side 재확인 및 추적
        main_side_after = get_main_side()
        if main_side_after != "none" and is_above_threshold(main_side_after) and hedge_side == main_side_after:
            with position_lock:
                entry_price = position_state[SYMBOL][hedge_side]["price"]
            
            post_threshold_entries[SYMBOL][hedge_side].append({
                "qty": int(hedge_qty),
                "price": float(entry_price),
                "entry_type": "hedge",
                "tp_order_id": None
            })
            log("📝 TRACKED", f"{hedge_side.upper()} hedge {hedge_qty} @ {entry_price:.4f} (MAIN, above threshold)")
        
        # 7. 그리드 취소
        cancel_grid_only()
        time.sleep(0.3)
        
        # 8. TP 생성
        refresh_all_tp_orders()
        
        # 9. 포지션 재확인 후 그리드 재생성
        time.sleep(0.3)
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
        
        log("🔍 DEBUG", f"After hedging: long={long_size}, short={short_size}")
        
        # ✅ 10. 그리드 재생성 (롱/숏 하나만 있을 때)
        if long_size == 0 or short_size == 0:
            log("📊 GRID", "Single position after hedge → Creating grid")
            current_price = get_current_price()
            if current_price > 0:
                global last_grid_time
                last_grid_time = 0
                time.sleep(0.3)
                initialize_grid(current_price)
        else:
            log("ℹ️ GRID", "Both positions exist → No grid creation")
        
    except Exception as e:
        log("❌", f"Hedge order error: {e}")
        import traceback
        log("❌", f"Traceback: {traceback.format_exc()}")

def refresh_all_tp_orders():
    """TP 주문 새로 생성 (즉시 체결 감지 + 중복 방지 + 디버깅 강화)"""
    cancel_tp_only()
    
    try:
        average_tp_orders[SYMBOL] = {"long": None, "short": None}
        instant_tp_triggered = False
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
            long_price = position_state[SYMBOL]["long"]["price"]
            short_price = position_state[SYMBOL]["short"]["price"]
        
        log("🎯 TP REFRESH", "Creating TP orders...")
        log_threshold_info()
        
        # ✅ 추가: post_threshold_entries 디버깅
        log("🔍 DEBUG", f"post_threshold_entries LONG: {post_threshold_entries[SYMBOL]['long']}")
        log("🔍 DEBUG", f"post_threshold_entries SHORT: {post_threshold_entries[SYMBOL]['short']}")
        
        main_side = get_main_side()
        log("🔍 DEBUG", f"main_side={main_side}, long={long_size}, short={short_size}")
        
        # ========================================================================
        # === 롱 TP 생성 ===
        # ========================================================================
        if long_size > 0:
            long_above = is_above_threshold("long")
            log("🔍 DEBUG", f"LONG: above={long_above}, is_main={main_side == 'long'}")
            
            try:
                if long_above and main_side == "long":
                    # ===== Individual TP 로직 =====
                    log("📈 LONG TP", "Above threshold & MAIN → Individual + Average TPs")
                    
                    individual_total = 0
                    for entry in post_threshold_entries[SYMBOL]["long"]:
                        tp_price = Decimal(str(entry["price"])) * (Decimal("1") + TP_GAP_PCT)
                        tp_price = tp_price.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                        
                        order = FuturesOrder(
                            contract=SYMBOL,
                            size=-entry["qty"],
                            price=str(tp_price),
                            tif="gtc",
                            reduce_only=True
                        )
                        
                        log("🔍 DEBUG", f"Creating LONG individual TP: qty={entry['qty']}, price={tp_price}")
                        result = api.create_futures_order(SETTLE, order)
                        log("🔍 DEBUG", f"LONG individual TP result: {result}")
                        
                        # ✅ 즉시 체결 확인
                        if result and hasattr(result, 'id'):
                            if hasattr(result, 'status') and result.status in ["finished", "closed"]:
                                log("⚡ INSTANT TP", f"LONG individual TP filled immediately @ {tp_price:.4f}")
                                instant_tp_triggered = True  # ✅ 플래그 설정
                            else:
                                entry["tp_order_id"] = result.id
                                individual_total += entry["qty"]
                                log("🎯 INDIVIDUAL TP", f"LONG {entry['qty']} @ {tp_price:.4f}")
                        else:
                            log("❌ TP", f"LONG individual TP creation failed: result={result}")
                    
                    # ===== Average TP =====
                    remaining = int(long_size - individual_total)
                    if remaining > 0:
                        tp_price = long_price * (Decimal("1") + TP_GAP_PCT)
                        tp_price = tp_price.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                        
                        order = FuturesOrder(
                            contract=SYMBOL,
                            size=-remaining,
                            price=str(tp_price),
                            tif="gtc",
                            reduce_only=True
                        )
                        
                        log("🔍 DEBUG", f"Creating LONG average TP: size={remaining}, price={tp_price}")
                        result = api.create_futures_order(SETTLE, order)
                        log("🔍 DEBUG", f"LONG average TP result: {result}")
                        
                        # ✅ 즉시 체결 확인
                        if result and hasattr(result, 'id'):
                            if hasattr(result, 'status') and result.status in ["finished", "closed"]:
                                log("⚡ INSTANT TP", f"LONG average TP filled immediately @ {tp_price:.4f}")
                                instant_tp_triggered = True  # ✅ 플래그 설정
                            else:
                                average_tp_orders[SYMBOL]["long"] = result.id
                                log("🎯 AVERAGE TP", f"LONG {remaining} @ {tp_price:.4f}")
                        else:
                            log("❌ TP", f"LONG average TP creation failed: result={result}")
                
                else:
                    # ===== Full average TP =====
                    log("📈 LONG TP", "Below threshold or COUNTER → Full average TP")
                    tp_price = long_price * (Decimal("1") + TP_GAP_PCT)
                    tp_price = tp_price.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                    
                    order = FuturesOrder(
                        contract=SYMBOL,
                        size=-int(long_size),
                        price=str(tp_price),
                        tif="gtc",
                        reduce_only=True
                    )
                    
                    log("🔍 DEBUG", f"Creating LONG full TP: size={int(long_size)}, price={tp_price}")
                    result = api.create_futures_order(SETTLE, order)
                    log("🔍 DEBUG", f"LONG full TP result: {result}")
                    
                    # ✅ 즉시 체결 확인
                    if result and hasattr(result, 'id'):
                        if hasattr(result, 'status') and result.status in ["finished", "closed"]:
                            log("⚡ INSTANT TP", f"LONG full TP filled immediately @ {tp_price:.4f}")
                            instant_tp_triggered = True  # ✅ 플래그 설정
                            # 즉시 새로고침 트리거
                            threading.Thread(
                                target=full_refresh,
                                args=("Instant_TP_Long",),
                                daemon=True
                            ).start()
                        else:
                            average_tp_orders[SYMBOL]["long"] = result.id
                            log("🎯 FULL TP", f"LONG {int(long_size)} @ {tp_price:.4f}")
                    else:
                        log("❌ TP", f"LONG full TP creation failed: result={result}")
            
            except Exception as e:
                log("❌ TP", f"LONG TP exception: {e}")
                import traceback
                log("❌", traceback.format_exc())
        
        # ========================================================================
        # === 숏 TP 생성 ===
        # ========================================================================
        if short_size > 0:
            short_above = is_above_threshold("short")
            log("🔍 DEBUG", f"SHORT: above={short_above}, is_main={main_side == 'short'}")
            
            try:
                if short_above and main_side == "short":
                    # ===== Individual TP 로직 =====
                    log("📉 SHORT TP", "Above threshold & MAIN → Individual + Average TPs")
                    
                    individual_total = 0
                    for entry in post_threshold_entries[SYMBOL]["short"]:
                        tp_price = Decimal(str(entry["price"])) * (Decimal("1") - TP_GAP_PCT)
                        tp_price = tp_price.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                        
                        order = FuturesOrder(
                            contract=SYMBOL,
                            size=entry["qty"],
                            price=str(tp_price),
                            tif="gtc",
                            reduce_only=True
                        )
                        
                        log("🔍 DEBUG", f"Creating SHORT individual TP: qty={entry['qty']}, price={tp_price}")
                        result = api.create_futures_order(SETTLE, order)
                        log("🔍 DEBUG", f"SHORT individual TP result: {result}")
                        
                        # ✅ 즉시 체결 확인
                        if result and hasattr(result, 'id'):
                            if hasattr(result, 'status') and result.status in ["finished", "closed"]:
                                log("⚡ INSTANT TP", f"SHORT individual TP filled immediately @ {tp_price:.4f}")
                                instant_tp_triggered = True  # ✅ 플래그 설정
                            else:
                                entry["tp_order_id"] = result.id
                                individual_total += entry["qty"]
                                log("🎯 INDIVIDUAL TP", f"SHORT {entry['qty']} @ {tp_price:.4f}")
                        else:
                            log("❌ TP", f"SHORT individual TP creation failed: result={result}")
                    
                    # ===== Average TP =====
                    remaining = int(short_size - individual_total)
                    if remaining > 0:
                        tp_price = short_price * (Decimal("1") - TP_GAP_PCT)
                        tp_price = tp_price.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                        
                        order = FuturesOrder(
                            contract=SYMBOL,
                            size=remaining,
                            price=str(tp_price),
                            tif="gtc",
                            reduce_only=True
                        )
                        
                        log("🔍 DEBUG", f"Creating SHORT average TP: size={remaining}, price={tp_price}")
                        result = api.create_futures_order(SETTLE, order)
                        log("🔍 DEBUG", f"SHORT average TP result: {result}")
                        
                        # ✅ 즉시 체결 확인
                        if result and hasattr(result, 'id'):
                            if hasattr(result, 'status') and result.status in ["finished", "closed"]:
                                log("⚡ INSTANT TP", f"SHORT average TP filled immediately @ {tp_price:.4f}")
                                instant_tp_triggered = True  # ✅ 플래그 설정
                            else:
                                average_tp_orders[SYMBOL]["short"] = result.id
                                log("🎯 AVERAGE TP", f"SHORT {remaining} @ {tp_price:.4f}")
                        else:
                            log("❌ TP", f"SHORT average TP creation failed: result={result}")
                
                else:
                    # ===== Full average TP =====
                    log("📉 SHORT TP", "Below threshold or COUNTER → Full average TP")
                    tp_price = short_price * (Decimal("1") - TP_GAP_PCT)
                    tp_price = tp_price.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                    
                    order = FuturesOrder(
                        contract=SYMBOL,
                        size=int(short_size),
                        price=str(tp_price),
                        tif="gtc",
                        reduce_only=True
                    )
                    
                    log("🔍 DEBUG", f"Creating SHORT full TP: size={int(short_size)}, price={tp_price}")
                    result = api.create_futures_order(SETTLE, order)
                    log("🔍 DEBUG", f"SHORT full TP result: {result}")
                    
                    # ✅ 즉시 체결 확인
                    if result and hasattr(result, 'id'):
                        if hasattr(result, 'status') and result.status in ["finished", "closed"]:
                            log("⚡ INSTANT TP", f"SHORT full TP filled immediately @ {tp_price:.4f}")
                            instant_tp_triggered = True  # ✅ 플래그 설정
                            # 즉시 새로고침 트리거
                            threading.Thread(
                                target=full_refresh,
                                args=("Instant_TP_Short",),
                                daemon=True
                            ).start()
                        else:
                            average_tp_orders[SYMBOL]["short"] = result.id
                            log("🎯 FULL TP", f"SHORT {int(short_size)} @ {tp_price:.4f}")
                    else:
                        log("❌ TP", f"SHORT full TP creation failed: result={result}")
            
            except Exception as e:
                log("❌ TP", f"SHORT TP exception: {e}")
                import traceback
                log("❌", traceback.format_exc())
        
        # ========================================================================
        # === TP 생성 후 포지션 재확인 (중복 방지) ===
        # ========================================================================
        time.sleep(0.5)
        sync_position()
        
        with position_lock:
            long_size_after = position_state[SYMBOL]["long"]["size"]
            short_size_after = position_state[SYMBOL]["short"]["size"]
        
        # ✅ instant_tp_triggered가 False일 때만 실행 (중복 방지)
        if not instant_tp_triggered:
            # TP가 즉시 체결되어 포지션이 사라진 경우
            if (long_size > 0 and long_size_after == 0) or (short_size > 0 and short_size_after == 0):
                log("⚡ INSTANT TP", "Position closed after TP creation → Triggering refresh")
                threading.Thread(
                    target=full_refresh,
                    args=("Instant_TP_Detected",),
                    daemon=True
                ).start()
        else:
            log("ℹ️ TP", "Instant TP already triggered, skipping duplicate refresh")
    
    except Exception as e:
        log("❌", f"TP refresh error: {e}")
        import traceback
        log("❌", f"Traceback: {traceback.format_exc()}")

def on_individual_tp_filled(main_side, filled_order_id):
    """개별 TP 체결 시 비주력 포지션 20% 청산"""
    counter_side = get_counter_side(main_side)
    
    with position_lock:
        counter_size = position_state[SYMBOL][counter_side]["size"]
    
    if counter_size <= 0:
        log("ℹ️", f"No {counter_side} position to partially close")
        return
    
    # 비주력 포지션 20% 청산
    close_qty = max(1, int(counter_size * Decimal("0.2")))
    
    log("✂️ PARTIAL", f"Individual TP filled → Close {counter_side.upper()} {close_qty} (20%)")
    
    close_order_data = {
        "contract": SYMBOL,
        "size": int(close_qty * (1 if counter_side == "long" else -1)),
        "price": "0",
        "tif": "ioc",
        "close": True
    }
    
    try:
        order = api.create_futures_order(SETTLE, FuturesOrder(**close_order_data))
        if order and hasattr(order, 'id'):
            log("✅ CLOSED", f"{counter_side.upper()} {close_qty} @ market (ID: {order.id})")
        else:
            log("✅ CLOSED", f"{counter_side.upper()} {close_qty} @ market (IOC filled)")
        
        # 포지션 동기화
        time.sleep(0.5)
        sync_position()
        
        # 남은 물량에 대해 TP 재생성
        time.sleep(0.3)
        refresh_all_tp_orders()
        
    except GateApiException as e:
        log("❌", f"Partial close API error: {e}")
    except Exception as e:
        log("❌", f"Partial close error: {e}")

def check_idle_and_enter():
    """30분 무이벤트 진입"""
    global last_event_time, idle_entry_count
    
    try:
        # 30분 체크
        if time.time() - last_event_time < IDLE_TIMEOUT:
            return
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
            long_price = position_state[SYMBOL]["long"]["price"]
            short_price = position_state[SYMBOL]["short"]["price"]
        
        # 롱/숏 모두 없으면 진입 안 함
        if long_size == 0 or short_size == 0:
            return
        
        # 주력/비주력 판단
        main_side = get_main_side()
        if main_side == "none":
            log("⚠️ IDLE", "No main position - skipping idle entry")
            return
        
        counter_side = get_counter_side(main_side)
        
        # 최대 한도 체크
        with balance_lock:
            max_value = account_balance * MAX_POSITION_RATIO
            base_qty = int(Decimal(str(account_balance)) * BASE_RATIO)
            if base_qty <= 0:
                base_qty = 1
        
        main_value = (long_price * long_size) if main_side == "long" else (short_price * short_size)
        counter_value = (short_price * short_size) if main_side == "long" else (long_price * long_size)
        
        if main_value >= max_value or counter_value >= max_value:
            log("⚠️ IDLE", f"Max position reached")
            return
        
        # OBV MACD 값
        obv_display = float(obv_macd_value) * 1000
        
        # ✅ 수정: 기존 함수 호출!
        obv_multiplier = calculate_obv_macd_weight(obv_display)
        
        # 진입 카운트 증가
        idle_entry_count += 1
        multiplier = idle_entry_count
        
        # Main 수량 (OBV MACD 적용)
        main_qty = max(1, int(base_qty * obv_multiplier * multiplier))
        
        # ✅ 수정: Counter 수량 (최소 1개 보장!)
        counter_qty = max(1, int(base_qty * multiplier))
        
        log_event_header("IDLE ENTRY")
        log("⏱️ IDLE", f"Entry #{idle_entry_count} (x{multiplier}) → BOTH sides")
        log("📊 IDLE QTY", f"Main {main_side.upper()} {main_qty} (OBV:{obv_display:.1f}, x{multiplier}) | Counter {counter_side.upper()} {counter_qty} (base, x{multiplier})")
        
        # Main 진입
        main_order_data = {
            "contract": SYMBOL,
            "size": int(main_qty * (1 if main_side == "long" else -1)),
            "price": "0",
            "tif": "ioc",
            "close": False  # ✅ 추가
        }
        
        try:
            main_order = api.create_futures_order(SETTLE, FuturesOrder(**main_order_data))
            if main_order and hasattr(main_order, 'id'):
                log("✅ IDLE ENTRY", f"Main {main_side.upper()} {main_qty} @ market (x{multiplier})")
            else:
                log("❌ IDLE", f"Main entry failed: result={main_order}")
        except GateApiException as e:
            log("❌", f"Idle entry API error (Main): {e}")
            return
        
        time.sleep(0.2)
        
        # Counter 진입
        counter_order_data = {
            "contract": SYMBOL,
            "size": int(counter_qty * (1 if counter_side == "long" else -1)),
            "price": "0",
            "tif": "ioc",
            "close": False  # ✅ 추가
        }
        
        try:
            counter_order = api.create_futures_order(SETTLE, FuturesOrder(**counter_order_data))
            if counter_order and hasattr(counter_order, 'id'):
                log("✅ IDLE ENTRY", f"Counter {counter_side.upper()} {counter_qty} @ market (x{multiplier})")
            else:
                log("❌ IDLE", f"Counter entry failed: result={counter_order}")
        except GateApiException as e:
            log("❌", f"Idle entry API error (Counter): {e}")
        
        time.sleep(0.5)
        sync_position()
        
        # 임계값 초과 시 진입 추적
        if is_above_threshold(main_side):
            with position_lock:
                main_entry_price = position_state[SYMBOL][main_side]["price"]
            
            post_threshold_entries[SYMBOL][main_side].append({
                "qty": int(main_qty),
                "price": float(main_entry_price),
                "entry_type": "idle",
                "tp_order_id": None
            })
            log("📝 TRACKED", f"{main_side.upper()} idle {main_qty} @ {main_entry_price:.4f} (MAIN, above threshold)")
        
        # 타이머 리셋 (배수는 유지)
        last_event_time = time.time()
        
        # TP 재생성
        refresh_all_tp_orders()
        
    except Exception as e:
        log("❌", f"Idle entry error: {e}")
        import traceback
        log("❌", f"Traceback: {traceback.format_exc()}")

def close_counter_on_individual_tp(main_side):
    """개별 TP 체결 시 비주력 20% 청산"""
    try:
        counter_side = get_counter_side(main_side)
        
        with position_lock:
            counter_size = position_state[SYMBOL][counter_side]["size"]
        
        if counter_size <= 0:
            log("ℹ️ COUNTER", "No counter position to close")
            return
        
        # 스냅샷 확인
        snapshot = counter_position_snapshot[SYMBOL][main_side]
        if snapshot == Decimal("0"):
            # 첫 개별 TP 체결: 스냅샷 저장
            snapshot = counter_size
            counter_position_snapshot[SYMBOL][main_side] = snapshot
            log("📸 SNAPSHOT", f"{counter_side.upper()} snapshot = {snapshot}")
        
        # 스냅샷 기준 20% 청산
        close_qty = max(1, int(snapshot * COUNTER_CLOSE_RATIO))
        if close_qty > counter_size:
            close_qty = int(counter_size)
        
        size = -close_qty if counter_side == "long" else close_qty
        
        log("🔄 COUNTER CLOSE", f"{counter_side.upper()} {close_qty} @ market (snapshot: {snapshot}, 20%)")
        order = FuturesOrder(contract=SYMBOL, size=size, price="0", tif='ioc', reduce_only=True)
        api.create_futures_order(SETTLE, order)
        
    except Exception as e:
        log("❌", f"Counter close error: {e}")


# =============================================================================
# 상태 추적
# =============================================================================
def track_entry(side, qty, price, entry_type, tp_id=None):
    """임계값 초과 후 진입 추적"""
    if not is_above_threshold(side):
        return
    
    entry_data = {
        "qty": int(qty),
        "price": float(price),
        "entry_type": entry_type,
        "tp_order_id": tp_id
    }
    post_threshold_entries[SYMBOL][side].append(entry_data)
    log("📝 TRACKED", f"{side.upper()} {qty} @ {price:.4f} ({entry_type}, tp_id={tp_id})")

# =============================================================================
# 시스템 새로고침
# =============================================================================
def full_refresh(event_type, skip_grid=False):
    """
    시스템 새로고침
    skip_grid=True: TP만 생성하고 그리드는 skip (TP 체결 시 사용)
    """
    log_event_header(f"FULL REFRESH: {event_type}")
    
    log("🔄 SYNC", "Syncing position...")
    sync_position()
    log_position_state()
    log_threshold_info()

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

# =============================================================================
# 모니터링 스레드
# =============================================================================
def place_hedge_order(side):
    if not ENABLE_AUTO_HEDGE:
        return None
    
    try:
        current_price = get_current_price()
        if current_price <= 0:
            return None
        
        counter_side = get_counter_side(side)
        with position_lock:
            main_size = position_state[SYMBOL][side]["size"]
            counter_size = position_state[SYMBOL][counter_side]["size"]
        
        # 비주력 포지션이 체결된 경우, 주력 포지션은 기본수량 또는 10% 중 큰 값으로 헷징
        if counter_size > 0 and main_size > 0:
            with balance_lock:
                base_size = int(Decimal(str(account_balance)) * BASE_RATIO)
            hedge_size = max(base_size, int(main_size * 0.1))
            size = hedge_size
        else:
            with balance_lock:
                base_size = int(Decimal(str(account_balance)) * BASE_RATIO)
            size = base_size
        
        hedge_order_data = {
            "contract": SYMBOL,
            "size": int(size * (1 if side == "long" else -1)),
            "price": "0",  # 시장가는 "0"
            "tif": "ioc"
        }
        
        order = api.create_futures_order(SETTLE, FuturesOrder(**hedge_order_data))
        order_id = order.id
        
        log_event_header("AUTO HEDGE")
        log("✅ HEDGE", f"{side.upper()} {size} @ market")
        
        # 주력 포지션은 개별 TP 주문 설정
        tp_id = create_individual_tp(side, size, current_price)
        if tp_id:
            with position_lock:
                post_threshold_entries[SYMBOL][side].append({
                    "price": float(current_price),
                    "qty": int(size),
                    "tp_order_id": tp_id,
                    "entry_type": "hedge"
                })
        
        full_refresh("Hedge")
        return order_id
        
    except GateApiException as e:
        log("❌", f"Hedge submission error: {e}")
        return None
    except Exception as e:
        log("❌", f"Hedge order error: {e}")
        return None

async def grid_fill_monitor():
    """WebSocket으로 그리드 체결 및 TP 체결 모니터링 (체결 감지 강화)"""
    global last_grid_time, idle_entry_count
    
    uri = f"wss://fx-ws.gateio.ws/v4/ws/{SETTLE}"
    ping_count = 0
    
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
                log("✅ WS", "Connected to WebSocket (attempt 1)")
                
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

                                # ✅ 추가: 모든 주문 이벤트 로그 (디버깅용)
                                log("🔍 WS RAW", f"id={order_data.get('id')}, status={order_data.get('status')}, finish_as={order_data.get('finish_as')}, size={order_data.get('size')}")
                                
                                # ✅ 수정: finish_as 체크 강화
                                finish_as = order_data.get("finish_as", "")
                                status = order_data.get("status", "")
                                
                                # ✅ 체결 조건: finish_as가 "filled", "ioc", "cancelled" 등이 아니고
                                # status가 "finished"인 경우도 포함
                                is_filled = (
                                    finish_as in ["filled", "ioc"] or 
                                    status in ["finished", "closed"]
                                )
                                
                                if not is_filled:
                                    continue
                                
                                # ✅ 추가: 체결 확인 로그
                                log("🔍 DEBUG", f"Order filled detected: id={order_data.get('id')}, finish_as={finish_as}, status={status}")
                                
                                is_reduce_only = order_data.get("is_reduce_only", False)
                                order_id = order_data.get("id")
                                size = order_data.get("size", 0)
                                price = float(order_data.get("price", 0))
                                
                                # TP 체결 시
                                if is_reduce_only:
                                    side = "long" if size < 0 else "short"
                                    log("🎯 TP FILLED", f"{side.upper()} @ {price:.4f}")
                                    
                                    update_event_time()
                                    
                                    threading.Thread(
                                        target=on_individual_tp_filled, 
                                        args=(side, order_id), 
                                        daemon=True
                                    ).start()
                                    
                                    time.sleep(0.5)
                                    
                                    with position_lock:
                                        long_size = position_state[SYMBOL]["long"]["size"]
                                        short_size = position_state[SYMBOL]["short"]["size"]
                                    
                                    if long_size == 0 and short_size == 0:
                                        log("🎯 AVG TP", "Both sides closed → Full refresh")
                                        update_event_time()
                                        
                                        threading.Thread(
                                            target=full_refresh, 
                                            args=("Average_TP",), 
                                            daemon=True
                                        ).start()
                                
                                # 그리드 체결 시
                                elif not is_reduce_only:
                                    side = "long" if size > 0 else "short"
                                    log("🔥 GRID FILLED", f"{side.upper()} @ {price:.4f}")
                                    
                                    update_event_time()
                                    
                                    try:
                                        grid_info = None
                                        if SYMBOL in grid_orders and side in grid_orders[SYMBOL]:
                                            for grid in grid_orders[SYMBOL][side]:
                                                if grid.get("order_id") == order_id:
                                                    grid_info = grid
                                                    break
                                        
                                        if grid_info:
                                            grid_price = grid_info.get("price", price)
                                            grid_qty = grid_info.get("qty", abs(size))
                                            was_counter = grid_info.get("is_counter", False)
                                            base_qty = grid_info.get("base_qty", 1)
                                            
                                            log("🔍 DEBUG", f"Grid info found: price={grid_price}, qty={grid_qty}, counter={was_counter}")
                                            
                                            threading.Thread(
                                                target=hedge_after_grid_fill, 
                                                args=(side, grid_price, grid_qty, was_counter, base_qty), 
                                                daemon=True
                                            ).start()
                                        else:
                                            log("⚠️ GRID", "Grid info not found, using defaults")
                                            with balance_lock:
                                                base_qty = int(Decimal(str(account_balance)) * BASE_RATIO)
                                                if base_qty <= 0:
                                                    base_qty = 1
                                            
                                            threading.Thread(
                                                target=hedge_after_grid_fill, 
                                                args=(side, price, abs(size), False, base_qty), 
                                                daemon=True
                                            ).start()
                                    
                                    except Exception as e:
                                        log("❌", f"Grid fill processing error: {e}")
                                        import traceback
                                        log("❌", traceback.format_exc())
                    
                    except asyncio.TimeoutError:
                        ping_count += 1
                        if ping_count % 40 == 1:
                            log("⚠️ WS", f"No order update for {ping_count * 150}s")
                        continue
        
        except Exception as e:
            log("❌", f"WebSocket error: {e}")
            log("⚠️ WS", "Reconnecting in 5s...")
            await asyncio.sleep(5)

def tp_monitor():
    """TP 체결 모니터링 (개별 TP + 평단 TP)"""
    while True:
        try:
            time.sleep(3)
            
            # ===== 개별 TP 체결 확인 =====
            for side in ["long", "short"]:
                for entry in list(post_threshold_entries[SYMBOL][side]):
                    try:
                        tp_id = entry.get("tp_order_id")
                        if not tp_id:
                            continue
                        
                        order = api.get_futures_order(SETTLE, str(tp_id))
                        if not order:
                            continue
                        
                        if hasattr(order, 'status') and order.status in ["finished", "closed"]:
                            log_event_header("INDIVIDUAL TP HIT")
                            log("🎯 TP", f"{side.upper()} {entry['qty']} closed @ {entry['price']:.4f}")
                            
                            # 추적 리스트에서 제거
                            post_threshold_entries[SYMBOL][side].remove(entry)

                            update_event_time()  # 이벤트 시간 갱신
                            
                            # ===== 비주력 20% 시장가 청산 =====
                            counter_side = get_counter_side(side)
                            
                            with position_lock:
                                counter_size = position_state[SYMBOL][counter_side]["size"]
                            
                            if counter_size > 0:
                                # 20% 청산
                                close_qty = max(1, int(counter_size * COUNTER_CLOSE_RATIO))
                                close_size = -close_qty if counter_side == "long" else close_qty
                                
                                log("🔄 COUNTER CLOSE", f"{counter_side.upper()} {close_qty} @ market (20% of {counter_size})")
                                
                                close_order = FuturesOrder(
                                    contract=SYMBOL,
                                    size=close_size,
                                    price="0",
                                    tif="ioc",
                                    reduce_only=True
                                )
                                api.create_futures_order(SETTLE, close_order)
                                
                                time.sleep(0.5)
                                sync_position()
                            
                            # 시스템 새로고침
                            time.sleep(0.5)
                            full_refresh("Individual_TP")
                            break
                    except:
                        pass
            
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
                        
                        # TP만 생성 (그리드는 skip)
                        full_refresh("Average_TP", skip_grid=True)

                        update_event_time()  # 이벤트 시간 갱신
                        
                        # 그리드 재생성
                        time.sleep(0.5)
                        current_price = get_current_price()
                        if current_price > 0:
                            # last_grid_time 초기화하여 강제 실행
                            global last_grid_time
                            last_grid_time = 0
                            initialize_grid(current_price)
                        
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
                long_price = position_state[SYMBOL]["long"]["price"]
                short_price = position_state[SYMBOL]["short"]["price"]
            
            # 포지션 변경 감지
            if long_size != prev_long_size or short_size != prev_short_size:
                if prev_long_size != Decimal("-1"):
                    log("🔄 CHANGE", f"Long {prev_long_size}→{long_size} | Short {prev_short_size}→{short_size}")
                prev_long_size = long_size
                prev_short_size = short_size
            
            with balance_lock:
                balance = account_balance  # ← INITIAL_BALANCE → account_balance로 수정
            
            threshold = balance * THRESHOLD_RATIO  # account_balance 기준
            max_value = balance * MAX_POSITION_RATIO  # account_balance 기준
            long_value = long_price * long_size
            short_value = short_price * short_size
            
            # 최대 보유 한도 체크
            if long_value >= max_value and not max_position_locked["long"]:
                log_event_header("MAX POSITION LIMIT")
                log("⚠️ LIMIT", f"LONG ${long_value:.2f} >= ${max_value:.2f}")
                max_position_locked["long"] = True
                cancel_grid_only()
            
            if short_value >= max_value and not max_position_locked["short"]:
                log_event_header("MAX POSITION LIMIT")
                log("⚠️ LIMIT", f"SHORT ${short_value:.2f} >= ${max_value:.2f}")
                max_position_locked["short"] = True
                cancel_grid_only()
            
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
            
            # 임계값 이하 복귀 시 초기화
            if long_value < threshold:
                if counter_position_snapshot[SYMBOL]["long"] != Decimal("0") or len(post_threshold_entries[SYMBOL]["long"]) > 0:
                    log("🔄 RESET", f"Long ${long_value:.2f} < threshold ${threshold:.2f}")
                    counter_position_snapshot[SYMBOL]["long"] = Decimal("0")
                    post_threshold_entries[SYMBOL]["long"].clear()
                    log("✅ CLEARED", "Long tracking data reset")
            
            if short_value < threshold:
                if counter_position_snapshot[SYMBOL]["short"] != Decimal("0") or len(post_threshold_entries[SYMBOL]["short"]) > 0:
                    log("🔄 RESET", f"Short ${short_value:.2f} < threshold ${threshold:.2f}")
                    counter_position_snapshot[SYMBOL]["short"] = Decimal("0")
                    post_threshold_entries[SYMBOL]["short"].clear()
                    log("✅ CLEARED", "Short tracking data reset")

        except Exception as e:
            log("❌", f"Position monitor error: {e}")
            time.sleep(5)

def idle_monitor():
    """30분 무이벤트 모니터링"""
    while True:
        try:
            time.sleep(60)  # 1분마다 체크
            check_idle_and_enter()
        except Exception as e:
            log("❌", f"Idle monitor error: {e}")
            time.sleep(10)

def periodic_health_check():
    """30초마다 종합 헬스체크 (강화)"""
    while True:
        try:
            time.sleep(30)
            log("🔍 HEALTH", "Starting comprehensive health check...")
            
            # 1. 기존: 포지션 동기화
            sync_position()
            
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
            
            if long_size == 0 and short_size == 0:
                log("🔍 HEALTH", "No position")
                continue
            
            # 2. 기존: 주문 상태 확인
            try:
                # ✅ 수정: status 파라미터 제거
                orders = api.list_futures_orders(SETTLE, SYMBOL)
                orders = [o for o in orders if o.status == 'open']
                
                grid_count = sum(1 for o in orders if not o.reduce_only)
                tp_count = sum(1 for o in orders if o.reduce_only)
                
                log("🔍 ORDERS", f"Grid: {grid_count}, TP: {tp_count}")
                
            except Exception as e:
                log("❌", f"List orders error: {e}")
                continue
            
            # 3. 기존: TP 확인 및 보완
            if long_size > 0 or short_size > 0:
                tp_orders_list = [o for o in orders if o.reduce_only]
                
                tp_long_qty = sum(abs(o.size) for o in tp_orders_list if o.size > 0)
                tp_short_qty = sum(abs(o.size) for o in tp_orders_list if o.size < 0)
                
                needs_tp_refresh = (
                    tp_count == 0 or
                    (long_size > 0 and tp_long_qty != long_size) or
                    (short_size > 0 and tp_short_qty != short_size)
                )
                
                if needs_tp_refresh:
                    log("🔧 HEALTH", f"TP mismatch (Long: {long_size} vs TP {tp_long_qty}, Short: {short_size} vs TP {tp_short_qty}) → Refreshing TP")
                    time.sleep(0.5)
                    refresh_all_tp_orders()
            
            # 4. 기존: 양방향 포지션 + 그리드 존재 체크
            if long_size > 0 and short_size > 0 and grid_count >= 2:
                log("🔧 HEALTH", f"Both positions with {grid_count} grids (should be 0) → Cancelling")
                time.sleep(0.5)
                cancel_grid_only()
            
            # ✅ 5. 신규: 전략 일관성 검증
            validate_strategy_consistency()
            
            # ✅ 6. 신규: 중복 그리드 제거
            remove_duplicate_orders()
            
            # ✅ 7. 신규: 오래된 주문 취소
            cancel_stale_orders()
            
            log("✅ HEALTH", "Health check complete")
            
        except Exception as e:
            log("❌", f"Health check error: {e}")
            import traceback
            log("❌", f"Traceback: {traceback.format_exc()}")


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
    obv_display = float(obv_macd_value) * 1000
    return jsonify({
        "status": "running",
        "obv_macd_display": obv_display,
        "obv_macd_internal": float(obv_macd_value),
        "api_configured": bool(API_KEY and API_SECRET)
    }), 200

@app.route('/status', methods=['GET'])
def status():
    """상세 상태 조회"""
    with position_lock:
        pos = position_state[SYMBOL]
    with balance_lock:
        bal = float(account_balance)
    
    obv_display = float(obv_macd_value) * 1000
    
    return jsonify({
        "balance": bal,
        "obv_macd_display": obv_display,
        "obv_macd_internal": float(obv_macd_value),
        "position": {
            "long": {"size": float(pos["long"]["size"]), "price": float(pos["long"]["price"])},
            "short": {"size": float(pos["short"]["size"]), "price": float(pos["short"]["price"])}
        },
        "post_threshold_entries": {
            "long": [{"qty": e["qty"], "price": e["price"], "type": e["entry_type"]} 
                     for e in post_threshold_entries[SYMBOL]["long"]],
            "short": [{"qty": e["qty"], "price": e["price"], "type": e["entry_type"]} 
                      for e in post_threshold_entries[SYMBOL]["short"]]
        },
        "counter_snapshot": {
            "long": float(counter_position_snapshot[SYMBOL]["long"]),
            "short": float(counter_position_snapshot[SYMBOL]["short"])
        },
        "max_locked": max_position_locked,
        "threshold_status": {
            "long": is_above_threshold("long"),
            "short": is_above_threshold("short")
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
    """임계값 추적 데이터 강제 초기화"""
    try:
        post_threshold_entries[SYMBOL]["long"].clear()
        post_threshold_entries[SYMBOL]["short"].clear()
        counter_position_snapshot[SYMBOL]["long"] = Decimal("0")
        counter_position_snapshot[SYMBOL]["short"] = Decimal("0")
        log("🔄 RESET", "All tracking data cleared")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# =============================================================================
# 메인 실행
# =============================================================================
def print_startup_summary():
    global account_balance
    
    log_divider("=")
    log("🚀 START", "ONDO Trading Bot v26.0")
    log_divider("=")
    
    # API 키 확인
    if not API_KEY or not API_SECRET:
        log("❌ ERROR", "API_KEY or API_SECRET not set!")
        log("ℹ️ INFO", "Set environment variables: API_KEY, API_SECRET")
        return
    
    log("✅ API", f"Key: {API_KEY[:8]}...")
    log("✅ API", f"Secret: {API_SECRET[:8]}...")
    
    # API 연결 테스트
    try:
        test_ticker = api.list_futures_tickers(SETTLE, contract=SYMBOL)
        if test_ticker:
            log("✅ API", "Connection test successful")
    except GateApiException as e:
        log("❌ API", f"Connection test failed: {e}")
        log("⚠️ WARNING", "Check API key permissions:")
        log("  ", "- Futures: Read + Trade")
        log("  ", "- Unified Account: Read")
    except Exception as e:
        log("❌ API", f"Connection test error: {e}")
    
    log_divider("-")
    log("📜 CONFIG", "Settings:")
    log("  ├─", f"Symbol: {SYMBOL}")
    log("  ├─", f"Grid/TP Gap: {GRID_GAP_PCT * 100}%")
    log("  ├─", f"Base Ratio: {BASE_RATIO * 100}%")
    log("  ├─", f"Threshold: {THRESHOLD_RATIO * 100}%")
    log("  ├─", f"Max Position: {MAX_POSITION_RATIO * 100}%")
    log("  ├─", f"Counter Ratio: {COUNTER_RATIO * 100}%")
    log("  ├─", f"Counter Close: {COUNTER_CLOSE_RATIO * 100}%")
    log("  └─", f"Hedge Main: {HEDGE_RATIO_MAIN * 100}%")
    log_divider("-")
    
    # 초기 잔고 조회
    try:
        accounts = unified_api.list_unified_accounts()
        if accounts and hasattr(accounts, 'total') and accounts.total:
            account_balance = Decimal(str(accounts.total))
            log("💰 BALANCE", f"{account_balance:.2f} USDT (Unified Total)")
        else:
            futures_accounts = api.list_futures_accounts(SETTLE)
            if futures_accounts and hasattr(futures_accounts, 'available') and futures_accounts.available:
                account_balance = Decimal(str(futures_accounts.available))
                log("💰 BALANCE", f"{account_balance:.2f} USDT (Futures Available)")
            else:
                log("⚠️ BALANCE", "Could not fetch - using default 50 USDT")
        
        log("💰 THRESHOLD", f"{account_balance * THRESHOLD_RATIO:.2f} USDT")
        log("💰 MAX POSITION", f"{account_balance * MAX_POSITION_RATIO:.2f} USDT")
    except Exception as e:
        log("❌ ERROR", f"Balance check failed: {e}")
        log("⚠️ WARNING", "Using default balance: 50 USDT")
    
    log_divider("-")
    
    # 기존 포지션 확인
    sync_position()
    log_position_state()
    log_threshold_info()
    log_divider("-")
    
    # 초기화
    try:
        current_price = get_current_price()
        if current_price > 0:
            log("💹 PRICE", f"{current_price:.4f}")
            cancel_all_orders()
            time.sleep(0.5)
            
            # 그리드 생성 (내부에서 롱/숏 모두 있으면 TP 생성)
            initialize_grid(current_price)
            
            # initialize_grid에서 TP를 생성하지 않은 경우에만 추가 생성
            # (롱/숏 중 하나만 있거나 없는 경우)
            with position_lock:
                long_size = position_state[SYMBOL]["long"]["size"]
                short_size = position_state[SYMBOL]["short"]["size"]
            
            # 롱/숏 중 하나만 있으면 TP 생성 (initialize_grid에서 이미 처리되지 않은 경우)
            if (long_size > 0 or short_size > 0) and not (long_size > 0 and short_size > 0):
                time.sleep(1)
                refresh_all_tp_orders()
        else:
            log("⚠️", "Could not fetch current price")
    except Exception as e:
        log("❌", f"Initialization error: {e}")
    
    log_divider("=")
    log("✅ INIT", "Complete. Starting threads...")
    log_divider("=")

def start_grid_monitor():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(grid_fill_monitor())
    
if __name__ == '__main__':
    print_startup_summary()
    
    # API 키 최종 확인
    if not API_KEY or not API_SECRET:
        log("❌ FATAL", "Cannot start without API credentials!")
        log("ℹ️ INFO", "Set Railway environment variables:")
        log("  ", "- API_KEY")
        log("  ", "- API_SECRET")
        log("  ", "- SYMBOL (optional, default: ONDO_USDT)")
        exit(1)
    
    update_event_time()  # ← 기존
    
    # 모든 모니터링 스레드 시작
    threading.Thread(target=update_balance_thread, daemon=True).start()
    threading.Thread(target=fetch_kline_thread, daemon=True).start()
    threading.Thread(target=start_websocket, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=start_grid_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    threading.Thread(target=idle_monitor, daemon=True).start()
    threading.Thread(target=periodic_health_check, daemon=True).start()  # ✅ 추가
    
    log("✅ THREADS", "All monitoring threads started")
    log("🌐 FLASK", "Starting server on port 8080...")
    log("📊 OBV MACD", "Self-calculating from 1min candles")
    log("📨 WEBHOOK", "Optional: TradingView webhook at /webhook")
    log("🔍 HEALTH", "Health check every 2 minutes")  # ✅ 추가
    
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)

