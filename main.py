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
        "base_ratio": Decimal("0.03"),      # 3%
        "tier1_min": Decimal("2"),        # Tier-1 시작
        "tier1_max": Decimal("3.0"),        # Tier-1 종료
        "tier1_multiplier": Decimal("0.8"), # Tier-1 청산 배수
        "tier2_multiplier": Decimal("1.5")  # Tier-2 청산 배수
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

# =============================================================================
# 헬퍼 함수 (Helper Functions)
# =============================================================================

def log(tag, message):
    """통합 로그 함수"""
    logger.info(f"[{tag}] {message}")


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
                
                size = int(pos.size)
                entry_price = Decimal(str(pos.entry_price)) if pos.entry_price else Decimal("0")
                
                with position_lock:
                    if size > 0:
                        position_state[contract]["long"]["size"] = Decimal(str(size))
                        position_state[contract]["long"]["entry_price"] = entry_price
                    elif size < 0:
                        position_state[contract]["short"]["size"] = Decimal(str(abs(size)))
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
    """OBV MACD 자체 계산 (3분봉 기준)"""
    global obv_macd_value
    
    try:
        if len(kline_history[symbol]) < 60:
            log("⏳ OBV", f"{symbol}: Not enough data ({len(kline_history[symbol])}/60)")
            return
        
        klines = list(kline_history[symbol])
        
        # OBV 계산
        obv = [0.0]
        for i in range(1, len(klines)):
            close_prev = float(klines[i-1][2])
            close_curr = float(klines[i][2])
            volume = float(klines[i][5])
            
            if close_curr > close_prev:
                obv.append(obv[-1] + volume)
            elif close_curr < close_prev:
                obv.append(obv[-1] - volume)
            else:
                obv.append(obv[-1])
        
        # EMA 계산
        def ema(data, period):
            k = 2 / (period + 1)
            ema_values = [sum(data[:period]) / period]
            for price in data[period:]:
                ema_values.append(price * k + ema_values[-1] * (1 - k))
            return ema_values
        
        # MACD 계산
        ema_fast = ema(obv, 12)
        ema_slow = ema(obv, 26)
        
        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(ema_slow))]
        signal_line = ema(macd_line, 9)
        
        # OBV MACD
        obv_macd = macd_line[-1] - signal_line[-1]
        
        # 정규화 (×1000)
        obv_macd_normalized = obv_macd / 1000.0
        
        obv_macd_value[symbol] = Decimal(str(obv_macd_normalized))
        
        log("📈 OBV", f"{symbol}: {float(obv_macd_value[symbol]):.6f} (×100: {float(obv_macd_value[symbol])*100:.2f})")
    
    except Exception as e:
        log("❌ OBV", f"{symbol} calculation error: {e}")


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
            sl_qty = int(non_main_size_at_tp * tier1_mult)
            tier = f"Tier-1 ({float(tier1_min)}~{float(tier1_max)}배, {float(tier1_mult)}x)"
        else:
            sl_qty = int(non_main_size_at_tp * tier2_mult)
            tier = f"Tier-2 ({float(tier1_max)}배+, {float(tier2_mult)}x)"
        
        # 안전장치
        if sl_qty < 1:
            sl_qty = 1
        
        if sl_qty > main_position_size:
            sl_qty = int(main_position_size)
        
        log("🔁 TIER", f"{symbol} {tier}: {non_main_side} TP {non_main_size_at_tp} → {main_side} SL {sl_qty}")
        
        # 주력 청산
        order_size = -sl_qty if main_side == "LONG" else sl_qty
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price=0,
            tif="ioc",
            reduce_only=True,
            text=generate_order_id()
        )
        
        api.create_futures_order(SETTLE, order)
        log("✅ SL", f"{symbol} {main_side} {sl_qty} executed")
        
        time.sleep(0.2)
        sync_position(symbol)
        refresh_all_tp_orders(symbol)
    
    except Exception as e:
        log("❌ TIER", f"{symbol} error: {e}")


