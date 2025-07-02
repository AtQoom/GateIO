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
        "base_tp_pct": 0.003,      # 기본 TP (fallback)
        "base_sl_pct": 0.0012,     # 기본 SL (fallback)
        "strategy_tpsl": {
            "1m": {"tp": 0.002, "sl": 0.001},   # 1분: TP 0.2%, SL 0.1%
            "3m": {"tp": 0.003, "sl": 0.0012},  # 3분: TP 0.3%, SL 0.12%
            "5m": {"tp": 0.004, "sl": 0.0015}   # 5분: TP 0.4%, SL 0.15%
        }
    },
    "strategy": {
        "5m_multiplier": 2.0,      # 5분 전략: 2배 수량
        "3m_multiplier": 1.5,      # 3분 전략: 1.5배 수량
        "1m_multiplier": 1.0       # 1분 전략: 기본 수량
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
    }
}

# 심볼별 TP/SL 가중치 설정
SYMBOL_TPSL_MULTIPLIERS = {
    "BTC_USDT": {"tp": 0.7, "sl": 0.7},
    "ETH_USDT": {"tp": 0.8, "sl": 0.8},
    "SOL_USDT": {"tp": 0.9, "sl": 0.9},
}

# 심볼 매핑
SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "ETHUSDT": "ETH_USDT", "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "LINKUSDT": "LINK_USDT", "SOLUSDT": "SOL_USDT", "PEPEUSDT": "PEPE_USDT",
    "BTCUSDT.P": "BTC_USDT", "ETHUSDT.P": "ETH_USDT", "ADAUSDT.P": "ADA_USDT",
    "SUIUSDT.P": "SUI_USDT", "LINKUSDT.P": "LINK_USDT", "SOLUSDT.P": "SOL_USDT", "PEPEUSDT.P": "PEPE_USDT",
    "BTCUSDTPERP": "BTC_USDT", "ETHUSDTPERP": "ETH_USDT", "ADAUSDTPERP": "ADA_USDT",
    "SUIUSDTPERP": "SUI_USDT", "LINKUSDTPERP": "LINK_USDT", "SOLUSDTPERP": "SOL_USDT", "PEPEUSDTPERP": "PEPE_USDT",
    "BTC_USDT": "BTC_USDT", "ETH_USDT": "ETH_USDT", "ADA_USDT": "ADA_USDT",
    "SUI_USDT": "SUI_USDT", "LINK_USDT": "LINK_USDT", "SOL_USDT": "SOL_USDT", "PEPE_USDT": "PEPE_USDT",
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

# ----------- 핵심 함수들 -----------
def get_tpsl_multipliers(symbol):
    """심볼별 TP/SL 배수 반환"""
    return SYMBOL_TPSL_MULTIPLIERS.get(symbol, {"tp": 1.0, "sl": 1.0})

def get_strategy_tpsl(strategy_name):
    """전략별 TP/SL 반환"""
    try:
        if "5M" in strategy_name.upper():
            strategy_key = "5m"
        elif "3M" in strategy_name.upper():
            strategy_key = "3m"
        elif "1M" in strategy_name.upper():
            strategy_key = "1m"
        else:
            return {"tp": CONFIG["trading"]["base_tp_pct"], "sl": CONFIG["trading"]["base_sl_pct"]}
        
        strategy_tpsl = CONFIG["trading"]["strategy_tpsl"].get(strategy_key, {})
        return {
            "tp": strategy_tpsl.get("tp", CONFIG["trading"]["base_tp_pct"]),
            "sl": strategy_tpsl.get("sl", CONFIG["trading"]["base_sl_pct"])
        }
    except Exception as e:
        log_debug("❌ 전략별 TP/SL 조회 실패", str(e))
        return {"tp": CONFIG["trading"]["base_tp_pct"], "sl": CONFIG["trading"]["base_sl_pct"]}

