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

GRID_GAP_PCT = Decimal("0.0012")  # 0.12%
TP_GAP_PCT = Decimal("0.0012")    # 0.12%
BASE_RATIO = Decimal("0.1")       # 기본 수량 비율
THRESHOLD_RATIO = Decimal("0.8")  # 임계값
COUNTER_RATIO = Decimal("0.30")   # 비주력 30%
COUNTER_CLOSE_RATIO = Decimal("0.20")  # 비주력 20% 청산
MAX_POSITION_RATIO = Decimal("5.0")    # 최대 5배
HEDGE_RATIO_MAIN = Decimal("0.10")     # 주력 10%

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
        balance = INITIAL_BALANCE
    
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
        balance = INITIAL_BALANCE
    with position_lock:
        long_size = position_state[SYMBOL]["long"]["size"]
        long_price = position_state[SYMBOL]["long"]["price"]
        short_size = position_state[SYMBOL]["short"]["size"]
        short_price = position_state[SYMBOL]["short"]["price"]
    
    threshold = balance * THRESHOLD_RATIO
    long_value = long_price * long_size
    short_value = short_price * short_size
    
    log("💰 THRESHOLD", f"${threshold:.2f} | Long: ${long_value:.2f} {'✅' if long_value >= threshold else '❌'} | Short: ${short_value:.2f} {'✅' if short_value >= threshold else '❌'}")

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
                log("🔌 WS", "Connected")
                
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
                                        position_state[SYMBOL]["short"]["size"] = Decimal("0")
                                        position_state[SYMBOL]["short"]["price"] = Decimal("0")
                                    elif size_dec < 0:
                                        position_state[SYMBOL]["short"]["size"] = abs(size_dec)
                                        position_state[SYMBOL]["short"]["price"] = entry_price
                                        position_state[SYMBOL]["long"]["size"] = Decimal("0")
                                        position_state[SYMBOL]["long"]["price"] = Decimal("0")
                                    else:
                                        position_state[SYMBOL]["long"]["size"] = Decimal("0")
                                        position_state[SYMBOL]["long"]["price"] = Decimal("0")
                                        position_state[SYMBOL]["short"]["size"] = Decimal("0")
                                        position_state[SYMBOL]["short"]["price"] = Decimal("0")
        except Exception as e:
            log("❌", f"WebSocket error: {e}")
            await asyncio.sleep(5)

def start_websocket():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(watch_positions())

# =============================================================================
# 포지션 동기화 - 에러 시 재시도 간격 증가
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
                        entry_price = abs(Decimal(str(p.entry_price))) if p.entry_price else Decimal("0")
                        if size_dec > 0:
                            position_state[SYMBOL]["long"]["size"] = size_dec
                            position_state[SYMBOL]["long"]["price"] = entry_price
                        elif size_dec < 0:
                            position_state[SYMBOL]["short"]["size"] = abs(size_dec)
                            position_state[SYMBOL]["short"]["price"] = entry_price
        return True
    except GateApiException as e:
        # API 인증 오류는 로그 스팸 방지를 위해 첫 번째만 출력
        return False
    except Exception as e:
        log("❌", f"Position sync error: {e}")
        return False

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
    obv_value = float(obv_macd_value) * 1000  # tt1 값 스케일링
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

def is_above_threshold(side):
    """특정 포지션이 임계값 초과인지 확인"""
    with position_lock:
        size = position_state[SYMBOL][side]["size"]
        price = position_state[SYMBOL][side]["price"]
    
    with balance_lock:
        threshold = INITIAL_BALANCE * THRESHOLD_RATIO
    
    return (price * size >= threshold and size > 0)

def get_counter_side(side):
    return "short" if side == "long" else "long"