# =============================================================================
# TP 주문 관리 (심볼별)
# =============================================================================

def refresh_all_tp_orders(symbol):
    """TP 주문 갱신"""
    
    try:
        sync_position(symbol)
        calculate_dynamic_tp_gap(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
            long_entry = position_state[symbol]["long"]["entry_price"]
            short_entry = position_state[symbol]["short"]["entry_price"]
        
        if long_size == 0 and short_size == 0:
            return
        
        # 기존 TP 취소
        cancel_tp_only(symbol)
        time.sleep(0.1)
        
        # LONG TP
        if long_size > 0 and long_entry > 0:
            tp_price_long = long_entry * (Decimal("1") + tp_gap_long[symbol])
            tp_price_long_rounded = tp_price_long.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            
            order = FuturesOrder(
                contract=symbol,
                size=-int(long_size),
                price=str(tp_price_long_rounded),
                tif="gtc",
                reduce_only=True,
                text=generate_order_id()
            )
            
            result = api.create_futures_order(SETTLE, order)
            average_tp_orders[symbol]["long"] = result.id
            log("📈 TP", f"{symbol} LONG {long_size} @ {tp_price_long_rounded} ({float(tp_gap_long[symbol])*100:.2f}%)")
        
        # SHORT TP
        if short_size > 0 and short_entry > 0:
            tp_price_short = short_entry * (Decimal("1") - tp_gap_short[symbol])
            tp_price_short_rounded = tp_price_short.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            
            order = FuturesOrder(
                contract=symbol,
                size=int(short_size),
                price=str(tp_price_short_rounded),
                tif="gtc",
                reduce_only=True,
                text=generate_order_id()
            )
            
            result = api.create_futures_order(SETTLE, order)
            average_tp_orders[symbol]["short"] = result.id
            log("📉 TP", f"{symbol} SHORT {short_size} @ {tp_price_short_rounded} ({float(tp_gap_short[symbol])*100:.2f}%)")
    
    except Exception as e:
        log("❌ TP", f"{symbol} refresh error: {e}")


def cancel_all_orders(symbol):
    """모든 주문 취소"""
    try:
        orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
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
    """TP 주문만 취소"""
    try:
        for side in ["long", "short"]:
            tp_id = average_tp_orders[symbol].get(side)
            if tp_id:
                try:
                    api.cancel_futures_order(SETTLE, tp_id)
                    average_tp_orders[symbol][side] = None
                except:
                    pass
    except Exception as e:
        log("❌ CANCEL_TP", f"{symbol} error: {e}")


# =============================================================================
# 초기 진입 (심볼별)
# =============================================================================

def initialize_grid(symbol, current_price=None):
    """초기 그리드 생성 (역추세 진입)"""
    
    if not initialize_grid_lock.acquire(blocking=False):
        log("⚠️ GRID", f"{symbol}: Already running")
        return
    
    try:
        now = time.time()
        if now - last_grid_time.get(symbol, 0) < 10:
            log("⚠️ GRID", f"{symbol}: Too soon")
            return
        last_grid_time[symbol] = now
        
        if current_price is None or current_price <= 0:
            current_price = get_current_price(symbol)
            if current_price <= 0:
                log("❌ GRID", f"{symbol}: Invalid price")
                return
        
        sync_position(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
        
        # 최대 한도 체크
        with balance_lock:
            max_value = initial_capital * MAXPOSITIONRATIO
        
        current_price_dec = Decimal(str(current_price))
        long_value = long_size * current_price_dec
        short_value = short_size * current_price_dec
        
        if long_value >= max_value or short_value >= max_value:
            log("⚠️ LIMIT", f"{symbol}: Max position reached (L:{long_value:.2f}, S:{short_value:.2f}, Max:{max_value:.2f})")
            return
        
        # OBV 기반 수량 계산
        obv_display = float(obv_macd_value[symbol]) * 100
        obv_abs = abs(obv_display)
        obv_weight = calculate_obv_macd_weight(obv_abs)
        
        # 기본 수량
        base_ratio = get_symbol_config(symbol, "base_ratio")
        with balance_lock:
            base_value = initial_capital * base_ratio
        
        base_qty = int(base_value / current_price_dec)
        if base_qty < 1:
            log("❌ GRID", f"{symbol}: Insufficient quantity (base_qty={base_qty})")
            return
        
        # 역추세 진입
        if obv_display > 0:
            # 롱 강세 → SHORT 주력
            short_qty = int(base_qty * (1 + obv_weight))
            long_qty = int(base_qty * HEDGE_RATIO_MAIN) if ENABLE_AUTO_HEDGE else base_qty
            log("📊 ENTRY", f"{symbol} OBV+{obv_display:.1f}: SHORT {short_qty} (주력), LONG {long_qty} (헤지)")
        elif obv_display < 0:
            # 숏 강세 → LONG 주력
            long_qty = int(base_qty * (1 + obv_weight))
            short_qty = int(base_qty * HEDGE_RATIO_MAIN) if ENABLE_AUTO_HEDGE else base_qty
            log("📊 ENTRY", f"{symbol} OBV{obv_display:.1f}: LONG {long_qty} (주력), SHORT {short_qty} (헤지)")
        else:
            long_qty = base_qty
            short_qty = base_qty
            log("📊 ENTRY", f"{symbol} OBV=0: LONG={long_qty}, SHORT={short_qty}")
        
        # LONG 진입
        if long_qty > 0:
            try:
                order = FuturesOrder(
                    contract=symbol,
                    size=long_qty,
                    price=0,
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, order)
                log("✅ ENTRY", f"{symbol} LONG {long_qty} market")
            except GateApiException as e:
                log("❌ ENTRY", f"{symbol} LONG error: {e}")
                return
        
        time.sleep(0.1)
        
        # SHORT 진입
        if short_qty > 0:
            try:
                order = FuturesOrder(
                    contract=symbol,
                    size=-short_qty,
                    price=0,
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, order)
                log("✅ ENTRY", f"{symbol} SHORT {short_qty} market")
            except GateApiException as e:
                log("❌ ENTRY", f"{symbol} SHORT error: {e}")
                return
        
        time.sleep(0.2)
        sync_position(symbol)
        refresh_all_tp_orders(symbol)
        
        last_event_time[symbol] = time.time()
        log("✅ GRID", f"{symbol}: Market entry complete!")
    
    finally:
        initialize_grid_lock.release()


# =============================================================================
# 불균형 헤징 (심볼별)
# =============================================================================

def market_entry_when_imbalanced(symbol):
    """불균형 포지션 시 자동 헤징"""
    
    if not ENABLE_AUTO_HEDGE:
        return
    
    try:
        sync_position(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
        
        if long_size == 0 and short_size == 0:
            return
        
        # 불균형 판단
        if long_size > 0 and short_size == 0:
            missing_side = "SHORT"
            missing_qty = int(long_size * HEDGE_RATIO_MAIN)
        elif short_size > 0 and long_size == 0:
            missing_side = "LONG"
            missing_qty = int(short_size * HEDGE_RATIO_MAIN)
        else:
            return
        
        if missing_qty < 1:
            missing_qty = 1
        
        # 최대 한도 체크
        current_price = get_current_price(symbol)
        if current_price <= 0:
            return
        
        current_price_dec = Decimal(str(current_price))
        
        with balance_lock:
            max_value = initial_capital * MAXPOSITIONRATIO
        
        if missing_side == "LONG":
            if long_size * current_price_dec >= max_value:
                log("⚠️ HEDGE", f"{symbol}: Max position reached for LONG")
                return
        else:
            if short_size * current_price_dec >= max_value:
                log("⚠️ HEDGE", f"{symbol}: Max position reached for SHORT")
                return
        
        log("⚖️ HEDGE", f"{symbol}: Imbalanced, adding {missing_side} {missing_qty}")
        
        # 헤징 진입
        order_size = missing_qty if missing_side == "LONG" else -missing_qty
        order = FuturesOrder(
            contract=symbol,
            size=order_size,
            price=0,
            tif="ioc",
            reduce_only=False,
            text=generate_order_id()
        )
        
        api.create_futures_order(SETTLE, order)
        log("✅ HEDGE", f"{symbol} {missing_side} {missing_qty} executed")
        
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
        return
    
    try:
        now = time.time()
        
        if now - last_idle_check.get(symbol, 0) < 60:
            return
        last_idle_check[symbol] = now
        
        # 최대 진입 횟수 체크
        if idle_entry_count[symbol] >= MAX_IDLE_ENTRIES:
            return
        
        sync_position(symbol)
        
        with position_lock:
            long_size = position_state[symbol]["long"]["size"]
            short_size = position_state[symbol]["short"]["size"]
            long_price = position_state[symbol]["long"]["entry_price"]
            short_price = position_state[symbol]["short"]["entry_price"]
        
        if long_size == 0 or short_size == 0:
            return
        
        time_since_last = now - last_event_time.get(symbol, now)
        if time_since_last < IDLE_TIMEOUT:
            return
        
        # 최대 한도 체크
        with balance_lock:
            max_value = initial_capital * MAXPOSITIONRATIO
        
        current_price = get_current_price(symbol)
        if current_price <= 0:
            return
        
        current_price_dec = Decimal(str(current_price))
        
        long_value = long_size * current_price_dec
        short_value = short_size * current_price_dec
        
        if long_value >= max_value or short_value >= max_value:
            log("⚠️ IDLE", f"{symbol}: Max position reached")
            return
        
        idle_entry_in_progress[symbol] = True
        
        # 손실도 계산
        pnl_long = (current_price_dec - long_price) * long_size
        pnl_short = (short_price - current_price_dec) * short_size
        total_pnl = pnl_long + pnl_short
        
        with balance_lock:
            balance = initial_capital
        
        loss_pct = (float(total_pnl) / float(balance)) * 100 if balance > 0 else 0
        
        # 손실 가중
        base_ratio = get_symbol_config(symbol, "base_ratio")
        with balance_lock:
            base_value = initial_capital * base_ratio
        
        base_qty = int(base_value / current_price_dec)
        adjusted_qty = int(base_qty * (1 + loss_pct / 100))
        
        # OBV 가중
        obv_display = float(obv_macd_value[symbol]) * 100
        obv_abs = abs(obv_display)
        obv_weight = calculate_obv_macd_weight(obv_abs)
        
        # 수량 배분
        if obv_display > 0:
            short_qty = int(adjusted_qty * (1 + obv_weight))
            long_qty = adjusted_qty
        elif obv_display < 0:
            long_qty = int(adjusted_qty * (1 + obv_weight))
            short_qty = adjusted_qty
        else:
            long_qty = adjusted_qty
            short_qty = adjusted_qty
        
        idle_entry_count[symbol] += 1
        log("⏰ IDLE", f"{symbol} #{idle_entry_count[symbol]}: Loss={loss_pct:.2f}%, LONG={long_qty}, SHORT={short_qty}")
        
        # 진입
        try:
            if long_qty > 0:
                order = FuturesOrder(
                    contract=symbol,
                    size=long_qty,
                    price=0,
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, order)
        except:
            pass
        
        time.sleep(0.1)
        
        try:
            if short_qty > 0:
                order = FuturesOrder(
                    contract=symbol,
                    size=-short_qty,
                    price=0,
                    tif="ioc",
                    reduce_only=False,
                    text=generate_order_id()
                )
                api.create_futures_order(SETTLE, order)
        except:
            pass
        
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
            orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
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
        orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
        
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
        orders = api.list_futures_orders(SETTLE, contract=symbol, status="open")
        
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
