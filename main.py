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
import hashlib
import numpy as np
from collections import deque

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
SYMBOLS = ["ARB_USDT", "PAXG_USDT"]  # 멀티 심볼
SETTLE = "usdt"

# Railway 환경 변수 로그
if API_KEY:
    logger.info(f"✅ API_KEY loaded: {API_KEY[:8]}...")
else:
    logger.error("❌ API_KEY not found in environment variables!")

if API_SECRET:
    logger.info(f"✅ API_SECRET loaded: {len(API_SECRET)} characters")
else:
    logger.error("❌ API_SECRET not found in environment variables!")

TITLE = os.environ.get("RAILWAY_STATIC_URL", "Railway Multi-Symbol Trading Bot")
logger.info(f"📌 Environment: {TITLE}")

# =============================================================================
# 전략 설정 (Strategy Configuration) - 심볼별 설정
# =============================================================================
# 심볼별 진입 비율 및 Tier 설정
SYMBOL_CONFIG = {
    "ARB_USDT": {
        "base_ratio": Decimal("0.02"),      # 2%
        "tier1_min": Decimal("1.0"),        # Tier-1 시작
        "tier1_max": Decimal("2.0"),        # Tier-1 종료
        "tier1_multiplier": Decimal("0.8"), # Tier-1 청산 배수
        "tier2_multiplier": Decimal("1.5")  # Tier-2 청산 배수
    },
    "PAXG_USDT": {
        "base_ratio": Decimal("0.10"),  # 3% → 10% 증가!
        # 1052 * 0.10 = 105.2 USDT
        # 105.2 / 4086 = 0.0257개 → 25 계약!
        "tier1_min": Decimal("2"),
        "tier1_max": Decimal("3.0"),
        "tier1_multiplier": Decimal("1.6"),
        "tier2_multiplier": Decimal("2.2")
    }
}

# 공통 설정
INITIALBALANCE = Decimal("50")               # 초기 잔고
MAXPOSITIONRATIO = Decimal("3.0")           # 최대 포지션 비율 (3배)
HEDGE_RATIO_MAIN = Decimal("0.10")          # 주력 헤지 비율 (10%)
ENABLE_AUTO_HEDGE = True                    # 자동 헤징 활성화

# TP 설정 (동적 TP)
TPMIN = Decimal("0.0021")                   # 최소 TP (0.21%)
TPMAX = Decimal("0.004")                    # 최대 TP (0.4%)

# 아이들 타임아웃
IDLE_TIMEOUT = 600  # 10분
MAX_IDLE_ENTRIES = 100  # 최대 아이들 진입 횟수

# OBV MACD 변화 감지 임계값
OBV_CHANGE_THRESHOLD = 10.0  # ×100 정규화 기준

# Initial Capital 파일
CAPITAL_FILE = "initial_capital.json"

# =============================================================================
# Gate.io API 설정
# =============================================================================
config = Configuration(key=API_KEY, secret=API_SECRET)
api_client = ApiClient(config)
api = FuturesApi(api_client)
unified_api = UnifiedApi(api_client)

# Flask 앱
app = Flask(__name__)

# =============================================================================
# 전역 변수 (Global Variables) - 멀티 심볼
# =============================================================================
# 계정 레벨 (공유)
account_balance = Decimal("0")
initial_capital = Decimal("0")

# 심볼별 변수
position_state = {
    symbol: {
        "long": {"size": Decimal("0"), "entry_price": Decimal("0")},
        "short": {"size": Decimal("0"), "entry_price": Decimal("0")}
    }
    for symbol in SYMBOLS
}

tp_gap_long = {symbol: TPMIN for symbol in SYMBOLS}
tp_gap_short = {symbol: TPMIN for symbol in SYMBOLS}

average_tp_orders = {
    symbol: {"long": None, "short": None}
    for symbol in SYMBOLS
}

grid_orders = {
    symbol: {"long": [], "short": []}
    for symbol in SYMBOLS
}

obv_macd_value = {symbol: Decimal("0") for symbol in SYMBOLS}
last_adjusted_obv = {symbol: 0.0 for symbol in SYMBOLS}
last_tp_hash = {symbol: "" for symbol in SYMBOLS}

idle_entry_count = {symbol: 0 for symbol in SYMBOLS}
idle_entry_in_progress = {symbol: False for symbol in SYMBOLS}
last_event_time = {symbol: 0.0 for symbol in SYMBOLS}
last_idle_check = {symbol: 0.0 for symbol in SYMBOLS}
last_grid_time = {symbol: 0.0 for symbol in SYMBOLS}

kline_history = {symbol: deque(maxlen=200) for symbol in SYMBOLS}

max_position_locked = {symbol: {"long": False, "short": False} for symbol in SYMBOLS}

# 락
position_lock = threading.Lock()
balance_lock = threading.Lock()
initialize_grid_lock = threading.Lock()

# 전역 변수
obv_history = {symbol: deque(maxlen=200) for symbol in SYMBOLS}
obv_macd_value = {symbol: Decimal("0") for symbol in SYMBOLS}

# =============================================================================
# 헬퍼 함수 (Helper Functions)
# =============================================================================

def log(tag, message):
    """통합 로그 함수"""
    logger.info(f"[{tag}] {message}")


def get_contract_size(symbol, actual_size):
    """실제 수량 → 계약 수 변환 (Gate.io는 1:1)"""
    return round(float(actual_size), 3)  # 소수점 3자리

def get_actual_size(symbol, contract_size):
    """계약 수 → 실제 수량 변환 (Gate.io는 1:1)"""
    return round(float(contract_size), 3)  # 소수점 3자리

def generate_order_id():
    """고유 주문 ID 생성"""
    return f"t-{int(time.time() * 1000)}"


def save_initial_capital():
    """초기 자본금 저장"""
    try:
        with balance_lock:
            data = {
                "initial_capital": str(initial_capital),
                "timestamp": time.time(),
                "symbols": SYMBOLS
            }
        with open(CAPITAL_FILE, 'w') as f:
            json.dump(data, f)
        log("💾 SAVE", f"Initial Capital: {initial_capital} USDT")
    except Exception as e:
        log("❌ SAVE", f"Failed to save initial capital: {e}")


def load_initial_capital():
    """초기 자본금 로드"""
    global initial_capital, account_balance
    
    try:
        if os.path.exists(CAPITAL_FILE):
            with open(CAPITAL_FILE, 'r') as f:
                data = json.load(f)
            
            saved_capital = Decimal(data.get("initial_capital", "0"))
            saved_time = data.get("timestamp", 0)
            
            if saved_capital > 0:
                with balance_lock:
                    initial_capital = saved_capital
                    account_balance = saved_capital
                
                time_diff = time.time() - saved_time
                log("💾 LOAD", f"Initial Capital: {initial_capital} USDT (saved {int(time_diff/60)} min ago)")
                return True
    
    except Exception as e:
        log("❌ LOAD", f"Failed to load initial capital: {e}")
    
    return False