# =============================================================================
# 주문 실행
# =============================================================================
def place_grid_order(side, price, qty, is_counter=False):
    try:
        if qty <= 0:
            log("⚠️ GRID", f"Invalid quantity: {qty}")
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
            grid_orders[SYMBOL][side].append({
                "order_id": result.id,
                "price": float(price),
                "qty": int(qty),
                "is_counter": is_counter
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

def initialize_grid(current_price):
    side = get_main_side()
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
            log("ℹ️ GRID", "Both positions exist → Skip grid creation")
            return
        
        cancel_grid_only()
        
        long_grid_price = current_price * (Decimal("1") - GRID_GAP_PCT)
        short_grid_price = current_price * (Decimal("1") + GRID_GAP_PCT)
        
        with balance_lock:
            threshold = INITIAL_BALANCE * THRESHOLD_RATIO
            balance = INITIAL_BALANCE
        
        long_above = (long_price * long_size >= threshold and long_size > 0)
        short_above = (short_price * short_size >= threshold and short_size > 0)
        
        log("📈 GRID INIT", f"Price: {current_price:.4f}")
        obv_display = float(obv_macd_value) * 1000
        log("📊 OBV MACD", f"Value: {obv_display:.2f}")
        
        if long_above:
            counter_qty = max(1, int(long_size * COUNTER_RATIO))
            same_qty = calculate_grid_qty(is_above_threshold=True)
            weight = BASE_RATIO
            log("⚖️ ASYMMETRIC", f"Long is MAIN (above threshold)")
            log("📊 QUANTITY", f"Counter(Short): {counter_qty} | Same(Long): {same_qty} | Weight: {weight*100}%")
            place_grid_order("short", short_grid_price, counter_qty, is_counter=True)
            place_grid_order("long", long_grid_price, same_qty, is_counter=False)
            
        elif short_above:
            counter_qty = max(1, int(short_size * COUNTER_RATIO))
            same_qty = calculate_grid_qty(is_above_threshold=True)
            weight = BASE_RATIO
            log("⚖️ ASYMMETRIC", f"Short is MAIN (above threshold)")
            log("📊 QUANTITY", f"Counter(Long): {counter_qty} | Same(Short): {same_qty} | Weight: {weight*100}%")
            place_grid_order("long", long_grid_price, counter_qty, is_counter=True)
            place_grid_order("short", short_grid_price, same_qty, is_counter=False)
            
        else:
            qty = calculate_grid_qty(is_above_threshold=False)
            weight = calculate_obv_macd_weight(obv_display)
            log("⚖️ SYMMETRIC", "Below threshold - OBV MACD based")
            log("📊 QUANTITY", f"Both sides: {qty} | OBV MACD: {obv_display:.2f} | Weight: {weight*100}%")
            place_grid_order("long", long_grid_price, qty, is_counter=False)
            place_grid_order("short", short_grid_price, qty, is_counter=False)
            
    except Exception as e:
        log("❌", f"Grid init error: {e}")

def hedge_after_grid_fill(filled_side, filled_price, filled_qty, was_counter_grid):
    """그리드 체결 후 헤징"""
    try:
        main_side = get_main_side()
        hedge_side = get_counter_side(filled_side)
        
        with position_lock:
            main_size = position_state[SYMBOL][main_side]["size"] if main_side != "none" else Decimal("0")
            main_price = position_state[SYMBOL][main_side]["price"] if main_side != "none" else Decimal("0")
        
        with balance_lock:
            balance = INITIAL_BALANCE
        
        current_price = get_current_price()
        if current_price <= 0: return
        
        threshold = balance * THRESHOLD_RATIO
        main_value = main_price * main_size
        above_threshold = (main_value >= threshold and main_size > 0)
        
        # 헤징 전 임계값 상태 저장
        hedge_will_be_above_threshold = is_above_threshold(hedge_side)
        
        # 헤징 수량 계산
        if above_threshold:
            if was_counter_grid:  # 비주력(30%) 그리드 체결
                qty_10pct = int(main_size * HEDGE_RATIO_MAIN)
                base_qty = round(float((balance * BASE_RATIO) / current_price))
                hedge_qty = max(qty_10pct, base_qty, 1)
                log("🛡️ HEDGE", f"Counter grid filled → Hedge: max(10%={qty_10pct}, base={base_qty}) = {hedge_qty}")
            else:  # 주력 그리드 체결
                hedge_qty = max(1, round(float((balance * BASE_RATIO) / current_price)))
                log("🛡️ HEDGE", f"Main grid filled → Hedge: base={hedge_qty}")
        else:
            hedge_qty = max(1, round(float((balance * BASE_RATIO) / current_price)))
            log("🛡️ HEDGE", f"Below threshold → Hedge: base={hedge_qty}")
        
        size = hedge_qty if hedge_side == "long" else -hedge_qty
        
        log("🛡️ HEDGING", f"{hedge_side.upper()} {hedge_qty} @ market")
        order = FuturesOrder(contract=SYMBOL, size=size, price="0", tif='ioc')
        created_order = api.create_futures_order(SETTLE, order)
        
        # 헤징 체결 확인 및 추적
        if created_order and hasattr(created_order, 'id'):
            time.sleep(1)
            try:
                trades = api.list_my_trades(settle=SETTLE, contract=SYMBOL, limit=10)
                for trade in trades:
                    if str(trade.order_id) == str(created_order.id):
                        trade_qty = abs(Decimal(str(trade.size)))
                        trade_price = Decimal(str(trade.price))
                        log("✅ HEDGE", f"{hedge_side.upper()} {trade_qty} @ {trade_price:.4f}")
                        
                        # 임계값 초과 후 헤징만 추적
                        if hedge_will_be_above_threshold:
                            track_entry(hedge_side, trade_qty, trade_price, "hedge", was_counter=False)
                        break
            except Exception as e:
                log("❌", f"Trade fetch error: {e}")
                    
    except Exception as e:
        log("❌", f"Hedging error: {e}")

def create_individual_tp(side, qty, entry_price):
    try:
        tp_price = entry_price * (Decimal("1") + TP_GAP_PCT) if side == "long" else entry_price * (Decimal("1") - TP_GAP_PCT)
        size = -qty if side == "long" else qty
        order = FuturesOrder(contract=SYMBOL, size=size, price=str(tp_price), tif="gtc", reduce_only=True)
        result = api.create_futures_order(SETTLE, order)
        if result and hasattr(result, 'id'):
            log("🎯 INDIVIDUAL TP", f"{side.upper()} {qty} @ {tp_price:.4f}")
            return result.id
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
        
        # 개별 TP가 있는 수량 계산
        individual_total = sum(entry["qty"] for entry in post_threshold_entries[SYMBOL][side])
        remaining_qty = int(size - individual_total)
        
        if remaining_qty <= 0: return
        
        tp_price = avg_price * (Decimal("1") + TP_GAP_PCT) if side == "long" else avg_price * (Decimal("1") - TP_GAP_PCT)
        order_size = -remaining_qty if side == "long" else remaining_qty
        
        order = FuturesOrder(contract=SYMBOL, size=order_size, price=str(tp_price), tif="gtc", reduce_only=True)
        result = api.create_futures_order(SETTLE, order)
        if result and hasattr(result, 'id'):
            average_tp_orders[SYMBOL][side] = result.id
            log("🎯 AVERAGE TP", f"{side.upper()} {remaining_qty} @ {tp_price:.4f} (remaining from pre-threshold)")
            
    except Exception as e:
        log("❌", f"Average TP error: {e}")

def refresh_all_tp_orders():
    try:
        orders = api.list_futures_orders(SETTLE, contract=SYMBOL, status='open')
        tp_orders = [o for o in orders if o.is_reduce_only]
        if tp_orders:
            log("🗑️ CANCEL", f"{len(tp_orders)} TP orders")
            for order in tp_orders:
                try:
                    api.cancel_futures_order(SETTLE, order.id)
                    time.sleep(0.05)
                except:
                    pass
        
        average_tp_orders[SYMBOL] = {"long": None, "short": None}
        
        with position_lock:
            long_size = position_state[SYMBOL]["long"]["size"]
            short_size = position_state[SYMBOL]["short"]["size"]
            long_price = position_state[SYMBOL]["long"]["price"]
            short_price = position_state[SYMBOL]["short"]["price"]
        
        log("📈 TP REFRESH", "Creating TP orders...")
        log_threshold_info()
        
        # 롱 TP
        if long_size > 0:
            if is_above_threshold("long"):
                log("📊 LONG TP", "Above threshold → Individual + Average TPs")
                # 개별 TP 생성
                individual_total = 0
                for entry in post_threshold_entries[SYMBOL]["long"]:
                    tp_id = create_individual_tp("long", entry["qty"], Decimal(str(entry["price"])))
                    if tp_id:
                        entry["tp_order_id"] = tp_id
                        individual_total += entry["qty"]
                
                # 나머지는 평단 TP
                remaining = int(long_size) - individual_total
                if remaining > 0:
                    tp_price = long_price * (Decimal("1") + TP_GAP_PCT)
                    order = FuturesOrder(contract=SYMBOL, size=-remaining, price=str(tp_price), tif="gtc", reduce_only=True)
                    result = api.create_futures_order(SETTLE, order)
                    if result and hasattr(result, 'id'):
                        average_tp_orders[SYMBOL]["long"] = result.id
                        log("🎯 AVERAGE TP", f"LONG {remaining} @ {tp_price:.4f}")
            else:
                # 임계값 미만: 전체 평단 TP
                log("📊 LONG TP", "Below threshold → Full average TP")
                tp_price = long_price * (Decimal("1") + TP_GAP_PCT)
                order = FuturesOrder(contract=SYMBOL, size=-int(long_size), price=str(tp_price), tif="gtc", reduce_only=True)
                result = api.create_futures_order(SETTLE, order)
                if result and hasattr(result, 'id'):
                    average_tp_orders[SYMBOL]["long"] = result.id
                    log("🎯 FULL TP", f"LONG {int(long_size)} @ {tp_price:.4f}")
        
        # 숏 TP
        if short_size > 0:
            if is_above_threshold("short"):
                log("📊 SHORT TP", "Above threshold → Individual + Average TPs")
                individual_total = 0
                for entry in post_threshold_entries[SYMBOL]["short"]:
                    tp_id = create_individual_tp("short", entry["qty"], Decimal(str(entry["price"])))
                    if tp_id:
                        entry["tp_order_id"] = tp_id
                        individual_total += entry["qty"]
                
                remaining = int(short_size) - individual_total
                if remaining > 0:
                    tp_price = short_price * (Decimal("1") - TP_GAP_PCT)
                    order = FuturesOrder(contract=SYMBOL, size=remaining, price=str(tp_price), tif="gtc", reduce_only=True)
                    result = api.create_futures_order(SETTLE, order)
                    if result and hasattr(result, 'id'):
                        average_tp_orders[SYMBOL]["short"] = result.id
                        log("🎯 AVERAGE TP", f"SHORT {remaining} @ {tp_price:.4f}")
            else:
                log("📊 SHORT TP", "Below threshold → Full average TP")
                tp_price = short_price * (Decimal("1") - TP_GAP_PCT)
                order = FuturesOrder(contract=SYMBOL, size=int(short_size), price=str(tp_price), tif="gtc", reduce_only=True)
                result = api.create_futures_order(SETTLE, order)
                if result and hasattr(result, 'id'):
                    average_tp_orders[SYMBOL]["short"] = result.id
                    log("🎯 FULL TP", f"SHORT {int(short_size)} @ {tp_price:.4f}")
                    
    except Exception as e:
        log("❌", f"TP refresh error: {e}")

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
def track_entry(side, qty, price, entry_type, was_counter):
    """임계값 초과 후 진입 추적"""
    if not is_above_threshold(side):
        return
    
    entry_data = {
        "qty": int(qty),
        "price": float(price),
        "entry_type": entry_type,
        "was_counter": was_counter,
        "tp_order_id": None
    }
    post_threshold_entries[SYMBOL][side].append(entry_data)
    log("📝 TRACKED", f"{side.upper()} {qty} @ {price:.4f} ({entry_type}, counter={was_counter})")

# =============================================================================
# 시스템 새로고침
# =============================================================================
def full_refresh(event_type):
    log_event_header(f"FULL REFRESH: {event_type}")
    
    log("🔄 SYNC", "Syncing position...")
    sync_position()
    log_position_state()
    log_threshold_info()

    cancel_all_orders()
    time.sleep(0.5)
    
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
                    "tp_order_id": tp_id
                })
        
        full_refresh("Hedge")
        return order_id
        
    except GateApiException as e:
        log("❌", f"Hedge submission error: {e}")
        return None
    except Exception as e:
        log("❌", f"Hedge order error: {e}")
        return None

