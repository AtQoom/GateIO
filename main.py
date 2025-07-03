import os
import json
import time
import asyncio
import threading
import websockets
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi

# ----------- 설정 파일 (Config) -----------
CONFIG = {
    "trading": {
        # 파인스크립트 v6.1 기본값과 일치
        "base_tp_pct": 0.005,      # 기본 익절률 0.5%
        "base_sl_pct": 0.002,      # 기본 손절률 0.2%
        "use_symbol_weights": True, # 심볼별 가중치 사용
        "min_signal_strength": 0.8  # v6.1 강화된 최소 신호 강도
    },
    "api": {
        "settle": "usdt",
        "ping_interval": 30,
        "reconnect_delay": 5,
        "max_delay": 60
    },
    "duplicate_prevention": {
        "cache_timeout": 300,
        "signal_timeout": 60,
        "cleanup_interval": 900
    },
    "filters": {
        "min_volatility": 0.5,     # 최소 변동성 %
        "min_volume_ratio": 1.8,   # 최소 거래량 비율
        "bb_long_threshold": 0.2,  # 볼린저 하단 20%
        "bb_short_threshold": 0.8  # 볼린저 상단 80%
    }
}

# 파인스크립트 v6.1의 심볼별 가중치 설정
SYMBOL_WEIGHTS = {
    "BTC_USDT": 0.6,   # 60%
    "ETH_USDT": 0.7,   # 70%
    "SOL_USDT": 0.8,   # 80%
    "ADA_USDT": 1.0,   # 100%
    "SUI_USDT": 1.0,   # 100%
    "LINK_USDT": 1.0,  # 100%
    "PEPE_USDT": 1.0,  # 100%
}

# 심볼 매핑 (파인스크립트에서 사용하는 심볼들)
SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "ETHUSDT": "ETH_USDT", "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "LINKUSDT": "LINK_USDT", "SOLUSDT": "SOL_USDT", 
    "PEPEUSDT": "PEPE_USDT",
    "BTCUSDT.P": "BTC_USDT", "ETHUSDT.P": "ETH_USDT", "ADAUSDT.P": "ADA_USDT",
    "SUIUSDT.P": "SUI_USDT", "LINKUSDT.P": "LINK_USDT", "SOLUSDT.P": "SOL_USDT", 
    "PEPEUSDT.P": "PEPE_USDT",
    "BTCUSDTPERP": "BTC_USDT", "ETHUSDTPERP": "ETH_USDT", "ADAUSDTPERP": "ADA_USDT",
    "SUIUSDTPERP": "SUI_USDT", "LINKUSDTPERP": "LINK_USDT", "SOLUSDTPERP": "SOL_USDT", 
    "PEPEUSDTPERP": "PEPE_USDT",
    "BTC_USDT": "BTC_USDT", "ETH_USDT": "ETH_USDT", "ADA_USDT": "ADA_USDT",
    "SUI_USDT": "SUI_USDT", "LINK_USDT": "LINK_USDT", "SOL_USDT": "SOL_USDT", 
    "PEPE_USDT": "PEPE_USDT",
    # 파인스크립트에서 자주 사용하는 형태
    "BTC": "BTC_USDT", "ETH": "ETH_USDT", "SOL": "SOL_USDT", "ADA": "ADA_USDT",
}

# 심볼별 계약 사양
SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"), "min_notional": Decimal("10")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"), "min_notional": Decimal("10")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("10")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("10")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("10")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("10")},
    "PEPE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000000"), "min_notional": Decimal("10")},
}

# ----------- 로그 설정 -----------
class CustomFilter(logging.Filter):
    def filter(self, record):
        filter_keywords = ["실시간 가격", "티커 수신", "포지션 없음", "계정 필드", "담보금 전환", "최종 선택", "전체 계정 정보", "웹소켓 핑", "핑 전송", "핑 성공", "ping", "Serving Flask app", "Debug mode", "WARNING: This is a development server"]
        message = record.getMessage()
        return not any(keyword in message for keyword in filter_keywords)

werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.addFilter(CustomFilter())
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
console_handler.setFormatter(formatter)
logger.handlers = []
logger.addHandler(console_handler)

def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception(msg)

# ----------- 서버 설정 -----------
app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = CONFIG["api"]["settle"]

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# 전역 변수
position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
position_strategy_info = {}  # 포지션별 전략 추적
alert_cache = {}
recent_signals = {}
duplicate_prevention_lock = threading.RLock()
entry_signals = {}  # 진입 신호 저장 (v6.1 추가)

# ----------- 핵심 함수들 -----------
def get_symbol_weight(symbol):
    """파인스크립트 v6.1 심볼별 가중치 반환"""
    return SYMBOL_WEIGHTS.get(symbol, 1.0)

def calculate_weighted_tpsl(symbol):
    """심볼별 가중치가 적용된 TP/SL 계산 (v6.1)"""
    weight = get_symbol_weight(symbol)
    base_tp = CONFIG["trading"]["base_tp_pct"]
    base_sl = CONFIG["trading"]["base_sl_pct"]
    
    # 파인스크립트 v6.1과 동일한 계산
    weighted_tp = base_tp * weight
    weighted_sl = base_sl * weight
    
    return weighted_tp, weighted_sl

def normalize_symbol(raw_symbol):
    """심볼 정규화"""
    if not raw_symbol:
        return None
    
    symbol = str(raw_symbol).upper().strip()
    
    # 직접 매핑
    if symbol in SYMBOL_MAPPING:
        return SYMBOL_MAPPING[symbol]
    
    # .P 제거 시도
    if symbol.endswith('.P'):
        base_symbol = symbol[:-2]
        if base_symbol in SYMBOL_MAPPING:
            return SYMBOL_MAPPING[base_symbol]
    
    # PERP 제거 시도
    if symbol.endswith('PERP'):
        base_symbol = symbol[:-4]
        if base_symbol in SYMBOL_MAPPING:
            return SYMBOL_MAPPING[base_symbol]
    
    return None