def get_symbol_config(symbol, key):
    """심볼별 설정 가져오기"""
    return SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["ARB_USDT"]).get(key)


def update_account_balance():
    """계정 잔고 업데이트"""
    global initial_capital, account_balance
    
    try:
        futures_account = api.list_futures_accounts(SETTLE)
        if futures_account:
            available_str = getattr(futures_account, 'available', None)
            if available_str:
                current_available = Decimal(str(available_str))
                
                # 모든 심볼의 포지션 확인
                all_positions_zero = True
                for symbol in SYMBOLS:
                    with position_lock:
                        long_size = position_state[symbol]["long"]["size"]
                        short_size = position_state[symbol]["short"]["size"]
                    if long_size > 0 or short_size > 0:
                        all_positions_zero = False
                        break
                
                # 포지션 없으면 Initial Capital 갱신
                if all_positions_zero:
                    with balance_lock:
                        old_initial = initial_capital
                        initial_capital = current_available
                        account_balance = initial_capital
                    
                    if old_initial != initial_capital:
                        save_initial_capital()
                        log("💰 CAPITAL", f"Updated: {old_initial} → {initial_capital}")
                else:
                    with balance_lock:
                        account_balance = initial_capital
    
    except Exception as e:
        log("❌ BALANCE", f"Update error: {e}")


# =============================================================================
# 포지션 관리 (Position Management)
# =============================================================================