def grid_fill_monitor():
    last_check_time = 0
    while True:
        try:
            time.sleep(0.5)
            current_time = time.time()
            if current_time - last_check_time < 3:
                continue
            last_check_time = current_time

            current_price = get_current_price()
            if current_price <= 0:
                continue

            # 그리드 주문 확인 및 체결 처리
            for side in ["long", "short"]:
                target_orders = grid_orders[SYMBOL][side]  # 수정
                filled_orders = []
                for order_info in list(target_orders):
                    try:
                        order_id = order_info["order_id"]  # dict 키로 접근
                        order = api.get_futures_order(SETTLE, str(order_id))
                        if not order:
                            continue
                        if hasattr(order, 'status') and order.status == "finished":
                            log_event_header("GRID FILLED")
                            log("✅ FILL", f"{side.upper()} @ {order.price}")
                            
                            # 주문 확인 후 헷징 주문 실행
                            place_hedge_order(side)
                            
                            # 모든 그리드 및 TP 주문 취소
                            cancel_grid_only()
                            full_refresh("Grid_Fill")
                            time.sleep(0.5)
                            
                            filled_orders.append(order_info)
                            break
                    except GateApiException as e:
                        if "ORDER_NOT_FOUND" in str(e):
                            filled_orders.append(order_info)
                    except Exception as e:
                        log("❌", f"Grid fill check error: {e}")
                
                for order_info in filled_orders:
                    if order_info in target_orders:
                        target_orders.remove(order_info)

        except Exception as e:
            log("❌", f"Grid fill monitor error: {e}")
            time.sleep(1)