def parse_pinescript_alert(message):
    """파인스크립트 v6.1 알림 파싱 (강화된 데이터 포함)"""
    try:
        # JSON 형태 시도
        if message.startswith('{') and message.endswith('}'):
            return json.loads(message)
        
        # 간단한 파이프 구분 메시지 (하위 호환성)
        if message.startswith("ENTRY:") or message.startswith("EXIT:"):
            parts = message.split("|")
            if message.startswith("ENTRY:") and len(parts) >= 4:
                action_side = parts[0].split(":")
                return {
                    "action": "entry",
                    "side": action_side[1].lower(),
                    "symbol": parts[1],
                    "strategy": parts[2] if len(parts) > 2 else "Simple_Entry",
                    "price": float(parts[3]) if len(parts) > 3 else 0,
                    "signal_strength": float(parts[4]) if len(parts) > 4 else 0.8,
                    "id": str(int(time.time())) + "_pinescript"
                }
            elif message.startswith("EXIT:") and len(parts) >= 4:
                action_side = parts[0].split(":")
                return {
                    "action": "exit",
                    "side": action_side[1].lower(),
                    "symbol": parts[1],
                    "reason": parts[2] if len(parts) > 2 else "SIGNAL_EXIT",
                    "price": float(parts[3]) if len(parts) > 3 else 0,
                    "pnl": float(parts[4]) if len(parts) > 4 else 0,
                    "id": str(int(time.time())) + "_pinescript"
                }
    except Exception as e:
        log_debug("❌ 파인스크립트 v6.1 메시지 파싱 실패", str(e))
    return None

# ----------- 중복 방지 시스템 (파인스크립트 v6.1 단일 진입 시스템과 동기화) -----------
def is_duplicate_alert(alert_data):
    """중복 방지 (파인스크립트 v6.1 강화된 단일 진입 시스템)"""
    global alert_cache, recent_signals
    
    with duplicate_prevention_lock:
        current_time = time.time()
        alert_id = alert_data.get("id", "")
        symbol = alert_data.get("symbol", "")
        side = alert_data.get("side", "")
        action = alert_data.get("action", "")
        strategy_name = alert_data.get("strategy", "")
        
        # 같은 alert_id 확인
        if alert_id in alert_cache:
            cache_entry = alert_cache[alert_id]
            time_diff = current_time - cache_entry["timestamp"]
            if cache_entry["processed"] and time_diff < CONFIG["duplicate_prevention"]["cache_timeout"]:
                return True
        
        # 파인스크립트 v6.1 단일 진입 시스템과 동일한 로직
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            if symbol_key in recent_signals:
                recent = recent_signals[symbol_key]
                time_diff = current_time - recent["time"]
                # 같은 방향의 진입 신호 중복 방지
                if recent["action"] == "entry" and time_diff < CONFIG["duplicate_prevention"]["signal_timeout"]:
                    return True
        
        # 캐시에 저장
        alert_cache[alert_id] = {"timestamp": current_time, "processed": False}
        
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            recent_signals[symbol_key] = {
                "side": side, 
                "time": current_time, 
                "action": action, 
                "strategy": strategy_name
            }
        
        # 오래된 캐시 정리
        cutoff_time = current_time - CONFIG["duplicate_prevention"]["cleanup_interval"]
        alert_cache = {k: v for k, v in alert_cache.items() if v["timestamp"] > cutoff_time}
        recent_signals = {k: v for k, v in recent_signals.items() if v["time"] > cutoff_time}
        
        return False

def mark_alert_processed(alert_id):
    """알림 처리 완료 표시"""
    with duplicate_prevention_lock:
        if alert_id in alert_cache:
            alert_cache[alert_id]["processed"] = True

# ----------- v6.1 신호 검증 함수 -----------
def validate_signal_strength(data):
    """파인스크립트 v6.1 신호 강도 검증"""
    signal_strength = float(data.get("signal_strength", 0))
    min_strength = CONFIG["trading"]["min_signal_strength"]
    
    if signal_strength < min_strength:
        log_debug("⚠️ 약한 신호", f"신호 강도: {signal_strength:.2f} < 최소: {min_strength}")
        return False
    
    return True

def validate_market_conditions(data):
    """파인스크립트 v6.1 시장 조건 검증"""
    # 추가 필터 검증 (파인스크립트에서 전송된 경우)
    volatility = float(data.get("volatility", 1.0))
    volume_ratio = float(data.get("volume_ratio", 1.0))
    bb_position = float(data.get("bb_position", 0.5))
    side = data.get("side", "").lower()
    
    # 변동성 체크
    if volatility < CONFIG["filters"]["min_volatility"]:
        log_debug("⚠️ 낮은 변동성", f"{volatility:.2f}% < 최소: {CONFIG['filters']['min_volatility']}%")
        return False
    
    # 거래량 체크
    if volume_ratio < CONFIG["filters"]["min_volume_ratio"]:
        log_debug("⚠️ 낮은 거래량", f"{volume_ratio:.2f}x < 최소: {CONFIG['filters']['min_volume_ratio']}x")
        return False
    
    # 볼린저밴드 위치 체크
    if side == "long" and bb_position > CONFIG["filters"]["bb_long_threshold"]:
        log_debug("⚠️ 볼린저 위치", f"롱 신호인데 BB 위치: {bb_position:.2f} > {CONFIG['filters']['bb_long_threshold']}")
        return False
    elif side == "short" and bb_position < CONFIG["filters"]["bb_short_threshold"]:
        log_debug("⚠️ 볼린저 위치", f"숏 신호인데 BB 위치: {bb_position:.2f} < {CONFIG['filters']['bb_short_threshold']}")
        return False
    
    return True