def get_tpsl_values(strategy_name=None):
    """현재 TP/SL 값 반환 (전략별)"""
    if strategy_name:
        strategy_tpsl = get_strategy_tpsl(strategy_name)
        return strategy_tpsl["tp"], strategy_tpsl["sl"]
    else:
        return CONFIG["trading"]["base_tp_pct"], CONFIG["trading"]["base_sl_pct"]

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

def parse_simple_alert(message):
    """간단한 파이프 구분 메시지 파싱"""
    try:
        if message.startswith("ENTRY:"):
            parts = message.split("|")
            if len(parts) >= 5:
                return {
                    "action": "entry", "side": parts[0].split(":")[1], "symbol": parts[1],
                    "strategy": parts[2], "price": float(parts[3]), "position_count": int(parts[4]),
                    "id": str(int(time.time())) + "_simple"
                }
        elif message.startswith("EXIT:"):
            parts = message.split("|")
            if len(parts) >= 5:
                return {
                    "action": "exit", "side": parts[0].split(":")[1], "symbol": parts[1],
                    "exit_reason": parts[2], "price": float(parts[3]), "pnl_pct": float(parts[4]),
                    "id": str(int(time.time())) + "_simple"
                }
    except Exception as e:
        log_debug("❌ 간단 메시지 파싱 실패", str(e))
    return None

# ----------- 중복 방지 시스템 -----------
def is_duplicate_alert(alert_data):
    """중복 방지 (단일 진입)"""
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
        
        # 같은 방향 신호 중복 확인
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            if symbol_key in recent_signals:
                recent = recent_signals[symbol_key]
                time_diff = current_time - recent["time"]
                if (recent["strategy"] == strategy_name and recent["action"] == "entry" and time_diff < CONFIG["duplicate_prevention"]["signal_timeout"]):
                    return True
        
        # 캐시에 저장
        alert_cache[alert_id] = {"timestamp": current_time, "processed": False}
        
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            recent_signals[symbol_key] = {"side": side, "time": current_time, "action": action, "strategy": strategy_name}
        
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

def calculate_position_size(symbol, strategy_type="standard"):
    """전략별 차등 수량 계산 (3개 전략)"""
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    
    if price <= 0 or equity <= 0:
        return Decimal("0")
    
    try:
        # 전략별 포지션 배수
        if "5M" in strategy_type.upper():
            position_ratio = Decimal(str(CONFIG["strategy"]["5m_multiplier"]))
            strategy_display = "🔥 5분 전략 (2배)"
        elif "3M" in strategy_type.upper():
            position_ratio = Decimal(str(CONFIG["strategy"]["3m_multiplier"]))
            strategy_display = "📊 3분 전략 (1.5배)"
        elif "1M" in strategy_type.upper():
            position_ratio = Decimal(str(CONFIG["strategy"]["1m_multiplier"]))
            strategy_display = "⚡ 1분 전략 (1배)"
        else:
            position_ratio = Decimal("1.0")
            strategy_display = "🔧 표준 전략 (1배)"
        
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
                position_state[symbol] = {"price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross", "count": 0}
                return True
            else:
                return False
                
        size = Decimal(str(pos.size))
        if size != 0:
            position_entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            position_state[symbol] = {"price": position_entry_price, "side": "buy" if size > 0 else "sell", "size": abs(size), "value": value, "margin": value, "mode": "cross", "count": 1}
        else:
            position_state[symbol] = {"price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross", "count": 0}
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
        
        # 청산 후 정리
        with duplicate_prevention_lock:
            keys_to_remove = [k for k in recent_signals.keys() if k.startswith(symbol + "_")]
            for key in keys_to_remove:
                del recent_signals[key]
        
        if symbol in position_strategy_info:
            del position_strategy_info[symbol]
        
        time.sleep(1)
        update_position_state(symbol)
        return True
    except Exception as e:
        return False
    finally:
        position_lock.release()