def tp_monitor():
    while True:
        try:
            time.sleep(3)
            
            # 개별 TP 체결 확인 (임계값 이후 진입 주력 포지션)
            for side in ["long", "short"]:
                filled_entries = []
                for entry in list(post_threshold_entries[SYMBOL][side]):
                    tp_id = entry.get("tp_order_id")
                    if not tp_id:
                        continue
                    
                    try:
                        order = api.get_futures_order(SETTLE, str(tp_id))
                        if order and hasattr(order, 'status') and order.status == "finished":
                            log_event_header("INDIVIDUAL TP HIT")
                            log("✅ TP", f"{side.upper()} {entry['qty']} @ entry {entry['price']:.4f}")
                            
                            close_counter_on_individual_tp(side)
                            time.sleep(0.5)
                            
                            filled_entries.append(entry)
                            full_refresh("Individual_TP")
                            break
                            
                    except GateApiException as e:
                        if "ORDER_NOT_FOUND" in str(e):
                            filled_entries.append(entry)
                    except Exception as e:
                        log("❌", f"Individual TP check error: {e}")
                
                for entry in filled_entries:
                    if entry in post_threshold_entries[SYMBOL][side]:
                        post_threshold_entries[SYMBOL][side].remove(entry)
            
            # 평단 TP 체결 확인 (임계값 이전 진입 물량)
            for side in ["long", "short"]:
                tp_id = average_tp_orders[SYMBOL][side]
                if not tp_id:
                    continue
                
                try:
                    order = api.get_futures_order(SETTLE, str(tp_id))
                    if order and hasattr(order, 'status') and order.status == "finished":
                        log_event_header("AVERAGE TP HIT")
                        log("✅ TP", f"{side.upper()} average position closed")
                        average_tp_orders[SYMBOL][side] = None
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
    api_error_count = 0
    last_error_log = 0
    
    while True:
        try:
            time.sleep(5)  # 1초 → 5초로 변경 (API 부하 감소)
            
            success = sync_position()
            
            if not success:
                api_error_count += 1
                # 10초에 한 번만 에러 로그 (스팸 방지)
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
                balance = INITIAL_BALANCE
            
            threshold = balance * THRESHOLD_RATIO
            max_value = balance * MAX_POSITION_RATIO
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
    with position_lock:
        pos = position_state[SYMBOL]
    with balance_lock:
        bal = float(INITIAL_BALANCE)
    
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
# =============================================================================route('/health', methods=['GET'])
def health():
    """헬스 체크"""
    return jsonify({
        "status": "running",
        "obv_macd": float(obv_macd_value),
        "api_configured": bool(API_KEY and API_SECRET)
    }), 200

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
    global INITIAL_BALANCE  # 함수 최상단으로 이동
    
    log_divider("=")
    log("🚀 START", "ONDO Trading Bot v26.0-COMPLETE")
    log_divider("=")
    
    # API 키 확인
    if not API_KEY or not API_SECRET:
        log("❌ ERROR", "API_KEY or API_SECRET not set!")
        log("ℹ️ INFO", "Set environment variables: API_KEY, API_SECRET")
        return
    else:
        log("✅ API", f"Key: {API_KEY[:8]}...")
        log("✅ API", f"Secret: {API_SECRET[:8]}...")
        
        # API 키 검증 테스트
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
    
    # 초기 잔고 - Unified Account 전체 잔고
    try:
        # Unified Account total 조회
        accounts = unified_api.list_unified_accounts()
        if accounts and hasattr(accounts, 'total') and accounts.total:
            INITIAL_BALANCE = Decimal(str(accounts.total))
            log("💰 BALANCE", f"{INITIAL_BALANCE:.2f} USDT (Unified Total)")
        else:
            log("⚠️ BALANCE", "Unified account not found, trying futures account...")
            # Futures 계좌 available 조회
            futures_accounts = api.list_futures_accounts(SETTLE)
            if futures_accounts and hasattr(futures_accounts, 'available') and futures_accounts.available:
                INITIAL_BALANCE = Decimal(str(futures_accounts.available))
                log("💰 BALANCE", f"{INITIAL_BALANCE:.2f} USDT (Futures Available)")
            else:
                log("⚠️ BALANCE", "Could not fetch - using default 50 USDT")
        
        log("💰 THRESHOLD", f"{INITIAL_BALANCE * THRESHOLD_RATIO:.2f} USDT")
        log("💰 MAX POSITION", f"{INITIAL_BALANCE * MAX_POSITION_RATIO:.2f} USDT")
    except Exception as e:
        log("❌ ERROR", f"Balance check failed: {e}")
        log("⚠️ WARNING", "Using default balance: 50 USDT")
    
    log_divider("-")
    
    # 기존 포지션
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
            
            initialize_grid(current_price)
            
            with position_lock:
                pos = position_state[SYMBOL]
                if pos['long']['size'] > 0 or pos['short']['size'] > 0:
                    refresh_all_tp_orders()
        else:
            log("⚠️", "Could not fetch current price")
    except Exception as e:
        log("❌", f"Initialization error: {e}")
    
    log_divider("=")
    log("✅ INIT", "Complete. Starting threads...")
    log_divider("=")

if __name__ == '__main__':
    print_startup_summary()
    
    # API 키 확인
    if not API_KEY or not API_SECRET:
        log("❌ FATAL", "Cannot start without API credentials!")
        log("ℹ️ INFO", "Set Railway environment variables:")
        log("  ", "- GATE_API_KEY")
        log("  ", "- GATE_API_SECRET")
        log("  ", "- SYMBOL (optional, default: ONDO_USDT)")
        exit(1)
    
    threading.Thread(target=update_balance_thread, daemon=True).start()
    threading.Thread(target=fetch_kline_thread, daemon=True).start()
    threading.Thread(target=start_websocket, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=grid_fill_monitor, daemon=True).start()
    threading.Thread(target=tp_monitor, daemon=True).start()
    
    log("✅ THREADS", "All monitoring threads started")
    log("🌐 FLASK", "Starting server on port 8080...")
    log("📊 OBV MACD", "Calculating from 1min candles (auto-normalized)")
    log("📨 WEBHOOK", "Optional: TradingView webhook available at /webhook")
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