# ----------- Gate.io API 함수들 -----------
def get_total_collateral(force=False):
    """순자산 조회"""
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        try:
            unified_accounts = unified_api.list_unified_accounts()
            if hasattr(unified_accounts, 'unified_account_total_equity'):
                equity = Decimal(str(unified_accounts.unified_account_total_equity))
                account_cache.update({"time": now, "data": equity})
                return equity
        except Exception:
            pass
            
        acc = api.list_futures_accounts(SETTLE)
        available = Decimal(str(getattr(acc, 'available', '0')))
        account_cache.update({"time": now, "data": available})
        return available
    except Exception as e:
        log_debug("❌ 총 자산 조회 실패", str(e), exc_info=True)
        return Decimal("0")

def get_price(symbol):
    """현재 가격 조회"""
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker or len(ticker) == 0:
            return Decimal("0")
        price_str = str(ticker[0].last).upper().replace("E", "e")
        price = Decimal(price_str).normalize()
        return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def get_current_position_count(symbol):
    """현재 포지션 개수 조회"""
    try:
        pos = api.get_position(SETTLE, symbol)
        size = Decimal(str(pos.size))
        return 1 if size != 0 else 0
    except Exception as e:
        if "POSITION_NOT_FOUND" in str(e):
            return 0
        log_debug(f"❌ 포지션 개수 조회 실패 ({symbol})", str(e))
        return 0