# ----------- 웹훅 처리 -----------
@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    """5분+3분+1분 전략 웹훅 처리"""
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
        
        # 메시지 파싱
        data = None
        if raw_data.startswith("ENTRY:") or raw_data.startswith("EXIT:"):
            data = parse_simple_alert(raw_data.strip())
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
        strategy_name = data.get("strategy", "")
        
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
        
        # 중복 방지 체크
        if is_duplicate_alert(data):
            return jsonify({"status": "duplicate_ignored", "message": "중복 알림 무시됨"})
        
        # === 청산 신호 처리 ===
        if action == "exit":
            update_position_state(symbol, timeout=1)
            current_side = position_state.get(symbol, {}).get("side")
            
            if not current_side:
                success = True
            else:
                success = close_position(symbol)
                if success and symbol in position_strategy_info:
                    del position_strategy_info[symbol]
            
            if success and alert_id:
                mark_alert_processed(alert_id)
                
            return jsonify({"status": "success" if success else "error", "action": "exit", "symbol": symbol})
        
        # === 진입 신호 처리 ===
        if action == "entry" and side in ["long", "short"]:
            # 전략 타입 분석
            if "5M_" in strategy_name.upper():
                strategy_display = "🔥 5분 전략 (2배 수량)"
                strategy_priority = "HIGH"
            elif "3M_" in strategy_name.upper():
                strategy_display = "📊 3분 전략 (1.5배 수량)"
                strategy_priority = "MEDIUM"
            elif "1M_" in strategy_name.upper():
                strategy_display = "⚡ 1분 전략 (기본 수량)"
                strategy_priority = "LOW"
            else:
                strategy_display = "🔧 표준 전략 (기본 수량)"
                strategy_priority = "MEDIUM"
            
            if not update_position_state(symbol, timeout=1):
                return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
            
            current_side = position_state.get(symbol, {}).get("side")
            desired_side = "buy" if side == "long" else "sell"
            
            # 기존 포지션 처리
            if current_side:
                if current_side == desired_side:
                    if alert_id:
                        mark_alert_processed(alert_id)
                    return jsonify({"status": "same_direction", "message": "기존 포지션과 같은 방향"})
                else:
                    if not close_position(symbol):
                        return jsonify({"status": "error", "message": "역포지션 청산 실패"})
                    time.sleep(3)
                    if not update_position_state(symbol):
                        pass
            
            # 수량 계산 및 주문 실행
            qty = calculate_position_size(symbol, strategy_name)
            if qty <= 0:
                return jsonify({"status": "error", "message": "수량 계산 오류"})
            
            success = place_order(symbol, desired_side, qty)
            
            # 진입 성공시 전략 정보 저장
            if success:
                strategy_key = "5m" if "5M" in strategy_name.upper() else "3m" if "3M" in strategy_name.upper() else "1m"
                position_strategy_info[symbol] = {"strategy": strategy_key, "entry_time": time.time(), "strategy_name": strategy_name}
            
            if success and alert_id:
                mark_alert_processed(alert_id)
            
            return jsonify({"status": "success" if success else "error", "action": "entry", "symbol": symbol, "side": side, "qty": float(qty), "strategy": strategy_name, "strategy_display": strategy_display})
        
        return jsonify({"error": f"Invalid action: {action}"}), 400
        
    except Exception as e:
        if alert_id:
            mark_alert_processed(alert_id)
        return jsonify({"status": "error", "message": str(e), "raw_data": raw_data[:200] if raw_data else "unavailable"}), 500