def sync_position(symbol=None, max_retries=3, retry_delay=2):
    """포지션 동기화 (멀티 심볼 지원)"""
    symbols_to_sync = [symbol] if symbol else SYMBOLS
    
    for attempt in range(max_retries):
        try:
            positions = api.list_positions(SETTLE)
            
            # 초기화
            for sym in symbols_to_sync:
                with position_lock:
                    position_state[sym]["long"]["size"] = Decimal("0")
                    position_state[sym]["long"]["entry_price"] = Decimal("0")
                    position_state[sym]["short"]["size"] = Decimal("0")
                    position_state[sym]["short"]["entry_price"] = Decimal("0")
            
            # 업데이트
            for pos in positions:
                contract = pos.contract
                if contract not in symbols_to_sync:
                    continue
                
                # ✅ 수정: 계약 수 → 실제 수량 변환
                contract_size = float(pos.size) if pos.size else 0
                actual_size = get_actual_size(contract, contract_size)  # 1 * 0.001 = 0.001
                
                entry_price = Decimal(str(pos.entry_price)) if pos.entry_price else Decimal("0")
                
                with position_lock:
                    if actual_size > 0:
                        position_state[contract]["long"]["size"] = Decimal(str(actual_size))
                        position_state[contract]["long"]["entry_price"] = entry_price
                    elif actual_size < 0:
                        position_state[contract]["short"]["size"] = Decimal(str(abs(actual_size)))
                        position_state[contract]["short"]["entry_price"] = entry_price
            
            # 로그
            for sym in symbols_to_sync:
                with position_lock:
                    long_size = position_state[sym]["long"]["size"]
                    short_size = position_state[sym]["short"]["size"]
                log("📊 SYNC", f"{sym}: L={long_size}, S={short_size}")
            
            return True
        
        except Exception as e:
            log("❌ SYNC", f"Attempt {attempt+1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    
    return False


def log_position_state(symbol):
    """포지션 상태 로깅"""
    try:
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
            long_price = position_state[symbol]["long"]["entry_price"]
            short_price = position_state[symbol]["short"]["entry_price"]
        
        if long_size == 0 and short_size == 0:
            log("📊 STATE", f"{symbol}: No positions")
            return
        
        current_price = get_current_price(symbol)
        current_price_dec = Decimal(str(current_price))
        
        # PnL 계산
        pnl_long = (current_price_dec - long_price) * long_size if long_size > 0 else Decimal("0")
        pnl_short = (short_price - current_price_dec) * short_size if short_size > 0 else Decimal("0")
        total_pnl = pnl_long + pnl_short
        
        # 포지션 가치
        long_value = long_size * current_price_dec
        short_value = short_size * current_price_dec
        
        log("📊 STATE", f"{symbol}:")
        log("  ", f"  LONG: {long_size} @ {long_price} (Value: {long_value:.2f}, PnL: {pnl_long:.2f})")
        log("  ", f"  SHORT: {short_size} @ {short_price} (Value: {short_value:.2f}, PnL: {pnl_short:.2f})")
        log("  ", f"  Total PnL: {total_pnl:.2f} USDT")
    
    except Exception as e:
        log("❌ STATE", f"{symbol} error: {e}")


def get_current_price(symbol):
    """현재 가격 조회"""
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if ticker and len(ticker) > 0:
            return float(ticker[0].last)
    except Exception as e:
        log("❌ PRICE", f"{symbol} price error: {e}")
    return 0


# =============================================================================
# OBV MACD 계산 (심볼별)
# =============================================================================

def calculate_obv_macd(symbol):
    """OBV MACD 계산 (파인스크립트 완전 구현)"""
    
    global obv_macd_value
    
    try:
        if len(kline_history[symbol]) < 60:
            log("❌ OBV", f"{symbol}: Not enough data ({len(kline_history[symbol])}/60)")
            return
        
        klines = list(kline_history[symbol])
        
        # 1. 기본 데이터 추출
        closes = np.array([float(k[2]) for k in klines])
        highs = np.array([float(k[3]) for k in klines])
        lows = np.array([float(k[4]) for k in klines])
        volumes = np.array([float(k[5]) for k in klines])
        
        # 2. OBV 계산 (파인스크립트 방식)
        window_len = 28
        v_len = 14
        
        # Price spread (volatility)
        price_spread_arr = highs - lows
        if len(price_spread_arr) >= window_len:
            price_spread = np.std(price_spread_arr[-window_len:])
        else:
            price_spread = np.std(price_spread_arr)
        
        # 누적 OBV
        v = np.zeros(len(closes))
        for i in range(1, len(closes)):
            sign = 1 if closes[i] > closes[i-1] else -1 if closes[i] < closes[i-1] else 0
            v[i] = v[i-1] + sign * volumes[i]
        
        # OBV smoothing
        smooth = sma(v, v_len)
        v_spread = np.std(v - smooth)
        
        if v_spread == 0:
            v_spread = 1  # ✅ 0 방지!
        
        # Shadow 계산
        shadow = (v - smooth) / v_spread * price_spread
        
        # Out 계산
        out = np.where(shadow > 0, highs + shadow, lows + shadow)
        
        # 3. OBV EMA (len=1, 거의 그대로)
        obvema = out  # ✅ len=1이므로 원본 사용!
        
        # 4. DEMA 계산 (MACD Fast Line)
        ma_fast = dema_np(obvema, 9)
        
        # 5. EMA 계산 (MACD Slow Line)
        ma_slow = ema_np(closes, 26)
        
        # 6. MACD 계산 (배열로!)
        macd_array = ma_fast - ma_slow  # ✅ 배열!
        
        # 7. Slope 계산 (Linear Regression)
        slope_len = 2
        slope, intercept = calc_slope(macd_array, slope_len)  # ✅ 배열 전달!
        tt1 = intercept + slope * (slope_len - 1)
        
        # 8. T-Channel 계산 (Trend Following)
        b = t_channel(tt1, symbol)  # ✅ symbol 추가!
        
        # 9. 심볼별 스케일링
        if symbol == "ARB_USDT":
            obv_macd_normalized = b * 1000.0  # ✅ ARB는 *1000
        else:  # PAXG_USDT
            obv_macd_normalized = b  # ✅ PAXG는 그대로
        
        obv_macd_value[symbol] = Decimal(str(obv_macd_normalized))
        
        log("📊 OBV", f"{symbol}: {float(obv_macd_value[symbol]):.6f} (×100: {float(obv_macd_value[symbol])*100:.2f})")
    
    except Exception as e:
        log("❌ OBV", f"{symbol} calculation error: {e}")


def sma(data, period):
    """Simple Moving Average"""
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(np.mean(data[:i+1]))
        else:
            result.append(np.mean(data[i-period+1:i+1]))
    return np.array(result)


def ema_np(data, period):
    """Exponential Moving Average"""
    k = 2 / (period + 1)
    ema = [np.mean(data[:period])]
    for price in data[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return np.array(ema)


def dema_np(data, period):
    """Double Exponential Moving Average"""
    ema1 = ema_np(data, period)
    ema2 = ema_np(ema1, period)
    return 2 * ema1 - ema2


def calc_slope(src, length):
    """Linear Regression Slope"""
    x = np.arange(1, length + 1)
    y = src[-length:] if len(src) >= length else src
    
    if len(y) < 2:
        return 0, src[-1] if len(src) > 0 else 0
    
    x = x[-len(y):]
    
    # 선형 회귀
    sum_x = np.sum(x)
    sum_y = np.sum(y)
    sum_x_sqr = np.sum(x * x)
    sum_xy = np.sum(x * y)
    
    n = len(y)
    slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x_sqr - sum_x * sum_x)
    average = sum_y / n
    intercept = average - slope * sum_x / n + slope
    
    return slope, intercept


# 전역 변수 (T-Channel)
t_channel_b = {symbol: 0.0 for symbol in SYMBOLS}
t_channel_dev = {symbol: 0.0 for symbol in SYMBOLS}
t_channel_oc = {symbol: 0 for symbol in SYMBOLS}
t_channel_n = {symbol: 0 for symbol in SYMBOLS}


def t_channel(src, symbol, p=1):
    """T-Channel (Trend Following)"""
    
    global t_channel_b, t_channel_dev, t_channel_oc, t_channel_n
    
    # 초기화
    if t_channel_n[symbol] == 0:
        t_channel_b[symbol] = src
        t_channel_n[symbol] = 1
        return src
    
    # 누적 카운트
    t_channel_n[symbol] += 1
    n = t_channel_n[symbol]
    
    # Average deviation
    a = abs(src - t_channel_b[symbol]) / n * p
    
    # Base update
    if src > t_channel_b[symbol] + a:
        t_channel_b[symbol] = src
    elif src < t_channel_b[symbol] - a:
        t_channel_b[symbol] = src
    
    # Deviation
    if t_channel_b[symbol] != t_channel_b.get(f"{symbol}_prev", t_channel_b[symbol]):
        t_channel_dev[symbol] = a
    
    t_channel_b[f"{symbol}_prev"] = t_channel_b[symbol]
    
    # Order change
    change_b = t_channel_b[symbol] - t_channel_b.get(f"{symbol}_prev2", t_channel_b[symbol])
    if change_b > 0:
        t_channel_oc[symbol] = 1
    elif change_b < 0:
        t_channel_oc[symbol] = -1
    
    t_channel_b[f"{symbol}_prev2"] = t_channel_b[symbol]
    
    return t_channel_b[symbol]


def fetch_kline_thread():
    """K-line 데이터 수집 (멀티 심볼)"""
    while True:
        try:
            for symbol in SYMBOLS:
                try:
                    candles = api.list_futures_candlesticks(
                        SETTLE,
                        contract=symbol,
                        interval="3m",
                        limit=200
                    )
                    
                    if candles:
                        kline_history[symbol].clear()
                        for c in candles:
                            kline_history[symbol].append([
                                int(c.t),
                                float(c.o),
                                float(c.c),
                                float(c.h),
                                float(c.l),
                                float(c.v)
                            ])
                        
                        calculate_obv_macd(symbol)
                
                except Exception as e:
                    log("❌ KLINE", f"{symbol} error: {e}")
            
            time.sleep(180)  # 3분마다
        
        except Exception as e:
            log("❌ KLINE", f"Thread error: {e}")
            time.sleep(60)


def calculate_obv_macd_weight(obv_display_abs):
    """OBV 추가 진입 비율 계산 (절댓값 기준)"""
    if obv_display_abs <= 20:
        return 0.10
    elif obv_display_abs <= 25:
        return 0.11
    elif obv_display_abs <= 30:
        return 0.12
    elif obv_display_abs <= 40:
        return 0.13
    elif obv_display_abs <= 50:
        return 0.15
    elif obv_display_abs <= 60:
        return 0.16
    elif obv_display_abs <= 70:
        return 0.17
    elif obv_display_abs <= 100:
        return 0.19
    else:
        return 0.20


# =============================================================================
# 동적 TP 계산 (심볼별)
# =============================================================================

def calculate_dynamic_tp_gap(symbol):
    """동적 TP 계산 (OBV MACD 기반)"""
    global tp_gap_long, tp_gap_short
    
    try:
        obv_display = float(obv_macd_value[symbol]) * 100
        obv_abs = abs(obv_display)
        
        # OBV에 따른 TP 강도
        if obv_abs < 10:
            tp_strength = TPMIN
        elif obv_abs < 20:
            tp_strength = Decimal("0.0026")
        elif obv_abs < 30:
            tp_strength = Decimal("0.0031")
        elif obv_abs < 40:
            tp_strength = Decimal("0.0036")
        else:
            tp_strength = TPMAX
        
        # 방향별 TP 적용
        if obv_display > 0:
            # 롱 강세 → SHORT 주력
            tp_gap_long[symbol] = tp_strength  # LONG은 순방향 TP
            tp_gap_short[symbol] = TPMIN       # SHORT은 안정화 TP
        elif obv_display < 0:
            # 숏 강세 → LONG 주력
            tp_gap_long[symbol] = TPMIN        # LONG은 안정화 TP
            tp_gap_short[symbol] = tp_strength # SHORT은 순방향 TP
        else:
            tp_gap_long[symbol] = TPMIN
            tp_gap_short[symbol] = TPMIN
        
        log("🎯 TP", f"{symbol}: LONG={float(tp_gap_long[symbol])*100:.2f}%, SHORT={float(tp_gap_short[symbol])*100:.2f}%")
    
    except Exception as e:
        log("❌ TP", f"{symbol} calculation error: {e}")


# =============================================================================
# Tier 전략 (물량 누적 방지) - 심볼별
# =============================================================================

def handle_non_main_position_tp(symbol, non_main_size_at_tp):
    """비주력 TP 체결 시 주력 청산 (Tier 전략)"""
    
    try:
        sync_position(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
            long_price = position_state[symbol]["long"]["entry_price"]
            short_price = position_state[symbol]["short"]["entry_price"]
        
        if long_size == 0 and short_size == 0:
            log("⚠️ TIER", f"{symbol}: No positions")
            return
        
        # 주력 포지션 판단
        current_price = get_current_price(symbol)
        if current_price <= 0:
            return
        
        current_price_dec = Decimal(str(current_price))
        long_value = long_size * long_price
        short_value = short_size * short_price
        
        if long_value > short_value:
            main_side = "LONG"
            non_main_side = "SHORT"
            main_position_value = long_value
            main_position_size = long_size
        else:
            main_side = "SHORT"
            non_main_side = "LONG"
            main_position_value = short_value
            main_position_size = short_size
        
        # Tier 설정 가져오기
        tier1_min = get_symbol_config(symbol, "tier1_min")
        tier1_max = get_symbol_config(symbol, "tier1_max")
        tier1_mult = get_symbol_config(symbol, "tier1_multiplier")
        tier2_mult = get_symbol_config(symbol, "tier2_multiplier")
        
        with balance_lock:
            balance = initial_capital
        
        # Tier 판정
        if balance * tier1_min <= main_position_value < balance * tier1_max:
            sl_qty = non_main_size_at_tp * tier1_mult  # ✅ int 제거!
            tier = f"Tier-1 ({float(tier1_min)}~{float(tier1_max)}배, {float(tier1_mult)}x)"
        else:
            sl_qty = non_main_size_at_tp * tier2_mult  # ✅ int 제거!
            tier = f"Tier-2 ({float(tier1_max)}배+, {float(tier2_mult)}x)"
        
        # ✅ 소수점 처리
        if sl_qty < Decimal("0.001"):
            sl_qty = Decimal("0.001")
        
        if sl_qty > main_position_size:
            sl_qty = main_position_size
        
        # ✅ 소수점 3자리로 반올림 (Gate.io 지원)
        sl_qty_rounded = round(float(sl_qty), 3)
        
        log("🔁 TIER", f"{symbol} {tier}: {non_main_side} TP {non_main_size_at_tp} → {main_side} SL {sl_qty_rounded}")
        
        # 주력 청산
        contract_qty = get_contract_size(symbol, sl_qty_rounded)
        order_size = -contract_qty if main_side == "LONG" else contract_qty
        
        order = FuturesOrder(
            contract=symbol,
            size=order_size,  # ✅ float 지원!
            price=0,
            tif="ioc",
            reduce_only=True,
            text=generate_order_id()
        )
        
        api.create_futures_order(SETTLE, order)
        log("✅ SL", f"{symbol} {main_side} {sl_qty_rounded} executed")
        
        time.sleep(0.2)
        sync_position(symbol)
        refresh_all_tp_orders(symbol)
    
    except Exception as e:
        log("❌ TIER", f"{symbol} error: {e}")


# =============================================================================
# TP 주문 관리 (심볼별)
# =============================================================================

def refresh_all_tp_orders(symbol):
    """TP 주문 갱신 (완전판)"""
    
    try:
        # 1. 포지션 동기화
        sync_position(symbol)
        calculate_dynamic_tp_gap(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
            long_entry = position_state[symbol]["long"]["entry_price"]
            short_entry = position_state[symbol]["short"]["entry_price"]
        
        if long_size == 0 and short_size == 0:
            return
        
        # 2. 기존 TP 완전 제거
        cancel_tp_only(symbol)
        time.sleep(0.5)
        
        # 3. 추가 확인 (3회 반복)
        for attempt in range(3):
            try:
                orders = api.list_futures_orders(SETTLE, contract=symbol, status='open')  # ✅ 수정!
                has_tp = False
                for order in orders:
                    is_reduce = getattr(order, 'reduce_only', False) or getattr(order, 'is_reduce_only', False)
                    if is_reduce:
                        has_tp = True
                        try:
                            api.cancel_futures_order(SETTLE, order.id)  # ✅ 수정!
                            log("🗑️ TP_RETRY", f"{symbol}: Removed pending TP {order.id} (attempt {attempt+1})")
                        except:
                            pass
                
                if not has_tp:
                    break
                
                time.sleep(0.5)
            except:
                break
        
        # 4. LONG TP 생성
        if long_size > 0 and long_entry > 0:
            try:
                tp_price_long = long_entry * (Decimal("1") + tp_gap_long[symbol])
                tp_price_long_rounded = tp_price_long.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                
                tp_size_long = -get_contract_size(symbol, float(long_size))
                
                order = FuturesOrder(
                    contract=symbol,
                    size=tp_size_long,  
                    price=str(tp_price_long_rounded),
                    tif="gtc",
                    reduce_only=True,
                    text=generate_order_id()
                )
                
                result = api.create_futures_order(SETTLE, order)
                average_tp_orders[symbol]["long"] = result.id
                
                tp_pct = float(tp_gap_long[symbol]) * 100
                log("📈 TP", f"{symbol} LONG {long_size} @ {tp_price_long_rounded} ({tp_pct:.2f}%)")
            
            except GateApiException as e:
                log("❌ TP", f"{symbol} LONG TP creation failed: {e}")
            except Exception as e:
                log("❌ TP", f"{symbol} LONG TP error: {e}")
        
        # 5. SHORT TP 생성
        if short_size > 0 and short_entry > 0:
            try:
                tp_price_short = short_entry * (Decimal("1") - tp_gap_short[symbol])
                tp_price_short_rounded = tp_price_short.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                
                tp_size_short = get_contract_size(symbol, float(short_size))
                
                order = FuturesOrder(
                    contract=symbol,
                    size=tp_size_short,
                    price=str(tp_price_short_rounded),
                    tif="gtc",
                    reduce_only=True,
                    text=generate_order_id()
                )
                
                result = api.create_futures_order(SETTLE, order)
                average_tp_orders[symbol]["short"] = result.id
                
                tp_pct = float(tp_gap_short[symbol]) * 100
                log("📉 TP", f"{symbol} SHORT {short_size} @ {tp_price_short_rounded} ({tp_pct:.2f}%)")
            
            except GateApiException as e:
                log("❌ TP", f"{symbol} SHORT TP creation failed: {e}")
            except Exception as e:
                log("❌ TP", f"{symbol} SHORT TP error: {e}")
    
    except Exception as e:
        log("❌ TP", f"{symbol} refresh error: {e}")

def cancel_all_orders(symbol):
    """모든 주문 취소"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=symbol, status='open')
        for order in orders:
            try:
               api.cancel_futures_order(SETTLE, order.id)
            except:
                pass
        
        grid_orders[symbol]["long"] = []
        grid_orders[symbol]["short"] = []
        average_tp_orders[symbol]["long"] = None
        average_tp_orders[symbol]["short"] = None
        
        log("🗑️ CANCEL", f"{symbol}: All orders cancelled")
    except Exception as e:
        log("❌ CANCEL", f"{symbol} error: {e}")


def cancel_tp_only(symbol):
    """TP 주문만 취소 (완전판)"""
    try:
        # 1. 메모리 ID로 취소
        for side in ["long", "short"]:
            tp_id = average_tp_orders[symbol].get(side)
            if tp_id:
                try:
                    api.cancel_futures_order(SETTLE, tp_id)  # ✅ 수정!
                    average_tp_orders[symbol][side] = None
                except:
                    pass
        
        time.sleep(0.5)  # 0.3 → 0.5초로 증가
        
        # 2. 모든 reduce_only 주문 제거 (완전히!)
        try:
            orders = api.list_futures_orders(SETTLE, contract=symbol, status='open')  # ✅ 수정!
            for order in orders:
                # reduce_only 체크
                is_reduce = False
                if hasattr(order, 'reduce_only'):
                    is_reduce = order.reduce_only
                elif hasattr(order, 'is_reduce_only'):
                    is_reduce = order.is_reduce_only
                
                if is_reduce:
                    try:
                        api.cancel_futures_order(SETTLE, order.id)  # ✅ 수정!
                        log("🗑️ TP_REMOVE", f"{symbol}: Removed pending TP {order.id}")
                    except:
                        pass
            
            time.sleep(0.5)  # 추가 대기
        except:
            pass
    
    except Exception as e:
        log("❌ CANCEL_TP", f"{symbol} error: {e}")


# =============================================================================
# 초기 진입 (심볼별)
# =============================================================================

def initialize_grid(symbol, current_price=None):
    """그리드 초기화 (완전판)"""
    
    try:
        sync_position(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
        
        if long_size > 0 or short_size > 0:
            log("⚠️ GRID", f"{symbol}: Already has positions (L={long_size}, S={short_size})")
            return
        
        if current_price is None:
            current_price = get_current_price(symbol)
        
        if current_price <= 0:
            log("❌ GRID", f"{symbol}: Invalid price ({current_price})")
            return
        
        current_price_dec = Decimal(str(current_price))
        
        calculate_dynamic_tp_gap(symbol)
        
        # OBV 가중
        obv_display = float(obv_macd_value[symbol]) * 100
        obv_abs = abs(obv_display)
        obv_weight = Decimal(str(calculate_obv_macd_weight(obv_abs)))
        
        # 기본 수량 (✅ int 제거!)
        base_ratio = get_symbol_config(symbol, "base_ratio")
        with balance_lock:
            base_value = initial_capital * base_ratio
        
        base_qty = base_value / current_price_dec  # ✅ Decimal 유지!
        
        # ✅ 최소 수량 보장
        if base_qty < Decimal("0.001"):
            base_qty = Decimal("0.001")
        
        # 역추세 진입 (✅ int 제거!)
        if obv_display > 0:
            short_qty = base_qty * (Decimal("1") + obv_weight)
            long_qty = base_qty * HEDGE_RATIO_MAIN if ENABLE_AUTO_HEDGE else base_qty
        elif obv_display < 0:
            long_qty = base_qty * (Decimal("1") + obv_weight)
            short_qty = base_qty * HEDGE_RATIO_MAIN if ENABLE_AUTO_HEDGE else base_qty
        else:
            long_qty = base_qty
            short_qty = base_qty
        
        # ✅ 최소 수량 보장 (다시 한번!)
        if long_qty < Decimal("0.001"):
            long_qty = Decimal("0.001")
        if short_qty < Decimal("0.001"):
            short_qty = Decimal("0.001")
        
        with balance_lock:
            long_value = long_qty * current_price_dec
            short_value = short_qty * current_price_dec
            max_value = initial_capital * MAXPOSITIONRATIO
        
        if long_value >= max_value or short_value >= max_value:
            log("⚠️ GRID", f"{symbol}: Exceeds max position (L:{long_value:.2f}, S:{short_value:.2f}, Max:{max_value:.2f})")
            return
        
        log("🔷 GRID", f"{symbol} OBV={obv_display:.2f}%, LONG={long_qty}, SHORT={short_qty}")
        
        # LONG 진입 (✅ int 제거!)
        if long_qty > 0:
            try:
                contract_qty = get_contract_size(symbol, float(long_qty))  # ✅ int 제거!
                
                order = FuturesOrder(
                    contract=symbol,
                    size=contract_qty,  # ✅ float 지원!
                    price=0,
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, order)
                log("✅ ENTRY", f"{symbol} LONG {long_qty} (Contract: {contract_qty})")
            except GateApiException as e:
                log("❌ ENTRY", f"{symbol} LONG error: {e}")
                return
        
        time.sleep(0.1)
        
        # SHORT 진입 (✅ int 제거!)
        if short_qty > 0:
            try:
                contract_qty = get_contract_size(symbol, float(short_qty))  # ✅ int 제거!
                
                order = FuturesOrder(
                    contract=symbol,
                    size=-contract_qty,  # ✅ float 지원!
                    price=0,
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, order)
                log("✅ ENTRY", f"{symbol} SHORT {short_qty} (Contract: {contract_qty})")
            except GateApiException as e:
                log("❌ ENTRY", f"{symbol} SHORT error: {e}")
                return
        
        time.sleep(0.2)
        sync_position(symbol)
        refresh_all_tp_orders(symbol)
        
        last_event_time[symbol] = time.time()
    
    except Exception as e:
        log("❌ GRID", f"{symbol} error: {e}")


# =============================================================================
# 불균형 헤징 (심볼별)
# =============================================================================

def market_entry_when_imbalanced(symbol):
    """불균형 발생 시 자동 헤징 (멀티 심볼 지원)"""
    
    if not ENABLE_AUTO_HEDGE:
        return
    
    try:
        sync_position(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
            long_price = position_state[symbol]["long"]["entry_price"]
            short_price = position_state[symbol]["short"]["entry_price"]
        
        if long_size == 0 and short_size == 0:
            return
        
        # 불균형 체크
        long_value = long_size * long_price
        short_value = short_size * short_price
        
        total_value = long_value + short_value
        if total_value == 0:
            return
        
        long_ratio = float(long_value / total_value)
        short_ratio = float(short_value / total_value)
        
        imbalance_threshold = 0.60  # 60:40 이상 차이
        
        if abs(long_ratio - short_ratio) < (imbalance_threshold - 0.5) * 2:
            return
        
        # 부족한 쪽 판단
        if long_value > short_value:
            missing_side = "SHORT"
            missing_qty = long_size - short_size
        else:
            missing_side = "LONG"
            missing_qty = short_size - long_size
        
        # 헤징 수량 계산
        hedge_ratio = get_symbol_config(symbol, "hedge_ratio_main")
        hedge_qty = abs(missing_qty) * hedge_ratio
        
        # ✅ 최소 수량 보장
        if hedge_qty < Decimal("0.001"):
            hedge_qty = Decimal("0.001")
        
        # ✅ 소수점 3자리로 반올림
        hedge_qty_rounded = round(float(hedge_qty), 3)
        
        # ✅ 최소값 재확인
        if hedge_qty_rounded < 0.001:
            hedge_qty_rounded = 0.001
        
        log("🔁 HEDGE", f"{symbol}: Imbalanced, adding {missing_side} {hedge_qty_rounded}")
        
        # 헤징 진입
        order_size = hedge_qty_rounded if missing_side == "LONG" else -hedge_qty_rounded
        
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price=0,
            tif="ioc",
            reduce_only=False,
            text=generate_order_id()
        )
        
        api.create_futures_order(SETTLE, order)
        log("✅ HEDGE", f"{symbol} {missing_side} {hedge_qty_rounded} executed")
        
        time.sleep(0.2)
        sync_position(symbol)
        refresh_all_tp_orders(symbol)
        
        last_event_time[symbol] = time.time()
    
    except Exception as e:
        log("❌ HEDGE", f"{symbol} error: {e}")


# =============================================================================
# 아이들 진입 (심볼별)
# =============================================================================

def check_idle_and_enter(symbol):
    """10분 무활동 시 아이들 진입"""
    
    if idle_entry_in_progress[symbol]:
        log("⚠️ IDLE", f"{symbol}: Entry in progress")
        return
    
    try:
        now = time.time()
        
        if now - last_idle_check.get(symbol, 0) < 60:
            return
        last_idle_check[symbol] = now
        
        if idle_entry_count[symbol] >= MAX_IDLE_ENTRIES:
            return
        
        sync_position(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
            long_price = position_state[symbol]["long"]["entry_price"]
            short_price = position_state[symbol]["short"]["entry_price"]
        
        if long_size == 0 and short_size == 0:
            return
        
        time_since_last = now - last_event_time.get(symbol, now)
        
        log("🔍 IDLE_CHECK", f"{symbol}: L={long_size}, S={short_size}, Last={time_since_last:.1f}s, Need={IDLE_TIMEOUT}s")
        
        if time_since_last < IDLE_TIMEOUT:
            log("⏳ IDLE", f"{symbol}: Waiting {IDLE_TIMEOUT - time_since_last:.1f}s more")
            return
        
        with balance_lock:
            max_value = initial_capital * MAXPOSITIONRATIO
        
        current_price = get_current_price(symbol)
        if current_price <= 0:
            return
        
        current_price_dec = Decimal(str(current_price))
        
        long_value = long_size * long_price
        short_value = short_size * short_price
        
        if long_value >= max_value or short_value >= max_value:
            log("⚠️ IDLE", f"{symbol}: Max position reached (L:{long_value:.2f}, S:{short_value:.2f}, Max:{max_value:.2f})")
            return
        
        idle_entry_in_progress[symbol] = True
        
        # 손실도 계산
        pnl_long = (current_price_dec - long_price) * long_size
        pnl_short = (short_price - current_price_dec) * short_size
        total_pnl = pnl_long + pnl_short
        
        with balance_lock:
            balance = initial_capital
        
        loss_pct = (float(total_pnl) / float(balance)) * 100 if balance > 0 else 0
        
        # 기본 수량 (✅ int 제거!)
        base_ratio = get_symbol_config(symbol, "base_ratio")
        with balance_lock:
            base_value = initial_capital * base_ratio
        
        base_qty = base_value / current_price_dec  # ✅ Decimal 유지!
        
        # ✅ 최소 수량 보장
        if base_qty < Decimal("0.001"):
            base_qty = Decimal("0.001")
        
        # 손실 가중 (✅ int 제거!)
        adjusted_qty = base_qty * (Decimal("1") + Decimal(str(loss_pct)) / Decimal("100"))
        
        # OBV 가중
        obv_display = float(obv_macd_value[symbol]) * 100
        obv_abs = abs(obv_display)
        obv_weight = Decimal(str(calculate_obv_macd_weight(obv_abs)))
        
        # 수량 배분 (✅ int 제거!)
        if obv_display > 0:
            short_qty = adjusted_qty * (Decimal("1") + obv_weight)
            long_qty = adjusted_qty
        elif obv_display < 0:
            long_qty = adjusted_qty * (Decimal("1") + obv_weight)
            short_qty = adjusted_qty
        else:
            long_qty = adjusted_qty
            short_qty = adjusted_qty
        
        # ✅ 최소 수량 보장 (다시 한번!)
        if long_qty < Decimal("0.001"):
            long_qty = Decimal("0.001")
        if short_qty < Decimal("0.001"):
            short_qty = Decimal("0.001")
        
        idle_entry_count[symbol] += 1
        log("⏰ IDLE", f"{symbol} #{idle_entry_count[symbol]}: Loss={loss_pct:.2f}%, LONG={long_qty}, SHORT={short_qty}")
        
        # LONG 진입 (✅ int 제거!)
        try:
            if float(long_qty) > 0:
                contract_qty = get_contract_size(symbol, float(long_qty))  # ✅ int 제거!
                
                if contract_qty < 0.001:  # ✅ 0.001!
                    contract_qty = 0.001
                
                order = FuturesOrder(
                    contract=symbol,
                    size=contract_qty,  # ✅ float 지원!
                    price=0,
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, order)
                log("✅ IDLE_LONG", f"{symbol}: {long_qty} ({contract_qty} qty)")
        except Exception as e:
            log("❌ IDLE", f"{symbol} LONG error: {e}")
        
        time.sleep(0.1)
        
        # SHORT 진입 (✅ int 제거!)
        try:
            if float(short_qty) > 0:
                contract_qty = get_contract_size(symbol, float(short_qty))  # ✅ int 제거!
                
                if contract_qty < 0.001:  # ✅ 0.001!
                    contract_qty = 0.001
                
                order = FuturesOrder(
                    contract=symbol,
                    size=-contract_qty,  # ✅ float 지원!
                    price=0,
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, order)
                log("✅ IDLE_SHORT", f"{symbol}: {short_qty} ({contract_qty} qty)")
        except Exception as e:
            log("❌ IDLE", f"{symbol} SHORT error: {e}")
        
        time.sleep(0.2)
        sync_position(symbol)
        refresh_all_tp_orders(symbol)
        
        last_event_time[symbol] = time.time()
    
    except Exception as e:
        log("❌ IDLE", f"{symbol} error: {e}")
    
    finally:
        idle_entry_in_progress[symbol] = False


# =============================================================================
# 검증 및 헬스 체크
# =============================================================================

def validate_strategy_consistency(symbol):
    """전략 일관성 검증"""
    
    try:
        sync_position(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
            long_price = position_state[symbol]["long"]["entry_price"]
            short_price = position_state[symbol]["short"]["entry_price"]
        
        current_price = get_current_price(symbol)
        if current_price <= 0:
            return
        
        current_price_dec = Decimal(str(current_price))
        
        long_value = long_size * long_price
        short_value = short_size * short_price
        
        # 최대 한도 초과 체크
        with balance_lock:
            max_value = initial_capital * MAXPOSITIONRATIO
               
        # 단일 포지션 + 주문 없음 → 헤징 또는 그리드 생성
        if (long_size > 0 and short_size == 0) or (long_size == 0 and short_size > 0):
            orders = api.list_futures_orders(SETTLE, contract=symbol, status='open')
            if len(orders) == 0:
                if ENABLE_AUTO_HEDGE:
                    log("⚠️ VALIDATE", f"{symbol}: Single position detected, hedging")
                    market_entry_when_imbalanced(symbol)
                else:
                    log("⚠️ VALIDATE", f"{symbol}: Single position detected, creating grid")
                    initialize_grid(symbol, current_price)
    
    except Exception as e:
        log("❌ VALIDATE", f"{symbol} error: {e}")


def remove_duplicate_orders(symbol):
    """중복 주문 제거"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=symbol, status='open')
        
        # 가격별 주문 그룹화
        price_groups = {}
        for order in orders:
            price = order.price
            if price not in price_groups:
                price_groups[price] = []
            price_groups[price].append(order)
        
        # 중복 제거
        for price, group in price_groups.items():
            if len(group) > 1:
                for order in group[1:]:
                    try:
                        api.cancel_futures_order(SETTLE, order.id)
                        log("🗑️ DUP", f"{symbol}: Removed duplicate @ {price}")
                    except:
                        pass
    
    except Exception as e:
        log("❌ DUP", f"{symbol} error: {e}")



def check_tp_hash_and_refresh(symbol):
    """TP 주문 해시 확인 및 갱신"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=symbol, status='open')
        
        # TP 주문만 필터
        tp_orders = [o for o in orders if o.reduce_only]
        
        if len(tp_orders) == 0:
            with position_lock:
                long_size = position_state[symbol]["long"]["size"]
                short_size = position_state[symbol]["short"]["size"]
            
            if long_size > 0 or short_size > 0:
                log("⚠️ TP_HASH", f"{symbol}: No TP orders, refreshing")
                refresh_all_tp_orders(symbol)
            return
        
        # 해시 계산
        tp_prices = sorted([float(o.price) for o in tp_orders])
        tp_hash = hashlib.md5(str(tp_prices).encode()).hexdigest()
        
        if last_tp_hash[symbol] != tp_hash:
            last_tp_hash[symbol] = tp_hash
            log("🔄 TP_HASH", f"{symbol}: Updated ({tp_hash[:8]})")
    
    except Exception as e:
        log("❌ TP_HASH", f"{symbol} error: {e}")


def check_obv_change_and_refresh_tp(symbol):
    """OBV 변화 감지 및 TP 갱신"""
    try:
        obv_display = float(obv_macd_value[symbol]) * 100
        last_obv = last_adjusted_obv[symbol]
        
        obv_change = abs(obv_display - last_obv)
        
        if obv_change >= OBV_CHANGE_THRESHOLD:
            log("🔄 OBV_CHANGE", f"{symbol}: {last_obv:.1f} → {obv_display:.1f} (Δ{obv_change:.1f})")
            
            with position_lock:
                long_size = position_state[symbol]["long"]["size"]
                short_size = position_state[symbol]["short"]["size"]
            
            if long_size > 0 or short_size > 0:
                refresh_all_tp_orders(symbol)
                last_adjusted_obv[symbol] = obv_display
    
    except Exception as e:
        log("❌ OBV_CHANGE", f"{symbol} error: {e}")


def periodic_health_check():
    """2분마다 헬스 체크 (모든 심볼)"""
    
    while True:
        try:
            time.sleep(120)  # 2분
            
            # 계정 잔고 갱신 (공유)
            update_account_balance()
            
            # 각 심볼별 헬스 체크
            for symbol in SYMBOLS:
                try:
                    sync_position(symbol)
                    check_tp_hash_and_refresh(symbol)
                    check_obv_change_and_refresh_tp(symbol)
                    validate_strategy_consistency(symbol)
                    remove_duplicate_orders(symbol)
                    market_entry_when_imbalanced(symbol)
                    check_idle_and_enter(symbol)
                    log_position_state(symbol)
                except Exception as e:
                    log("❌ HEALTH", f"{symbol} error: {e}")
        
        except Exception as e:
            log("❌ HEALTH", f"Loop error: {e}")


# =============================================================================
# WebSocket (멀티 심볼)
# =============================================================================

async def grid_fill_monitor():
    """Futures WebSocket - 멀티 심볼 주문 체결 감지"""
    
    uri = f"wss://fx-ws.gateio.ws/v4/ws/{SETTLE}"
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                # 인증 메시지 생성
                timestamp = int(time.time())
                signature_string = f"channel=futures.orders&event=subscribe&time={timestamp}"
                signature = hashlib.sha512((signature_string + "\n" + API_SECRET).encode()).hexdigest()
                
                # 모든 심볼 구독
                for symbol in SYMBOLS:
                    auth_msg = {
                        "time": timestamp,
                        "channel": "futures.orders",
                        "event": "subscribe",
                        "payload": [symbol],
                        "auth": {
                            "method": "api_key",
                            "KEY": API_KEY,
                            "SIGN": signature
                        }
                    }
                    await ws.send(json.dumps(auth_msg))
                    log("🔌 WS", f"Subscribed to {symbol}")
                
                await asyncio.sleep(1)
                
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    
                    if data.get("event") == "update" and data.get("channel") == "futures.orders":
                        result = data.get("result", [])
                        
                        for order_data in result:
                            contract = order_data.get("contract")
                            if contract not in SYMBOLS:
                                continue
                            
                            status = order_data.get("status")
                            finish_as = order_data.get("finish_as")
                            
                            # TP 체결 감지
                            if status == "finished" and finish_as == "filled":
                                size = int(order_data.get("size", 0))
                                
                                sync_position(contract)
                                
                                with position_lock:
                                    long_size = position_state[contract]["long"]["size"]
                                    short_size = position_state[contract]["short"]["size"]
                                
                                # TP 체결 판단
                                if size < 0 and long_size == 0:
                                    tp_qty = abs(size)
                                    log("🎯 TP", f"{contract} LONG TP {tp_qty} filled")
                                    asyncio.create_task(async_handle_tp(contract, tp_qty))
                                    last_event_time[contract] = time.time()
                                
                                elif size > 0 and short_size == 0:
                                    tp_qty = abs(size)
                                    log("🎯 TP", f"{contract} SHORT TP {tp_qty} filled")
                                    asyncio.create_task(async_handle_tp(contract, tp_qty))
                                    last_event_time[contract] = time.time()
        
        except Exception as e:
            log("❌ WS", f"Error: {e}")
            await asyncio.sleep(5)


async def async_handle_tp(symbol, tp_qty):
    """TP 체결 비동기 처리"""
    await asyncio.sleep(0.1)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, handle_non_main_position_tp, symbol, tp_qty)


# =============================================================================
# Flask 엔드포인트
# =============================================================================

@app.route('/webhook', methods=['POST'])
def webhook():
    """TradingView webhook - 멀티 심볼 지원"""
    global obv_macd_value
    
    try:
        data = request.get_json()
        symbol = data.get('symbol', 'ARB_USDT')
        tt1 = data.get('tt1', 0)
        
        if symbol not in SYMBOLS:
            return jsonify({"status": "error", "message": f"Invalid symbol: {symbol}"}), 400
        
        # OBV MACD 저장 (×1000 스케일)
        obv_macd_value[symbol] = Decimal(str(tt1 / 1000.0))
        
        log("📨 WEBHOOK", f"{symbol}: OBV MACD={tt1:.2f} → {float(obv_macd_value[symbol]):.6f}")
        
        return jsonify({
            "status": "success",
            "symbol": symbol,
            "tt1": float(tt1),
            "stored": float(obv_macd_value[symbol])
        }), 200
    
    except Exception as e:
        log("❌ WEBHOOK", f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/status', methods=['GET'])
def status():
    """봇 상태 조회 (멀티 심볼)"""
    try:
        status_data = {
            "symbols": SYMBOLS,
            "initial_capital": float(initial_capital),
            "max_position_ratio": float(MAXPOSITIONRATIO),
            "positions": {}
        }
        
        for symbol in SYMBOLS:
            with position_lock:
                pos = position_state[symbol]
            
            config = SYMBOL_CONFIG[symbol]
            
            status_data["positions"][symbol] = {
                "long": {
                    "size": float(pos["long"]["size"]),
                    "entry_price": float(pos["long"]["entry_price"])
                },
                "short": {
                    "size": float(pos["short"]["size"]),
                    "entry_price": float(pos["short"]["entry_price"])
                },
                "obv_macd": float(obv_macd_value[symbol]),
                "tp_long": float(tp_gap_long[symbol]) * 100,
                "tp_short": float(tp_gap_short[symbol]) * 100,
                "idle_count": idle_entry_count[symbol],
                "config": {
                    "base_ratio": float(config["base_ratio"]) * 100,
                    "tier1": f"{float(config['tier1_min'])}~{float(config['tier1_max'])}배 ({float(config['tier1_multiplier'])}x)",
                    "tier2": f"{float(config['tier1_max'])}배+ ({float(config['tier2_multiplier'])}x)"
                }
            }
        
        return jsonify(status_data), 200
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/manual_entry/<symbol>', methods=['POST'])
def manual_entry(symbol):
    """수동 진입"""
    if symbol not in SYMBOLS:
        return jsonify({"status": "error", "message": "Invalid symbol"}), 400
    
    try:
        current_price = get_current_price(symbol)
        initialize_grid(symbol, current_price)
        return jsonify({"status": "success", "symbol": symbol}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/cancel_all/<symbol>', methods=['POST'])
def cancel_all_endpoint(symbol):
    """모든 주문 취소"""
    if symbol not in SYMBOLS:
        return jsonify({"status": "error", "message": "Invalid symbol"}), 400
    
    try:
        cancel_all_orders(symbol)
        return jsonify({"status": "success", "symbol": symbol}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({"status": "ok"}), 200


# =============================================================================
# 초기화 및 메인
# =============================================================================

def print_startup_summary():
    """시작 요약"""
    log("=" * 70, "")
    log("🚀 START", "Multi-Symbol Trading Bot v30.0 (Complete Edition)")
    log("=" * 70, "")
    log("📊 SYMBOLS", f"{', '.join(SYMBOLS)}")
    log("💰 CAPITAL", f"{initial_capital} USDT")
    log("📏 MAX POSITION", f"{float(MAXPOSITIONRATIO)}배 (계정 전체 기준)")
    log("⚙️ AUTO HEDGE", f"{'Enabled' if ENABLE_AUTO_HEDGE else 'Disabled'} ({float(HEDGE_RATIO_MAIN)*100}%)")
    log("=" * 70, "")
    
    for symbol in SYMBOLS:
        config = SYMBOL_CONFIG[symbol]
        log("⚙️ CONFIG", f"{symbol}:")
        log("  ", f"  Base Ratio: {float(config['base_ratio'])*100}%")
        log("  ", f"  Tier-1: {float(config['tier1_min'])}~{float(config['tier1_max'])}배 ({float(config['tier1_multiplier'])}x)")
        log("  ", f"  Tier-2: {float(config['tier1_max'])}배+ ({float(config['tier2_multiplier'])}x)")
    
    log("=" * 70, "")


def main():
    """메인 함수"""
    
    # Initial Capital 로드
    if not load_initial_capital():
        try:
            futures_account = api.list_futures_accounts(SETTLE)
            if futures_account:
                available_str = getattr(futures_account, 'available', None)
                if available_str:
                    global initial_capital, account_balance
                    with balance_lock:
                        initial_capital = Decimal(str(available_str))
                        account_balance = initial_capital
                    save_initial_capital()
                    log("💰 INIT", f"Initial Capital: {initial_capital} USDT")
        except Exception as e:
            log("❌ INIT", f"Failed to get initial capital: {e}")
    
    # 초기 동기화
    for symbol in SYMBOLS:
        sync_position(symbol)
        log_position_state(symbol)
    
    print_startup_summary()
    
    # K-line 스레드
    kline_thread = threading.Thread(target=fetch_kline_thread, daemon=True)
    kline_thread.start()
    log("✅ THREAD", "K-line fetcher started")
    
    # Health Check 스레드
    health_thread = threading.Thread(target=periodic_health_check, daemon=True)
    health_thread.start()
    log("✅ THREAD", "Health checker started")
    
    # WebSocket 스레드
    def run_websocket():
        asyncio.run(grid_fill_monitor())
    
    ws_thread = threading.Thread(target=run_websocket, daemon=True)
    ws_thread.start()
    log("✅ THREAD", "WebSocket monitor started")
    
    # Flask 실행
    port = int(os.environ.get("PORT", 8080))
    log("🌐 FLASK", f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