def calculate_position_size(symbol, strategy_type="Simple_Entry", signal_strength=0.8):
    """파인스크립트 v6.1 전략에 맞는 포지션 사이즈 계산"""
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    
    if price <= 0 or equity <= 0:
        return Decimal("0")
    
    try:
        # 파인스크립트 v6.1은 default_qty_value=100 (100% 자산 사용)
        position_ratio = Decimal("1.0")
        
        # v6.1: 신호 강도에 따른 포지션 크기 조정
        if signal_strength < 0.85:
            position_ratio *= Decimal("0.8")  # 약한 신호는 80%만
        elif signal_strength >= 0.9:
            position_ratio *= Decimal("1.1")  # 강한 신호는 110%
        
        # 전략 표시
        weight = get_symbol_weight(symbol)
        weight_display = f"{int(weight * 100)}%"
        if weight == 0.6:
            strategy_display = f"⚡ BTC 가중치 단타 v6.1 ({weight_display})"
        elif weight == 0.7:
            strategy_display = f"⚡ ETH 가중치 단타 v6.1 ({weight_display})"
        elif weight == 0.8:
            strategy_display = f"⚡ SOL 가중치 단타 v6.1 ({weight_display})"
        else:
            strategy_display = f"⚡ 기본 가중치 단타 v6.1 ({weight_display})"
        
        # 수량 계산
        adjusted_equity = equity * position_ratio
        raw_qty = adjusted_equity / (price * cfg["contract_size"])
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        
        # 최소 주문 금액 체크
        order_value = final_qty * price * cfg["contract_size"]
        if order_value < cfg["min_notional"]:
            return Decimal("0")
        
        return final_qty
        
    except Exception as e:
        log_debug(f"❌ 수량 계산 오류 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    """주문 실행"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        return False
    try:
        cfg = SYMBOL_CONFIG[symbol]
        qty_dec = Decimal(str(qty)).quantize(cfg["qty_step"], rounding=ROUND_DOWN)
        
        if qty_dec < cfg["min_qty"]:
            return False
            
        price = get_price(symbol)
        order_value = qty_dec * price * cfg["contract_size"]
        
        if order_value < cfg["min_notional"]:
            return False
            
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        
        api.create_futures_order(SETTLE, order)
        time.sleep(2)
        update_position_state(symbol)
        return True
        
    except Exception as e:
        if retry > 0 and ("INVALID_PARAM" in str(e) or "POSITION_EMPTY" in str(e) or "INSUFFICIENT_AVAILABLE" in str(e)):
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // cfg["qty_step"]) * cfg["qty_step"]
            retry_qty = max(retry_qty, cfg["min_qty"])
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False
    finally:
        position_lock.release()

def update_position_state(symbol, timeout=5):
    """포지션 상태 업데이트"""
    acquired = position_lock.acquire(timeout=timeout)
    if not acquired:
        return False
    try:
        try:
            pos = api.get_position(SETTLE, symbol)
        except Exception as e:
            if "POSITION_NOT_FOUND" in str(e):
                position_state[symbol] = {
                    "price": None, "side": None, "size": Decimal("0"), 
                    "value": Decimal("0"), "margin": Decimal("0"), 
                    "mode": "cross", "count": 0
                }
                return True
            else:
                return False
                
        size = Decimal(str(pos.size))
        if size != 0:
            position_entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            position_state[symbol] = {
                "price": position_entry_price, 
                "side": "buy" if size > 0 else "sell", 
                "size": abs(size), 
                "value": value, 
                "margin": value, 
                "mode": "cross", 
                "count": 1
            }
        else:
            position_state[symbol] = {
                "price": None, "side": None, "size": Decimal("0"), 
                "value": Decimal("0"), "margin": Decimal("0"), 
                "mode": "cross", "count": 0
            }
        return True
    except Exception as e:
        return False
    finally:
        position_lock.release()

def close_position(symbol):
    """포지션 청산"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        return False
    try:
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        
        # 청산 후 정리 (파인스크립트 v6.1 가상 포지션 추적과 동일)
        with duplicate_prevention_lock:
            keys_to_remove = [k for k in recent_signals.keys() if k.startswith(symbol + "_")]
            for key in keys_to_remove:
                del recent_signals[key]
        
        if symbol in position_strategy_info:
            del position_strategy_info[symbol]
        
        if symbol in entry_signals:
            del entry_signals[symbol]
        
        time.sleep(1)
        update_position_state(symbol)
        return True
    except Exception as e:
        return False
    finally:
        position_lock.release()

def calculate_profit_simple(entry_price, exit_price, side):
    """단순 수익률 계산"""
    if side == "long" or side == "buy":
        return float((exit_price - entry_price) / entry_price * 100)
    else:
        return float((entry_price - exit_price) / entry_price * 100)

# ----------- 웹훅 처리 (파인스크립트 v6.1 알림 처리) -----------
@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    """파인스크립트 v6.1 웹훅 처리 (강화된 심볼별 가중치 단타 전략)"""
    symbol = None
    alert_id = None
    raw_data = ""
    
    try:
        # Raw 데이터 확인
        try:
            raw_data = request.get_data(as_text=True)
        except Exception:
            raw_data = ""
        
        if not raw_data or raw_data.strip() == "":
            return jsonify({"error": "Empty data"}), 400
        
        # 파인스크립트 메시지 파싱
        data = None
        if raw_data.startswith("ENTRY:") or raw_data.startswith("EXIT:"):
            data = parse_pinescript_alert(raw_data.strip())
        else:
            try:
                data = request.get_json(force=True)
                if data is None:
                    data = json.loads(raw_data)
            except Exception:
                return jsonify({"error": "JSON parsing failed"}), 400
                
        if not data:
            return jsonify({"error": "Empty parsed data"}), 400
        
        # 필드 추출
        alert_id = data.get("id", "")
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower() if data.get("side") else ""
        action = data.get("action", "").lower() if data.get("action") else ""
        strategy_name = data.get("strategy", "Simple_Entry")
        signal_strength = float(data.get("signal_strength", 0.8))
        signal_type = data.get("signal_type", "unknown")
        
        # v6.1 추가 데이터
        rsi3_pred = float(data.get("rsi3_pred", 50))
        rsi3_conf = float(data.get("rsi3_conf", 50))
        rsi15s_pred = float(data.get("rsi15s_pred", 50))
        rsi15s_conf = float(data.get("rsi15s_conf", 50))
        
        # 필수 필드 검증
        missing_fields = []
        if not raw_symbol: missing_fields.append("symbol")
        if not side: missing_fields.append("side")
        if not action: missing_fields.append("action")
        
        if missing_fields:
            return jsonify({"error": f"Missing required fields: {missing_fields}"}), 400
        
        # 심볼 변환
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": f"Symbol not supported: {raw_symbol}"}), 400
        
        # 중복 방지 체크 (파인스크립트 v6.1 단일 진입 시스템)
        if is_duplicate_alert(data):
            return jsonify({"status": "duplicate_ignored", "message": "중복 알림 무시됨 (v6.1 단일 진입)"})
        
        # === 청산 신호 처리 ===
        if action == "exit":
            update_position_state(symbol, timeout=1)
            current_side = position_state.get(symbol, {}).get("side")
            
            if not current_side:
                success = True
                log_debug(f"🔄 청산 신호 ({symbol})", f"포지션 없음 - 신호 무시")
            else:
                success = close_position(symbol)
                if success:
                    exit_reason = data.get("reason", "SIGNAL_EXIT")
                    pnl = float(data.get("pnl", 0))
                    confidence = float(data.get("confidence", 0))
                    
                    log_debug(f"✅ 청산 완료 ({symbol})", 
                             f"사유: {exit_reason}, PnL: {pnl:.2f}%, 신뢰도: {confidence:.2f}")
                    
                    if symbol in position_strategy_info:
                       del position_strategy_info[symbol]
           
           if success and alert_id:
               mark_alert_processed(alert_id)
               
           return jsonify({
               "status": "success" if success else "error", 
               "action": "exit", 
               "symbol": symbol,
               "reason": data.get("reason", "SIGNAL_EXIT"),
               "pnl": float(data.get("pnl", 0))
           })
       
       # === 진입 신호 처리 (v6.1 강화) ===
       if action == "entry" and side in ["long", "short"]:
           # v6.1 신호 검증
           if not validate_signal_strength(data):
               if alert_id:
                   mark_alert_processed(alert_id)
               return jsonify({
                   "status": "rejected", 
                   "reason": "weak_signal", 
                   "signal_strength": signal_strength
               })
           
           # v6.1 시장 조건 검증
           if not validate_market_conditions(data):
               if alert_id:
                   mark_alert_processed(alert_id)
               return jsonify({
                   "status": "rejected", 
                   "reason": "market_conditions",
                   "filters": CONFIG["filters"]
               })
           
           # 심볼별 가중치 정보
           weight = get_symbol_weight(symbol)
           weighted_tp, weighted_sl = calculate_weighted_tpsl(symbol)
           
           weight_display = f"{int(weight * 100)}%"
           if weight == 0.6:
               weight_info = f"🟠 BTC 가중치 ({weight_display})"
           elif weight == 0.7:
               weight_info = f"🟡 ETH 가중치 ({weight_display})"
           elif weight == 0.8:
               weight_info = f"🟢 SOL 가중치 ({weight_display})"
           else:
               weight_info = f"⚪ 기본 가중치 ({weight_display})"
           
           # v6.1 신호 타입 표시
           signal_emoji = "🚀" if signal_type == "hybrid_enhanced" else "🔄" if signal_type == "backup_enhanced" else "📊"
           
           if not update_position_state(symbol, timeout=1):
               return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
           
           current_side = position_state.get(symbol, {}).get("side")
           desired_side = "buy" if side == "long" else "sell"
           
           # 기존 포지션 처리 (파인스크립트 v6.1 단일 진입 시스템)
           if current_side:
               if current_side == desired_side:
                   if alert_id:
                       mark_alert_processed(alert_id)
                   log_debug(f"🔄 진입 신호 ({symbol})", f"기존 포지션과 같은 방향 - 신호 무시")
                   return jsonify({"status": "same_direction", "message": "기존 포지션과 같은 방향"})
               else:
                   log_debug(f"🔄 역포지션 감지 ({symbol})", f"기존: {current_side} -> 새로운: {desired_side}")
                   if not close_position(symbol):
                       return jsonify({"status": "error", "message": "역포지션 청산 실패"})
                   time.sleep(3)
                   if not update_position_state(symbol):
                       pass
           
           # v6.1 신호 강도 기반 수량 계산
           qty = calculate_position_size(symbol, strategy_name, signal_strength)
           if qty <= 0:
               return jsonify({"status": "error", "message": "수량 계산 오류"})
           
           # 진입 시도
           success = place_order(symbol, desired_side, qty)
           
           # 진입 성공시 상세 정보 저장
           if success:
               entry_time = time.time()
               position_strategy_info[symbol] = {
                   "strategy": "weighted_scalping_v6.1", 
                   "entry_time": entry_time, 
                   "strategy_name": strategy_name,
                   "signal_strength": signal_strength,
                   "signal_type": signal_type,
                   "weight": weight,
                   "rsi_data": {
                       "rsi3_pred": rsi3_pred,
                       "rsi3_conf": rsi3_conf,
                       "rsi15s_pred": rsi15s_pred,
                       "rsi15s_conf": rsi15s_conf
                   }
               }
               
               # v6.1 진입 신호 저장 (학습용)
               entry_signals[symbol] = {
                   "entry_time": datetime.now().isoformat(),
                   "entry_price": float(get_price(symbol)),
                   "side": side,
                   "signal_strength": signal_strength,
                   "signal_type": signal_type,
                   "market_data": {
                       "volatility": float(data.get("volatility", 0)),
                       "volume_ratio": float(data.get("volume_ratio", 0)),
                       "bb_position": float(data.get("bb_position", 0.5))
                   }
               }
               
               log_debug(f"✅ 진입 완료 ({symbol}) {signal_emoji}", 
                        f"방향: {side.upper()}, 수량: {qty}, {weight_info}, "
                        f"신호강도: {signal_strength:.2f}, 타입: {signal_type}, "
                        f"TP: {weighted_tp*100:.3f}%, SL: {weighted_sl*100:.3f}%, "
                        f"RSI3: {rsi3_conf:.1f}, RSI15s: {rsi15s_conf:.1f}")
           else:
               log_debug(f"❌ 진입 실패 ({symbol})", f"주문 실행 오류")
           
           if success and alert_id:
               mark_alert_processed(alert_id)
           
           return jsonify({
               "status": "success" if success else "error", 
               "action": "entry", 
               "symbol": symbol, 
               "side": side, 
               "qty": float(qty), 
               "strategy": strategy_name,
               "signal_type": signal_type,
               "weight": weight,
               "weight_display": weight_info,
               "tp_pct": weighted_tp * 100,
               "sl_pct": weighted_sl * 100,
               "signal_strength": signal_strength,
               "rsi_data": {
                   "rsi3_conf": rsi3_conf,
                   "rsi15s_conf": rsi15s_conf
               }
           })
       
       return jsonify({"error": f"Invalid action: {action}"}), 400
       
   except Exception as e:
       if alert_id:
           mark_alert_processed(alert_id)
       return jsonify({
           "status": "error", 
           "message": str(e), 
           "raw_data": raw_data[:200] if raw_data else "unavailable"
       }), 500

# ----------- API 엔드포인트들 -----------
@app.route("/status", methods=["GET"])
def status():
   """서버 상태 조회 (파인스크립트 v6.1 기반)"""
   try:
       equity = get_total_collateral(force=True)
       positions = {}
       
       for sym in SYMBOL_CONFIG:
           if update_position_state(sym, timeout=1):
               pos = position_state.get(sym, {})
               if pos.get("side"):
                   weight = get_symbol_weight(sym)
                   weighted_tp, weighted_sl = calculate_weighted_tpsl(sym)
                   strategy_info = position_strategy_info.get(sym, {})
                   
                   position_info = {k: float(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
                   position_info.update({
                       "symbol_weight": weight,
                       "weight_display": f"{int(weight * 100)}%",
                       "weighted_tp_pct": weighted_tp * 100,
                       "weighted_sl_pct": weighted_sl * 100,
                       "signal_strength": strategy_info.get("signal_strength", 0.8),
                       "signal_type": strategy_info.get("signal_type", "unknown"),
                       "strategy_name": strategy_info.get("strategy_name", "Simple_Entry"),
                       "entry_time": strategy_info.get("entry_time", 0),
                       "rsi_data": strategy_info.get("rsi_data", {})
                   })
                   positions[sym] = position_info
       
       # 심볼별 가중치 설정 정보
       weight_settings = {}
       for symbol in SYMBOL_CONFIG:
           weight = get_symbol_weight(symbol)
           weighted_tp, weighted_sl = calculate_weighted_tpsl(symbol)
           
           weight_settings[symbol] = {
               "weight": weight,
               "weight_display": f"{int(weight * 100)}%",
               "base_tp_pct": CONFIG["trading"]["base_tp_pct"] * 100,
               "base_sl_pct": CONFIG["trading"]["base_sl_pct"] * 100,
               "weighted_tp_pct": weighted_tp * 100,
               "weighted_sl_pct": weighted_sl * 100
           }
       
       return jsonify({
           "status": "running",
           "mode": "pinescript_weighted_scalping_v6.1",
           "timestamp": datetime.now().isoformat(),
           "margin_balance": float(equity),
           "positions": positions,
           "weight_settings": weight_settings,
           "config": CONFIG,
           "pinescript_features": {
               "strategy_name": "심볼별 가중치 단타 전략 v6.1 (강화)",
               "symbol_weights": SYMBOL_WEIGHTS,
               "base_tpsl": {
                   "tp_pct": CONFIG["trading"]["base_tp_pct"] * 100,
                   "sl_pct": CONFIG["trading"]["base_sl_pct"] * 100
               },
               "single_entry_system": True,
               "hybrid_mode": True,
               "real_time_tpsl": True,
               "min_signal_strength": CONFIG["trading"]["min_signal_strength"],
               "filters": CONFIG["filters"],
               "supported_signals": ["Simple_Long", "Simple_Short", "hybrid_enhanced", "backup_enhanced"]
           }
       })
   except Exception as e:
       return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/config", methods=["GET"])
def get_config():
   """현재 설정 조회 (파인스크립트 v6.1 설정 반영)"""
   try:
       return jsonify({
           "config": CONFIG,
           "symbol_weights": SYMBOL_WEIGHTS,
           "base_tpsl": {
               "tp_pct": CONFIG["trading"]["base_tp_pct"] * 100,
               "sl_pct": CONFIG["trading"]["base_sl_pct"] * 100
           },
           "weighted_tpsl_by_symbol": {
               symbol: {
                   "weight": get_symbol_weight(symbol),
                   "tp_pct": calculate_weighted_tpsl(symbol)[0] * 100,
                   "sl_pct": calculate_weighted_tpsl(symbol)[1] * 100
               } for symbol in SYMBOL_CONFIG
           },
           "active_positions": {symbol: info for symbol, info in position_strategy_info.items()},
           "pinescript_version": "v6.1",
           "strategy_description": "강화된 심볼별 가중치 단타 전략 (더 엄격한 조건, 추가 필터)",
           "v6.1_features": {
               "min_signal_strength": CONFIG["trading"]["min_signal_strength"],
               "bollinger_filter": True,
               "volume_profile_filter": True,
               "rsi_acceleration_check": True,
               "dynamic_position_sizing": True,
               "enhanced_filters": CONFIG["filters"]
           }
       })
   except Exception as e:
       return jsonify({"error": str(e)}), 500

@app.route("/test-symbol/<symbol>", methods=["GET"])
def test_symbol_mapping(symbol):
   """심볼 매핑 및 가중치 테스트"""
   normalized = normalize_symbol(symbol)
   is_valid = normalized and normalized in SYMBOL_CONFIG
   
   if normalized:
       weight = get_symbol_weight(normalized)
       weighted_tp, weighted_sl = calculate_weighted_tpsl(normalized)
       
       weight_info = {
           "weight": weight,
           "weight_display": f"{int(weight * 100)}%",
           "base_tp_pct": CONFIG["trading"]["base_tp_pct"] * 100,
           "base_sl_pct": CONFIG["trading"]["base_sl_pct"] * 100,
           "weighted_tp_pct": weighted_tp * 100,
           "weighted_sl_pct": weighted_sl * 100
       }
   else:
       weight_info = None
   
   return jsonify({
       "input": symbol, 
       "normalized": normalized, 
       "valid": is_valid,
       "weight_info": weight_info,
       "all_mappings": {k: v for k, v in SYMBOL_MAPPING.items() if k.startswith(symbol.upper()[:3])},
       "pinescript_compatibility": "v6.1"
   })

# ----------- v6.1 추가 엔드포인트 -----------
@app.route("/signal-stats", methods=["GET"])
def signal_stats():
   """신호 통계 (v6.1 추가)"""
   try:
       stats = {
           "active_signals": len(entry_signals),
           "signal_details": {},
           "recent_signals": []
       }
       
       # 최근 신호들
       for symbol, signal in entry_signals.items():
           entry_time = datetime.fromisoformat(signal["entry_time"])
           holding_time = (datetime.now() - entry_time).total_seconds()
           
           stats["signal_details"][symbol] = {
               "entry_time": signal["entry_time"],
               "holding_time_seconds": holding_time,
               "signal_strength": signal["signal_strength"],
               "signal_type": signal["signal_type"],
               "side": signal["side"],
               "market_data": signal.get("market_data", {})
           }
       
       # 최근 처리된 신호들
       with duplicate_prevention_lock:
           for key, signal in recent_signals.items():
               stats["recent_signals"].append({
                   "symbol_side": key,
                   "time": datetime.fromtimestamp(signal["time"]).isoformat(),
                   "strategy": signal.get("strategy", "unknown")
               })
       
       return jsonify(stats)
   except Exception as e:
       return jsonify({"error": str(e)}), 500

# ----------- 실시간 TP/SL 모니터링 (파인스크립트 v6.1 기반) -----------
async def send_ping(ws):
   """웹소켓 핑 전송"""
   while True:
       try:
           await ws.ping()
       except Exception:
           break
       await asyncio.sleep(CONFIG["api"]["ping_interval"])

async def price_listener():
   """실시간 가격 모니터링 및 심볼별 가중치 TP/SL 처리 (v6.1)"""
   uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
   symbols = list(SYMBOL_CONFIG.keys())
   reconnect_delay = CONFIG["api"]["reconnect_delay"]
   max_delay = CONFIG["api"]["max_delay"]
   
   while True:
       try:
           async with websockets.connect(uri, ping_interval=CONFIG["api"]["ping_interval"], ping_timeout=15) as ws:
               subscribe_msg = {
                   "time": int(time.time()), 
                   "channel": "futures.tickers", 
                   "event": "subscribe", 
                   "payload": symbols
               }
               await ws.send(json.dumps(subscribe_msg))
               ping_task = asyncio.create_task(send_ping(ws))
               reconnect_delay = CONFIG["api"]["reconnect_delay"]
               
               while True:
                   try:
                       msg = await asyncio.wait_for(ws.recv(), timeout=45)
                       try:
                           data = json.loads(msg)
                       except json.JSONDecodeError:
                           continue
                       
                       if not isinstance(data, dict) or data.get("event") == "subscribe":
                           continue
                           
                       result = data.get("result")
                       if not result:
                           continue
                           
                       if isinstance(result, list):
                           for item in result:
                               if isinstance(item, dict):
                                   process_ticker_data(item)
                       elif isinstance(result, dict):
                           process_ticker_data(result)
                           
                   except (asyncio.TimeoutError, websockets.ConnectionClosed):
                       ping_task.cancel()
                       break
                   except Exception:
                       continue
       except Exception:
           await asyncio.sleep(reconnect_delay)
           reconnect_delay = min(reconnect_delay * 2, max_delay)

def process_ticker_data(ticker):
   """Gate.io 실시간 가격으로 심볼별 가중치 TP/SL 체크 (v6.1)"""
   try:
       contract = ticker.get("contract")
       last = ticker.get("last")
       if not contract or not last or contract not in SYMBOL_CONFIG:
           return
           
       price = Decimal(str(last).replace("E", "e")).normalize()
       
       acquired = position_lock.acquire(timeout=1)
       if not acquired:
           return
           
       try:
           if not update_position_state(contract, timeout=1):
               return
               
           pos = position_state.get(contract, {})
           position_entry_price = pos.get("price")
           size = pos.get("size", 0)
           side = pos.get("side")
           
           if not position_entry_price or size <= 0 or side not in ["buy", "sell"]:
               return
           
           # 심볼별 가중치 TP/SL 계산 (파인스크립트 v6.1과 동일)
           weight = get_symbol_weight(contract)
           weighted_tp, weighted_sl = calculate_weighted_tpsl(contract)
           
           # 전략 정보 가져오기
           strategy_info = position_strategy_info.get(contract, {})
           signal_type = strategy_info.get("signal_type", "unknown")
           signal_strength = strategy_info.get("signal_strength", 0.8)
           
           # 가중치 및 신호 타입 표시
           weight_display = f"{int(weight * 100)}%"
           if weight == 0.6:
               weight_info = f"🟠BTC({weight_display})"
           elif weight == 0.7:
               weight_info = f"🟡ETH({weight_display})"
           elif weight == 0.8:
               weight_info = f"🟢SOL({weight_display})"
           else:
               weight_info = f"⚪기본({weight_display})"
           
           signal_emoji = "🚀" if signal_type == "hybrid_enhanced" else "🔄" if signal_type == "backup_enhanced" else "📊"
           
           if side == "buy":
               sl = position_entry_price * (1 - Decimal(str(weighted_sl)))
               tp = position_entry_price * (1 + Decimal(str(weighted_tp)))
               if price <= sl:
                   log_debug(f"🛑 SL 트리거 ({contract}) {signal_emoji}", 
                            f"[{weight_info}] 현재가:{price} <= SL:{sl} (손절률:{weighted_sl*100:.3f}%)")
                   close_position(contract)
               elif price >= tp:
                   log_debug(f"🎯 TP 트리거 ({contract}) {signal_emoji}", 
                            f"[{weight_info}] 현재가:{price} >= TP:{tp} (익절률:{weighted_tp*100:.3f}%)")
                   close_position(contract)
           else:  # sell
               sl = position_entry_price * (1 + Decimal(str(weighted_sl)))
               tp = position_entry_price * (1 - Decimal(str(weighted_tp)))
               if price >= sl:
                   log_debug(f"🛑 SL 트리거 ({contract}) {signal_emoji}", 
                            f"[{weight_info}] 현재가:{price} >= SL:{sl} (손절률:{weighted_sl*100:.3f}%)")
                   close_position(contract)
               elif price <= tp:
                   log_debug(f"🎯 TP 트리거 ({contract}) {signal_emoji}", 
                            f"[{weight_info}] 현재가:{price} <= TP:{tp} (익절률:{weighted_tp*100:.3f}%)")
                   close_position(contract)
       finally:
           position_lock.release()
   except Exception:
       pass

def backup_position_loop():
   """백업 포지션 상태 갱신"""
   while True:
       try:
           for sym in SYMBOL_CONFIG:
               update_position_state(sym, timeout=1)
           time.sleep(300)  # 5분마다 갱신
       except Exception:
           time.sleep(300)

def log_initial_status():
   """서버 시작시 초기 상태 로깅 (파인스크립트 v6.1 정보 포함)"""
   try:
       log_debug("🚀 서버 시작", "파인스크립트 심볼별 가중치 단타 전략 v6.1 (강화)")
       equity = get_total_collateral(force=True)
       log_debug("💰 총 자산(초기)", f"{equity} USDT")
       
       # 심볼별 가중치 설정 로깅
       log_debug("⚙️ 심볼별 가중치", "BTC: 60%, ETH: 70%, SOL: 80%, 기타: 100%")
       log_debug("📊 기본 TP/SL", f"TP: {CONFIG['trading']['base_tp_pct']*100:.1f}%, SL: {CONFIG['trading']['base_sl_pct']*100:.1f}%")
       log_debug("🎯 최소 신호 강도", f"{CONFIG['trading']['min_signal_strength']:.2f}")
       
       # v6.1 필터 설정 로깅
       log_debug("🔍 v6.1 필터", f"최소 변동성: {CONFIG['filters']['min_volatility']}%, "
                                f"최소 거래량: {CONFIG['filters']['min_volume_ratio']}x, "
                                f"BB 롱: <{CONFIG['filters']['bb_long_threshold']}, "
                                f"BB 숏: >{CONFIG['filters']['bb_short_threshold']}")
       
       # 각 심볼별 실제 적용 TP/SL 로깅
       for symbol in SYMBOL_CONFIG:
           weight = get_symbol_weight(symbol)
           weighted_tp, weighted_sl = calculate_weighted_tpsl(symbol)
           log_debug(f"🎯 {symbol}", 
                    f"가중치: {int(weight*100)}%, TP: {weighted_tp*100:.2f}%, SL: {weighted_sl*100:.2f}%")
       
       # 초기 포지션 상태 확인
       for symbol in SYMBOL_CONFIG:
           if not update_position_state(symbol, timeout=3):
               continue
           pos = position_state.get(symbol, {})
           if pos.get("side"):
               count = pos.get("count", 0)
               log_debug(f"📊 초기 포지션 ({symbol})", 
                        f"방향: {pos['side']}, 수량: {pos['size']}, 포지션수: {count}/1")
           else:
               log_debug(f"📊 초기 포지션 ({symbol})", "포지션 없음")
               
   except Exception as e:
       log_debug("❌ 초기 상태 로깅 실패", str(e), exc_info=True)

# ----------- 추가 유틸리티 엔드포인트 -----------
@app.route("/pinescript-info", methods=["GET"])
def pinescript_info():
   """파인스크립트 전략 정보"""
   return jsonify({
       "strategy_name": "심볼별 가중치 단타 전략 v6.1 (강화)",
       "version": "6.1",
       "features": {
           "symbol_weights": True,
           "single_entry_system": True,
           "hybrid_mode": True,
           "real_time_tpsl": True,
           "15s_confirmation": True,
           "multi_timeframe": True,
           "bollinger_filter": True,
           "volume_profile": True,
           "rsi_acceleration": True,
           "min_volatility": True
       },
       "symbol_weights": SYMBOL_WEIGHTS,
       "default_settings": {
           "base_sl_pct": CONFIG["trading"]["base_sl_pct"] * 100,
           "base_tp_pct": CONFIG["trading"]["base_tp_pct"] * 100,
           "qty_value": 100,
           "pyramiding": 1,
           "min_signal_strength": CONFIG["trading"]["min_signal_strength"]
       },
       "filters": CONFIG["filters"],
       "supported_alerts": {
           "entry_formats": [
               '{"action":"entry","side":"long","symbol":"SYMBOL","strategy":"Simple_Long","signal_strength":0.85,"signal_type":"hybrid_enhanced",...}',
               '{"action":"entry","side":"short","symbol":"SYMBOL","strategy":"Simple_Short","signal_strength":0.82,"signal_type":"backup_enhanced",...}'
           ],
           "exit_formats": [
               '{"action":"exit","side":"long","symbol":"SYMBOL","reason":"STOP_LOSS","pnl":-0.2,...}',
               '{"action":"exit","side":"short","symbol":"SYMBOL","reason":"TAKE_PROFIT","pnl":0.5,...}'
           ]
       }
   })

@app.route("/weights", methods=["GET"])
def get_weights():
   """심볼별 가중치 상세 정보 (v6.1)"""
   try:
       weight_details = {}
       for symbol in SYMBOL_CONFIG:
           weight = get_symbol_weight(symbol)
           weighted_tp, weighted_sl = calculate_weighted_tpsl(symbol)
           
           # 심볼 타입 분류
           if "BTC" in symbol:
               symbol_type = "🟠 Bitcoin"
           elif "ETH" in symbol:
               symbol_type = "🟡 Ethereum"
           elif "SOL" in symbol:
               symbol_type = "🟢 Solana"
           else:
               symbol_type = "⚪ Others"
           
           weight_details[symbol] = {
               "symbol_type": symbol_type,
               "weight": weight,
               "weight_display": f"{int(weight * 100)}%",
               "base_tp_pct": CONFIG["trading"]["base_tp_pct"] * 100,
               "base_sl_pct": CONFIG["trading"]["base_sl_pct"] * 100,
               "weighted_tp_pct": weighted_tp * 100,
               "weighted_sl_pct": weighted_sl * 100,
               "reduction_factor": f"{(1-weight)*100:.0f}% 감소" if weight < 1.0 else "감소 없음"
           }
       
       return jsonify({
           "pinescript_strategy": "심볼별 가중치 단타 전략 v6.1 (강화)",
           "weight_system": {
               "BTC": "60% (40% 감소)",
               "ETH": "70% (30% 감소)", 
               "SOL": "80% (20% 감소)",
               "Others": "100% (감소 없음)"
           },
           "symbol_details": weight_details,
           "v6.1_enhancements": {
               "stricter_rsi": "롱 28, 숏 72 (더 극단적)",
               "higher_volume": "1.8x 이상 거래량",
               "bollinger_bands": "롱 <20%, 숏 >80%",
               "min_volatility": "0.5% 이상",
               "signal_strength": "0.8 이상만 진입"
           }
       })
   except Exception as e:
       return jsonify({"error": str(e)}), 500

# ----------- 메인 실행 -----------
if __name__ == "__main__":
   log_initial_status()
   
   # 실시간 가격 모니터링 (심볼별 가중치 TP/SL)
   threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
   
   # 백업 포지션 상태 갱신
   threading.Thread(target=backup_position_loop, daemon=True).start()
   
   port = int(os.environ.get("PORT", 8080))
   log_debug("🚀 서버 시작", f"포트 {port}에서 실행")
   log_debug("⚡ 파인스크립트 v6.1", "강화된 심볼별 가중치 단타 전략 연동")
   log_debug("🎯 가중치 시스템", "BTC: 60%, ETH: 70%, SOL: 80%, 기타: 100%")
   log_debug("🔥 실시간 TP/SL", f"기본 TP: {CONFIG['trading']['base_tp_pct']*100:.1f}%, SL: {CONFIG['trading']['base_sl_pct']*100:.1f}% (가중치 적용)")
   log_debug("🛡️ 강화된 필터", f"신호강도 ≥{CONFIG['trading']['min_signal_strength']}, 볼린저/거래량/변동성 필터")
   log_debug("📊 단일 진입 시스템", "파인스크립트 v6.1과 동일한 중복 방지")
   
   app.run(host="0.0.0.0", port=port, debug=False)