# ----------- API 엔드포인트들 -----------
@app.route("/status", methods=["GET"])
def status():
    """서버 상태 조회"""
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        
        for sym in SYMBOL_CONFIG:
            if update_position_state(sym, timeout=1):
                pos = position_state.get(sym, {})
                if pos.get("side"):
                    multipliers = get_tpsl_multipliers(sym)
                    strategy_info = position_strategy_info.get(sym, {})
                    current_strategy = strategy_info.get("strategy", "3m")
                    
                    strategy_tpsl = CONFIG["trading"]["strategy_tpsl"].get(current_strategy, {})
                    base_tp = strategy_tpsl.get("tp", CONFIG["trading"]["base_tp_pct"])
                    base_sl = strategy_tpsl.get("sl", CONFIG["trading"]["base_sl_pct"])
                    
                    actual_tp = base_tp * multipliers["tp"]
                    actual_sl = base_sl * multipliers["sl"]
                    
                    position_info = {k: float(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
                    position_info.update({
                        "current_strategy": current_strategy,
                        "strategy_display": {"5m": "🔥5분", "3m": "📊3분", "1m": "⚡1분"}.get(current_strategy, "🔧기본"),
                        "actual_tp_pct": actual_tp * 100,
                        "actual_sl_pct": actual_sl * 100,
                    })
                    positions[sym] = position_info
        
        # 전략별 TP/SL 설정 정보
        tpsl_settings = {}
        for symbol in SYMBOL_CONFIG:
            multipliers = get_tpsl_multipliers(symbol)
            strategy_info = {}
            for strategy_key in ["1m", "3m", "5m"]:
                strategy_tpsl = CONFIG["trading"]["strategy_tpsl"].get(strategy_key, {})
                base_tp = strategy_tpsl.get("tp", CONFIG["trading"]["base_tp_pct"])
                base_sl = strategy_tpsl.get("sl", CONFIG["trading"]["base_sl_pct"])
                
                strategy_info[f"{strategy_key}_strategy"] = {
                    "base_tp_pct": base_tp * 100,
                    "base_sl_pct": base_sl * 100,
                    "actual_tp_pct": base_tp * multipliers["tp"] * 100,
                    "actual_sl_pct": base_sl * multipliers["sl"] * 100
                }
            
            tpsl_settings[symbol] = {"tp_multiplier": multipliers["tp"], "sl_multiplier": multipliers["sl"], "strategies": strategy_info}
        
        return jsonify({
            "status": "running",
            "mode": "pinescript_5m3m1m_triple_strategy",
            "timestamp": datetime.now().isoformat(),
            "margin_balance": float(equity),
            "positions": positions,
            "tpsl_settings": tpsl_settings,
            "config": CONFIG,
            "pinescript_features": {
                "perfect_alerts": True,
                "strategy_levels": {
                    "5m_strategy": {"multiplier": CONFIG["strategy"]["5m_multiplier"], "priority": "HIGH", "description": "5분-3분-15초 삼중확인", "tp_pct": CONFIG["trading"]["strategy_tpsl"]["5m"]["tp"] * 100, "sl_pct": CONFIG["trading"]["strategy_tpsl"]["5m"]["sl"] * 100},
                    "3m_strategy": {"multiplier": CONFIG["strategy"]["3m_multiplier"], "priority": "MEDIUM", "description": "3분-15초 이중확인", "tp_pct": CONFIG["trading"]["strategy_tpsl"]["3m"]["tp"] * 100, "sl_pct": CONFIG["trading"]["strategy_tpsl"]["3m"]["sl"] * 100},
                    "1m_strategy": {"multiplier": CONFIG["strategy"]["1m_multiplier"], "priority": "LOW", "description": "1분-15초 빠른반응", "tp_pct": CONFIG["trading"]["strategy_tpsl"]["1m"]["tp"] * 100, "sl_pct": CONFIG["trading"]["strategy_tpsl"]["1m"]["sl"] * 100}
                }
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/config", methods=["GET"])
def get_config():
    """현재 설정 조회"""
    try:
        return jsonify({
            "config": CONFIG,
            "current_tpsl": {
                "strategy_tpsl": {
                    "1m": {"tp_pct": CONFIG["trading"]["strategy_tpsl"]["1m"]["tp"] * 100, "sl_pct": CONFIG["trading"]["strategy_tpsl"]["1m"]["sl"] * 100},
                    "3m": {"tp_pct": CONFIG["trading"]["strategy_tpsl"]["3m"]["tp"] * 100, "sl_pct": CONFIG["trading"]["strategy_tpsl"]["3m"]["sl"] * 100},
                    "5m": {"tp_pct": CONFIG["trading"]["strategy_tpsl"]["5m"]["tp"] * 100, "sl_pct": CONFIG["trading"]["strategy_tpsl"]["5m"]["sl"] * 100}
                }
            },
            "symbol_tpsl_multipliers": SYMBOL_TPSL_MULTIPLIERS,
            "strategy_multipliers": {"5m_strategy": CONFIG["strategy"]["5m_multiplier"], "3m_strategy": CONFIG["strategy"]["3m_multiplier"], "1m_strategy": CONFIG["strategy"]["1m_multiplier"]},
            "active_positions": {symbol: info for symbol, info in position_strategy_info.items()}
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test-symbol/<symbol>", methods=["GET"])
def test_symbol_mapping(symbol):
    """심볼 매핑 테스트"""
    normalized = normalize_symbol(symbol)
    is_valid = normalized and normalized in SYMBOL_CONFIG
    multipliers = get_tpsl_multipliers(normalized) if normalized else {"tp": 1.0, "sl": 1.0}
    
    # 전략별 TP/SL 계산
    strategy_results = {}
    for strategy_key in ["1m", "3m", "5m"]:
        strategy_tpsl = CONFIG["trading"]["strategy_tpsl"].get(strategy_key, {})
        base_tp = strategy_tpsl.get("tp", CONFIG["trading"]["base_tp_pct"])
        base_sl = strategy_tpsl.get("sl", CONFIG["trading"]["base_sl_pct"])
        
        strategy_results[strategy_key] = {
            "tp_pct": base_tp * multipliers["tp"] * 100,
            "sl_pct": base_sl * multipliers["sl"] * 100
        }
    
    return jsonify({
        "input": symbol, "normalized": normalized, "valid": is_valid,
        "tpsl_multipliers": multipliers, "strategy_tpsl": strategy_results,
        "all_mappings": {k: v for k, v in SYMBOL_MAPPING.items() if k.startswith(symbol.upper()[:3])}
    })

# ----------- 실시간 TP/SL 모니터링 -----------
async def send_ping(ws):
    """웹소켓 핑 전송"""
    while True:
        try:
            await ws.ping()
        except Exception:
            break
        await asyncio.sleep(CONFIG["api"]["ping_interval"])

async def price_listener():
    """실시간 가격 모니터링 및 전략별 TP/SL 처리"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = CONFIG["api"]["reconnect_delay"]
    max_delay = CONFIG["api"]["max_delay"]
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=CONFIG["api"]["ping_interval"], ping_timeout=15) as ws:
                subscribe_msg = {"time": int(time.time()), "channel": "futures.tickers", "event": "subscribe", "payload": symbols}
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
    """Gate.io 실시간 가격으로 전략별 TP/SL 체크"""
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
            
            # 포지션별 전략 정보 조회
            strategy_info = position_strategy_info.get(contract, {})
            strategy_key = strategy_info.get("strategy", "3m")
            
            # 전략별 TP/SL 비율 적용
            strategy_tpsl = CONFIG["trading"]["strategy_tpsl"].get(strategy_key, {})
            base_tp = strategy_tpsl.get("tp", CONFIG["trading"]["base_tp_pct"])
            base_sl = strategy_tpsl.get("sl", CONFIG["trading"]["base_sl_pct"])
            
            # 심볼별 가중치 적용
            multipliers = get_tpsl_multipliers(contract)
            sl_pct = Decimal(str(base_sl)) * Decimal(str(multipliers["sl"]))
            tp_pct = Decimal(str(base_tp)) * Decimal(str(multipliers["tp"]))
            
            strategy_display = {"5m": "🔥5분", "3m": "📊3분", "1m": "⚡1분"}.get(strategy_key, "🔧기본")
            
            if side == "buy":
                sl = position_entry_price * (1 - sl_pct)
                tp = position_entry_price * (1 + tp_pct)
                if price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"[{strategy_display}] 현재가:{price} <= SL:{sl} (SL비율:{sl_pct*100:.3f}%)")
                    close_position(contract)
                elif price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"[{strategy_display}] 현재가:{price} >= TP:{tp} (TP비율:{tp_pct*100:.3f}%)")
                    close_position(contract)
            else:
                sl = position_entry_price * (1 + sl_pct)
                tp = position_entry_price * (1 - tp_pct)
                if price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"[{strategy_display}] 현재가:{price} >= SL:{sl} (SL비율:{sl_pct*100:.3f}%)")
                    close_position(contract)
                elif price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"[{strategy_display}] 현재가:{price} <= TP:{tp} (TP비율:{tp_pct*100:.3f}%)")
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
            time.sleep(300)
        except Exception:
            time.sleep(300)

def log_initial_status():
    """서버 시작시 초기 상태 로깅"""
    try:
        log_debug("🚀 서버 시작", "5분+3분+1분 삼중 전략 모드 (전략별 TP/SL)")
        equity = get_total_collateral(force=True)
        log_debug("💰 총 자산(초기)", f"{equity} USDT")
        
        # 전략별 TP/SL 설정 로깅
        for strategy_key in ["1m", "3m", "5m"]:
            strategy_tpsl = CONFIG["trading"]["strategy_tpsl"].get(strategy_key, {})
            tp_pct = strategy_tpsl.get("tp", 0) * 100
            sl_pct = strategy_tpsl.get("sl", 0) * 100
            log_debug(f"📊 {strategy_key.upper()} 전략", f"TP: {tp_pct:.2f}%, SL: {sl_pct:.2f}%")
        
        for symbol in SYMBOL_CONFIG:
            if not update_position_state(symbol, timeout=3):
                continue
            pos = position_state.get(symbol, {})
            if pos.get("side"):
                count = pos.get("count", 0)
                log_debug(f"📊 초기 포지션 ({symbol})", f"방향: {pos['side']}, 수량: {pos['size']}, 포지션수: {count}/1")
            else:
                log_debug(f"📊 초기 포지션 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug("❌ 초기 상태 로깅 실패", str(e), exc_info=True)

# ----------- 메인 실행 -----------
if __name__ == "__main__":
    log_initial_status()
    
    # 실시간 가격 모니터링
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    
    # 백업 포지션 상태 갱신
    threading.Thread(target=backup_position_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"포트 {port}에서 실행 (전략별 TP/SL 적용)")
    log_debug("✅ 전략별 TP/SL", "1분 0.2%/0.1%, 3분 0.3%/0.12%, 5분 0.4%/0.15%")
    log_debug("🔥 실시간 TP/SL", "Gate.io 가격 기준 전략별 자동 TP/SL 처리")
    log_debug("🔥 5분 전략", f"{CONFIG['strategy']['5m_multiplier']}배 수량, TP/SL: {CONFIG['trading']['strategy_tpsl']['5m']['tp']*100:.1f}%/{CONFIG['trading']['strategy_tpsl']['5m']['sl']*100:.1f}%")
    log_debug("📊 3분 전략", f"{CONFIG['strategy']['3m_multiplier']}배 수량, TP/SL: {CONFIG['trading']['strategy_tpsl']['3m']['tp']*100:.1f}%/{CONFIG['trading']['strategy_tpsl']['3m']['sl']*100:.1f}%")
    log_debug("⚡ 1분 전략", f"{CONFIG['strategy']['1m_multiplier']}배 수량, TP/SL: {CONFIG['trading']['strategy_tpsl']['1m']['tp']*100:.1f}%/{CONFIG['trading']['strategy_tpsl']['1m']['sl']*100:.1f}%")
    
    app.run(host="0.0.0.0", port=port, debug=False)
