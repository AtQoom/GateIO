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

# ----------- 로그 필터 및 설정 -----------
class CustomFilter(logging.Filter):
    def filter(self, record):
        filter_keywords = [
            "실시간 가격", "티커 수신", "포지션 없음", "계정 필드",
            "담보금 전환", "최종 선택", "전체 계정 정보",
            "웹소켓 핑", "핑 전송", "핑 성공", "ping",
            "Serving Flask app", "Debug mode", "WARNING: This is a development server"
        ]
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
SETTLE = "usdt"

# 🔥 확장된 심볼 매핑 (모든 가능한 형태 지원)
SYMBOL_MAPPING = {
    # 기본 형태
    "BTCUSDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", 
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT",
    "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT",
    
    # .P 형태 (영구선물)
    "BTCUSDT.P": "BTC_USDT",
    "ETHUSDT.P": "ETH_USDT", 
    "ADAUSDT.P": "ADA_USDT",
    "SUIUSDT.P": "SUI_USDT",
    "LINKUSDT.P": "LINK_USDT",
    "SOLUSDT.P": "SOL_USDT",
    "PEPEUSDT.P": "PEPE_USDT",
    
    # PERP 형태
    "BTCUSDTPERP": "BTC_USDT",
    "ETHUSDTPERP": "ETH_USDT", 
    "ADAUSDTPERP": "ADA_USDT",
    "SUIUSDTPERP": "SUI_USDT",
    "LINKUSDTPERP": "LINK_USDT",
    "SOLUSDTPERP": "SOL_USDT",
    "PEPEUSDTPERP": "PEPE_USDT",
    
    # 언더스코어 형태
    "BTC_USDT": "BTC_USDT",
    "ETH_USDT": "ETH_USDT",
    "ADA_USDT": "ADA_USDT",
    "SUI_USDT": "SUI_USDT",
    "LINK_USDT": "LINK_USDT",
    "SOL_USDT": "SOL_USDT",
    "PEPE_USDT": "PEPE_USDT",
}

# 🔥 심볼별 TP/SL 배수 설정 (파인스크립트와 일치)
SYMBOL_TPSL_MULTIPLIERS = {
    "BTC_USDT": {"tp": 0.7, "sl": 0.7},    # BTC: 70%
    "ETH_USDT": {"tp": 0.8, "sl": 0.8},    # ETH: 80%
    "SOL_USDT": {"tp": 0.9, "sl": 0.9},    # SOL: 90%
    # 기타 심볼은 기본값 (100%) 사용
}

def get_tpsl_multipliers(symbol):
    """심볼별 TP/SL 배수 반환"""
    return SYMBOL_TPSL_MULTIPLIERS.get(symbol, {"tp": 1.0, "sl": 1.0})

def parse_simple_alert(message):
    """간단한 파이프 구분 메시지 파싱"""
    try:
        if message.startswith("ENTRY:"):
            # ENTRY:long|BTCUSDT|5M_LONG|50000|1
            parts = message.split("|")
            if len(parts) >= 5:
                return {
                    "action": "entry",
                    "side": parts[0].split(":")[1],
                    "symbol": parts[1],
                    "strategy": parts[2],
                    "price": float(parts[3]),
                    "position_count": int(parts[4]),
                    "id": str(int(time.time())) + "_simple"
                }
        elif message.startswith("EXIT:"):
            # EXIT:long|BTCUSDT|stop_loss|50500|1.2
            parts = message.split("|")
            if len(parts) >= 5:
                return {
                    "action": "exit",
                    "side": parts[0].split(":")[1],
                    "symbol": parts[1],
                    "exit_reason": parts[2],
                    "price": float(parts[3]),
                    "pnl_pct": float(parts[4]),
                    "id": str(int(time.time())) + "_simple"
                }
    except Exception as e:
        log_debug("❌ 간단 메시지 파싱 실패", str(e))
    return None

def normalize_symbol(raw_symbol):
    """🔥 강화된 심볼 정규화"""
    if not raw_symbol:
        log_debug("❌ 심볼 정규화", "입력 심볼이 비어있음")
        return None
    
    symbol = str(raw_symbol).upper().strip()
    log_debug("🔍 심볼 정규화 시작", f"원본: '{raw_symbol}' -> 정리: '{symbol}'")
    
    # 직접 매핑
    if symbol in SYMBOL_MAPPING:
        result = SYMBOL_MAPPING[symbol]
        log_debug("✅ 직접 매핑 성공", f"'{symbol}' -> '{result}'")
        return result
    
    # .P 제거 시도
    if symbol.endswith('.P'):
        base_symbol = symbol[:-2]
        if base_symbol in SYMBOL_MAPPING:
            result = SYMBOL_MAPPING[base_symbol]
            log_debug("✅ .P 제거 후 매핑 성공", f"'{base_symbol}' -> '{result}'")
            return result
    
    # PERP 제거 시도
    if symbol.endswith('PERP'):
        base_symbol = symbol[:-4]
        if base_symbol in SYMBOL_MAPPING:
            result = SYMBOL_MAPPING[base_symbol]
            log_debug("✅ PERP 제거 후 매핑 성공", f"'{base_symbol}' -> '{result}'")
            return result
    
    # : 이후 제거 시도
    if ':' in symbol:
        base_symbol = symbol.split(':')[0]
        if base_symbol in SYMBOL_MAPPING:
            result = SYMBOL_MAPPING[base_symbol]
            log_debug("✅ : 제거 후 매핑 성공", f"'{base_symbol}' -> '{result}'")
            return result
    
    log_debug("❌ 심볼 매핑 실패", f"'{symbol}' 매핑을 찾을 수 없음")
    return None

SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.0001"),
        "min_notional": Decimal("10")
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.01"),
        "min_notional": Decimal("10")
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10"),
        "min_notional": Decimal("10")
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "min_notional": Decimal("10")
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "min_notional": Decimal("10")
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "min_notional": Decimal("10")
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10000000"),
        "min_notional": Decimal("10")
    },
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}

# === 🔥 중복 방지 시스템 ===
alert_cache = {}
recent_signals = {}
duplicate_prevention_lock = threading.RLock()

def is_duplicate_alert(alert_data):
    """중복 방지 (단일 진입)"""
    global alert_cache, recent_signals
    
    with duplicate_prevention_lock:
        current_time = time.time()
        alert_id = alert_data.get("id", "")
        symbol = alert_data.get("symbol", "")
        side = alert_data.get("side", "")
        action = alert_data.get("action", "")
        
        # 🔥 전략 이름 유연하게 처리
        strategy_name = alert_data.get("strategy", "")
        signal_type = alert_data.get("signal_type", "")
        
        log_debug("🔍 중복 체크 시작", f"ID: {alert_id}, Symbol: {symbol}, Side: {side}, Action: {action}")
        
        # 1. 같은 alert_id 확인
        if alert_id in alert_cache:
            cache_entry = alert_cache[alert_id]
            time_diff = current_time - cache_entry["timestamp"]
            
            if cache_entry["processed"] and time_diff < 300:
                log_debug("🚫 중복 ID 차단", f"ID: {alert_id}, {time_diff:.1f}초 전 처리됨")
                return True
        
        # 2. 같은 방향 신호 중복 확인 (60초 쿨다운)
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            if symbol_key in recent_signals:
                recent = recent_signals[symbol_key]
                time_diff = current_time - recent["time"]
                
                # 60초 쿨다운 체크
                if time_diff < 60:
                    log_debug("🚫 60초 쿨다운", 
                             f"{symbol} {side} 신호가 {time_diff:.1f}초 전에 이미 처리됨")
                    return True
        
        # 3. 캐시에 저장
        alert_cache[alert_id] = {"timestamp": current_time, "processed": False}
        
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            recent_signals[symbol_key] = {
                "side": side,
                "time": current_time,
                "action": action,
                "strategy": strategy_name,
                "signal_type": signal_type
            }
        
        # 4. 오래된 캐시 정리
        cutoff_time = current_time - 900
        alert_cache = {k: v for k, v in alert_cache.items() if v["timestamp"] > cutoff_time}
        recent_signals = {k: v for k, v in recent_signals.items() if v["time"] > cutoff_time}
        
        log_debug("✅ 신규 알림 승인", f"ID: {alert_id}, {symbol} {side} {action} ({strategy_name})")
        return False

def mark_alert_processed(alert_id):
    """알림 처리 완료 표시"""
    with duplicate_prevention_lock:
        if alert_id in alert_cache:
            alert_cache[alert_id]["processed"] = True
            log_debug("✅ 알림 처리 완료", f"ID: {alert_id}")

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
            elif hasattr(unified_accounts, 'equity'):
                equity = Decimal(str(unified_accounts.equity))
                account_cache.update({"time": now, "data": equity})
                return equity
        except Exception:
            pass
            
        try:
            from gate_api import WalletApi
            wallet_api = WalletApi(client)
            total_balance = wallet_api.get_total_balance(currency="USDT")
            if hasattr(total_balance, 'total'):
                equity = Decimal(str(total_balance.total))
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

def calculate_position_size(symbol, signal_type="none"):
    """
    🔥 신호별 차등 수량 계산
    - hybrid_enhanced (메인신호): 50% 수량
    - backup_enhanced (백업신호): 20% 수량
    """
    cfg = SYMBOL_CONFIG[symbol]
    
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    
    if price <= 0 or equity <= 0:
        log_debug(f"❌ 수량 계산 불가 ({symbol})", f"가격: {price}, 순자산: {equity}")
        return Decimal("0")
    
    try:
        # 🔥 신호별 포지션 배수
        if signal_type == "hybrid_enhanced":
            position_ratio = Decimal("0.5")  # 메인신호: 50%
            strategy_display = "🔥 메인신호 (50%)"
        elif signal_type == "backup_enhanced":
            position_ratio = Decimal("0.2")  # 백업신호: 20%
            strategy_display = "📊 백업신호 (20%)"
        else:
            position_ratio = Decimal("0.5")  # 기본값: 50%
            strategy_display = "🔧 기본신호 (50%)"
        
        log_debug(f"📈 신호 타입 감지 ({symbol})", f"{strategy_display}")
        
        # 수량 계산
        adjusted_equity = equity * position_ratio
        raw_qty = adjusted_equity / (price * cfg["contract_size"])
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        
        # 최소 주문 금액 체크
        order_value = final_qty * price * cfg["contract_size"]
        if order_value < cfg["min_notional"]:
            log_debug(f"⛔ 최소 주문 금액 미달 ({symbol})", f"{order_value} < {cfg['min_notional']} USDT")
            return Decimal("0")
        
        # 상세 로깅
        current_count = get_current_position_count(symbol)
        log_debug(f"📊 수량 계산 완료 ({symbol})", 
                 f"신호타입: {signal_type}, 순자산: {equity} USDT, "
                 f"배수: {position_ratio}x, 최종수량: {final_qty}, "
                 f"투자금액: {order_value:.2f} USDT, 현재포지션: {current_count}/1")
        
        return final_qty
        
    except Exception as e:
        log_debug(f"❌ 수량 계산 오류 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    """주문 실행"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 주문 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        min_qty = cfg["min_qty"]
        qty_dec = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        
        if qty_dec < min_qty:
            log_debug(f"⛔ 잘못된 수량 ({symbol})", f"{qty_dec} < 최소 {min_qty}")
            return False
            
        price = get_price(symbol)
        order_value = qty_dec * price * cfg["contract_size"]
        
        if order_value < cfg["min_notional"]:
            log_debug(f"⛔ 최소 주문 금액 미달 ({symbol})", f"{order_value} < {cfg['min_notional']}")
            return False
            
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        
        current_count = get_current_position_count(symbol)
        log_debug(f"📤 주문 시도 ({symbol})", 
                 f"{side.upper()} {float(qty_dec)} 계약, 주문금액: {order_value:.2f} USDT")
        
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        
        time.sleep(2)
        update_position_state(symbol)
        return True
        
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", f"{error_msg}")
        
        if retry > 0 and ("INVALID_PARAM" in error_msg or 
                         "POSITION_EMPTY" in error_msg or 
                         "INSUFFICIENT_AVAILABLE" in error_msg):
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // step) * step
            retry_qty = max(retry_qty, min_qty)
            log_debug(f"🔄 재시도 ({symbol})", f"{qty} → {retry_qty}")
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
                    "price": None, "side": None,
                    "size": Decimal("0"), "value": Decimal("0"),
                    "margin": Decimal("0"), "mode": "cross",
                    "count": 0
                }
                return True
            else:
                log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
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
                "price": None, "side": None,
                "size": Decimal("0"), "value": Decimal("0"), 
                "margin": Decimal("0"), "mode": "cross",
                "count": 0
            }
        return True
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e), exc_info=True)
        return False
    finally:
        position_lock.release()

def close_position(symbol):
    """포지션 청산"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 청산 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "신호에 의한 청산")
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "전체 포지션 청산")
        
        # 청산 후 recent_signals 초기화
        with duplicate_prevention_lock:
            keys_to_remove = [k for k in recent_signals.keys() if k.startswith(symbol + "_")]
            for key in keys_to_remove:
                del recent_signals[key]
        
        time.sleep(1)
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
        return False
    finally:
        position_lock.release()

def log_initial_status():
    """서버 시작시 초기 상태 로깅"""
    try:
        log_debug("🚀 서버 시작", "파인스크립트 알림 전용 모드 - 초기 상태 확인 중...")
        equity = get_total_collateral(force=True)
        log_debug("💰 총 자산(초기)", f"{equity} USDT")
        
        # 심볼별 TP/SL 설정 로깅
        log_debug("🎯 심볼별 TP/SL 설정", "")
        for symbol in SYMBOL_CONFIG:
            multipliers = get_tpsl_multipliers(symbol)
            base_tp = 0.0025  # 0.25% (파인스크립트 기본 익절률)
            base_sl = 0.002   # 0.2% (파인스크립트 기본 손절률)
            actual_tp = base_tp * multipliers["tp"]
            actual_sl = base_sl * multipliers["sl"]
            
            log_debug(f"📊 {symbol} TP/SL", 
                     f"TP: {actual_tp*100:.3f}% ({multipliers['tp']*100:.0f}%), "
                     f"SL: {actual_sl*100:.3f}% ({multipliers['sl']*100:.0f}%)")
        
        for symbol in SYMBOL_CONFIG:
            if not update_position_state(symbol, timeout=3):
                log_debug("❌ 포지션 조회 실패", f"초기화 중 {symbol} 상태 확인 불가")
                continue
            pos = position_state.get(symbol, {})
            if pos.get("side"):
                count = pos.get("count", 0)
                log_debug(
                    f"📊 초기 포지션 ({symbol})",
                    f"방향: {pos['side']}, 수량: {pos['size']}, 진입가: {pos['price']}, "
                    f"평가금액: {pos['value']} USDT, 포지션수: {count}/1"
                )
            else:
                log_debug(f"📊 초기 포지션 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug("❌ 초기 상태 로깅 실패", str(e), exc_info=True)

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    """헬스체크 엔드포인트"""
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    """🔥 파인스크립트 알림 웹훅 처리"""
    symbol = None
    alert_id = None
    raw_data = ""
    
    try:
        log_debug("🔄 웹훅 시작", "파인스크립트 신호 수신")
        
        # Raw 데이터 확인
        try:
            raw_data = request.get_data(as_text=True)
            log_debug("📄 Raw 데이터", f"길이: {len(raw_data)}, 내용: {raw_data[:500]}...")
        except Exception as e:
            log_debug("❌ Raw 데이터 읽기 실패", str(e))
            raw_data = ""
        
        if not raw_data or raw_data.strip() == "":
            log_debug("❌ 빈 데이터", "Raw 데이터가 비어있음")
            return jsonify({"error": "Empty data"}), 400
        
        # 메시지 파싱
        data = None
        
        # 간단한 형태 파싱
        if raw_data.startswith("ENTRY:") or raw_data.startswith("EXIT:"):
            data = parse_simple_alert(raw_data.strip())
            if data:
                log_debug("✅ 간단 메시지 파싱 성공", f"Action: {data.get('action')}, Symbol: {data.get('symbol')}")
            else:
                log_debug("❌ 간단 메시지 파싱 실패", f"Raw: {raw_data[:100]}")
                return jsonify({"error": "Simple message parsing failed"}), 400
        else:
            # JSON 파싱
            try:
                data = request.get_json(force=True)
                if data is None:
                    data = json.loads(raw_data)
                log_debug("✅ JSON 파싱 성공", "JSON 데이터 추출 완료")
            except Exception as e:
                log_debug("❌ JSON 파싱 실패", f"오류: {str(e)}, Raw: {raw_data[:100]}")
                return jsonify({"error": "JSON parsing failed", "raw_data": raw_data[:200]}), 400
                
        if not data:
            log_debug("❌ 빈 파싱 결과", "파싱된 데이터가 비어있음")
            return jsonify({"error": "Empty parsed data"}), 400
            
        log_debug("📥 웹훅 데이터", json.dumps(data, indent=2, ensure_ascii=False, default=str))
        
        # 필드 추출 (유연한 파싱)
        alert_id = data.get("id", "")
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower() if data.get("side") else ""
        action = data.get("action", "").lower() if data.get("action") else ""
        
        # 🔥 전략 이름 유연하게 처리 (어떤 이름이든 허용)
        strategy_name = data.get("strategy", "Unknown")
        signal_type = data.get("signal_type", "none")
        
        price = data.get("price", 0)
        position_count = data.get("position_count", 1)
        
        log_debug("🔍 필드 추출", f"ID: '{alert_id}', Symbol: '{raw_symbol}', Side: '{side}', Action: '{action}'")
        log_debug("🔍 추가 필드", f"Strategy: '{strategy_name}', SignalType: '{signal_type}', Price: {price}")
        
        # 필수 필드 검증 (최소한만 체크)
        missing_fields = []
        if not raw_symbol:
            missing_fields.append("symbol")
        if not action:
            missing_fields.append("action")
        
        # side는 entry일 때만 필수
        if action == "entry" and not side:
            missing_fields.append("side")
            
        if missing_fields:
            log_debug("❌ 필수 필드 누락", f"누락된 필드: {missing_fields}")
            return jsonify({"error": f"Missing required fields: {missing_fields}"}), 400
        
        log_debug("✅ 필수 필드 검증 통과", "모든 필드가 존재함")
        
        # 심볼 변환
        log_debug("🔍 심볼 정규화 시작", f"원본: '{raw_symbol}'")
        symbol = normalize_symbol(raw_symbol)
        
        if not symbol:
            log_debug("❌ 심볼 정규화 실패", f"'{raw_symbol}' -> None")
            return jsonify({"error": f"Symbol normalization failed: {raw_symbol}"}), 400
            
        if symbol not in SYMBOL_CONFIG:
            log_debug("❌ 심볼 설정 없음", f"'{symbol}' not in {list(SYMBOL_CONFIG.keys())}")
            return jsonify({"error": f"Symbol not supported: {symbol}"}), 400
        
        log_debug("✅ 심볼 매핑 성공", f"'{raw_symbol}' -> '{symbol}'")
        
        # 심볼별 TP/SL 설정 확인
        multipliers = get_tpsl_multipliers(symbol)
        log_debug("🎯 심볼별 TP/SL 배수", f"{symbol}: TP={multipliers['tp']*100:.0f}%, SL={multipliers['sl']*100:.0f}%")
        
        # 중복 방지 체크
        if is_duplicate_alert(data):
            log_debug("🚫 중복 알림 차단", f"Symbol: {symbol}, Side: {side}, Action: {action}")
            return jsonify({"status": "duplicate_ignored", "message": "중복 알림 무시됨"})
        
        log_debug("✅ 중복 체크 통과", "신규 알림으로 확인됨")
        
        # === 청산 신호 처리 ===
        if action == "exit":
            log_debug(f"🔄 청산 신호 처리 시작 ({symbol})", f"전략: {strategy_name}")
            
            update_position_state(symbol, timeout=1)
            current_side = position_state.get(symbol, {}).get("side")
            
            if not current_side:
                log_debug(f"⚠️ 청산 건너뜀 ({symbol})", "포지션 없음")
                success = True
            else:
                log_debug(f"🔄 포지션 청산 실행 ({symbol})", f"현재 포지션: {current_side}")
                success = close_position(symbol)
            
            if success and alert_id:
                mark_alert_processed(alert_id)
                
            log_debug(f"🔁 청산 결과 ({symbol})", f"성공: {success}")
            return jsonify({
                "status": "success" if success else "error", 
                "action": "exit",
                "symbol": symbol,
                "strategy": strategy_name,
                "signal_type": signal_type,
                "tpsl_multipliers": multipliers
            })
        
        # === 🔥 진입 신호 처리 ===
        if action == "entry" and side in ["long", "short"]:
            log_debug(f"🎯 진입 신호 처리 시작 ({symbol})", f"{side} 방향, 신호타입: {signal_type}")
            
            # 신호 타입별 물량 계산
            if signal_type == "hybrid_enhanced":
                quantity_display = "🔥 메인신호 (50%)"
                quantity_ratio = 0.5
            elif signal_type == "backup_enhanced":
                quantity_display = "📊 백업신호 (20%)"
                quantity_ratio = 0.2
            else:
                quantity_display = "🔧 기본신호 (50%)"
                quantity_ratio = 0.5
            
            log_debug(f"📈 신호 분석 ({symbol})", f"타입: {quantity_display}")
            
            if not update_position_state(symbol, timeout=1):
                log_debug(f"❌ 포지션 상태 조회 실패 ({symbol})", "")
                return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
            
            current_side = position_state.get(symbol, {}).get("side")
            desired_side = "buy" if side == "long" else "sell"
            
            log_debug(f"📊 현재 상태 ({symbol})", f"현재: {current_side}, 요청: {desired_side}")
            
            # 기존 포지션 처리 (단일 진입)
            if current_side:
                if current_side == desired_side:
                    log_debug("⚠️ 같은 방향 포지션 존재", "기존 포지션 유지")
                    if alert_id:
                        mark_alert_processed(alert_id)
                    return jsonify({"status": "same_direction", "message": "기존 포지션과 같은 방향"})
                else:
                    log_debug("🔄 역포지션 처리 시작", f"현재: {current_side} → 목표: {desired_side}")
                    if not close_position(symbol):
                        log_debug("❌ 역포지션 청산 실패", "")
                        return jsonify({"status": "error", "message": "역포지션 청산 실패"})
                    time.sleep(3)
                    if not update_position_state(symbol):
                        log_debug("❌ 역포지션 후 상태 갱신 실패", "")
            
            # 신호별 수량 계산
            log_debug(f"🧮 수량 계산 시작 ({symbol})", f"신호타입: {signal_type}")
            qty = calculate_position_size(symbol, signal_type)
            log_debug(f"🧮 수량 계산 완료 ({symbol})", f"{qty} 계약 ({quantity_display})")
            
            if qty <= 0:
                log_debug("❌ 수량 오류", f"계산된 수량: {qty}")
                return jsonify({"status": "error", "message": "수량 계산 오류"})
            
            # 주문 실행
            log_debug(f"📤 주문 실행 시작 ({symbol})", f"{desired_side} {qty} 계약")
            success = place_order(symbol, desired_side, qty)
            
            if success and alert_id:
                mark_alert_processed(alert_id)
            
            log_debug(f"📨 최종 결과 ({symbol})", f"주문 성공: {success}, {quantity_display}")
            
            return jsonify({
                "status": "success" if success else "error", 
                "action": "entry",
                "symbol": symbol,
                "side": side,
                "qty": float(qty),
                "strategy": strategy_name,
                "signal_type": signal_type,
                "quantity_display": quantity_display,
                "quantity_ratio": quantity_ratio,
                "entry_mode": "single",
                "max_positions": 1,
                "tpsl_multipliers": multipliers
            })
        
        # 잘못된 액션
        log_debug("❌ 잘못된 액션", f"Action: {action}, 지원되는 액션: entry, exit")
        return jsonify({"error": f"Invalid action: {action}"}), 400
        
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 웹훅 전체 실패 ({symbol or 'unknown'})", error_msg, exc_info=True)
        
        if alert_id:
            mark_alert_processed(alert_id)
            
        return jsonify({
            "status": "error", 
            "message": error_msg,
            "raw_data": raw_data[:200] if raw_data else "unavailable"
        }), 500

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
                    # 심볼별 TP/SL 정보 추가
                    multipliers = get_tpsl_multipliers(sym)
                    base_tp = 0.0025  # 0.25% (파인스크립트)
                    base_sl = 0.002   # 0.2% (파인스크립트)
                    actual_tp = base_tp * multipliers["tp"]
                    actual_sl = base_sl * multipliers["sl"]
                    
                    position_info = {k: float(v) if isinstance(v, Decimal) else v 
                                   for k, v in pos.items()}
                    position_info.update({
                        "tp_multiplier": multipliers["tp"],
                        "sl_multiplier": multipliers["sl"],
                        "actual_tp_pct": actual_tp * 100,
                        "actual_sl_pct": actual_sl * 100,
                        "base_tp_pct": base_tp * 100,
                        "base_sl_pct": base_sl * 100
                    })
                    positions[sym] = position_info
        
        # 중복 방지 상태 정보
        with duplicate_prevention_lock:
            duplicate_stats = {
                "alert_cache_size": len(alert_cache),
                "recent_signals_size": len(recent_signals),
                "recent_signals": {k: {
                    "side": v["side"], 
                    "action": v["action"], 
                    "strategy": v["strategy"],
                    "signal_type": v.get("signal_type", "unknown"),
                    "age_seconds": round(time.time() - v["time"], 1)
                } for k, v in recent_signals.items()}
            }
        
        # 심볼별 TP/SL 설정 정보
        tpsl_settings = {}
        for symbol in SYMBOL_CONFIG:
            multipliers = get_tpsl_multipliers(symbol)
            base_tp = 0.0025  # 파인스크립트 기본 익절률
            base_sl = 0.002   # 파인스크립트 기본 손절률
            tpsl_settings[symbol] = {
                "tp_multiplier": multipliers["tp"],
                "sl_multiplier": multipliers["sl"],
                "actual_tp_pct": base_tp * multipliers["tp"] * 100,
                "actual_sl_pct": base_sl * multipliers["sl"] * 100,
                "base_tp_pct": base_tp * 100,
                "base_sl_pct": base_sl * 100
            }
        
        return jsonify({
            "status": "running",
            "mode": "pinescript_alert_only",
            "timestamp": datetime.now().isoformat(),
            "margin_balance": float(equity),
            "positions": positions,
            "duplicate_prevention": duplicate_stats,
            "symbol_mappings": SYMBOL_MAPPING,
            "tpsl_settings": tpsl_settings,
            "pinescript_features": {
                "perfect_alerts": True,
                "future_prediction": True,
                "60s_cooldown": True,
                "signal_levels": {
                    "hybrid_enhanced": {"quantity": "50%", "priority": "HIGH", "description": "메인 신호"},
                    "backup_enhanced": {"quantity": "20%", "priority": "MEDIUM", "description": "백업 신호"}
                },
                "pyramiding": 1,
                "entry_timeframe": "15S",
                "exit_timeframe": "1M",
                "sl_tp_managed_by_server": True,
                "symbol_specific_tpsl": True,
                "enhanced_logging": True,
                "weighted_tpsl": True,
                "flexible_strategy_names": True
            }
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug_account():
    """계정 디버깅 정보"""
    try:
        acc = api.list_futures_accounts(SETTLE)
        debug_info = {
            "raw_response": str(acc),
            "total": str(getattr(acc, 'total', '없음')),
            "available": str(getattr(acc, 'available', '없음')),
            "margin_balance": str(getattr(acc, 'margin_balance', '없음')),
            "equity": str(getattr(acc, 'equity', '없음')),
        }
        return jsonify(debug_info)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/test-symbol/<symbol>", methods=["GET"])
def test_symbol_mapping(symbol):
    """심볼 매핑 및 TP/SL 테스트"""
    normalized = normalize_symbol(symbol)
    is_valid = normalized and normalized in SYMBOL_CONFIG
    multipliers = get_tpsl_multipliers(normalized) if normalized else {"tp": 1.0, "sl": 1.0}
    
    base_tp = 0.0025  # 파인스크립트 기본 익절률
    base_sl = 0.002   # 파인스크립트 기본 손절률
    
    return jsonify({
        "input": symbol,
        "normalized": normalized,
        "valid": is_valid,
        "config_exists": normalized in SYMBOL_CONFIG if normalized else False,
        "tpsl_multipliers": multipliers,
        "calculated_tpsl": {
            "tp_pct": base_tp * multipliers["tp"] * 100,
            "sl_pct": base_sl * multipliers["sl"] * 100,
            "base_tp_pct": base_tp * 100,
            "base_sl_pct": base_sl * 100
        },
        "all_mappings": {k: v for k, v in SYMBOL_MAPPING.items() if k.startswith(symbol.upper()[:3])}
    })

@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    """중복 방지 캐시 초기화"""
    global alert_cache, recent_signals
    with duplicate_prevention_lock:
        alert_cache.clear()
        recent_signals.clear()
    log_debug("🗑️ 캐시 초기화", "모든 중복 방지 캐시가 초기화되었습니다")
    return jsonify({"status": "cache_cleared", "message": "중복 방지 캐시가 초기화되었습니다"})

@app.route("/tpsl-settings", methods=["GET"])
def tpsl_settings():
    """심볼별 TP/SL 설정 조회"""
    try:
        base_tp = 0.0025  # 0.25% (파인스크립트)
        base_sl = 0.002   # 0.2% (파인스크립트)
        
        settings = {}
        for symbol in SYMBOL_CONFIG:
            multipliers = get_tpsl_multipliers(symbol)
            settings[symbol] = {
                "tp_multiplier": multipliers["tp"],
                "sl_multiplier": multipliers["sl"],
                "base_tp_pct": base_tp * 100,
                "base_sl_pct": base_sl * 100,
                "actual_tp_pct": base_tp * multipliers["tp"] * 100,
                "actual_sl_pct": base_sl * multipliers["sl"] * 100,
                "is_custom": symbol in SYMBOL_TPSL_MULTIPLIERS
            }
        
        return jsonify({
            "base_settings": {
                "tp_pct": base_tp * 100,
                "sl_pct": base_sl * 100
            },
            "symbol_settings": settings,
            "custom_symbols": list(SYMBOL_TPSL_MULTIPLIERS.keys())
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === 🔥 실시간 가격 모니터링 및 심볼별 TP/SL 처리 ===
async def send_ping(ws):
    """웹소켓 핑 전송"""
    while True:
        try:
            await ws.ping()
        except Exception:
            break
        await asyncio.sleep(30)

async def price_listener():
    """실시간 가격 모니터링 및 심볼별 TP/SL 처리"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60
    log_debug("📡 웹소켓 시작", f"Gate.io 가격 기준 심볼별 TP/SL 모니터링 - 심볼: {len(symbols)}개")
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=15) as ws:
                subscribe_msg = {
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }
                await ws.send(json.dumps(subscribe_msg))
                ping_task = asyncio.create_task(send_ping(ws))
                reconnect_delay = 5
                
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=45)
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("event") == "subscribe":
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
    """Gate.io 실시간 가격으로 심볼별 TP/SL 체크 (파인스크립트 기준)"""
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
            count = pos.get("count", 0)
            
            if not position_entry_price or size <= 0 or side not in ["buy", "sell"]:
                return
            
            # 🔥 파인스크립트와 일치하는 TP/SL 비율
            multipliers = get_tpsl_multipliers(contract)
            base_sl_pct = Decimal("0.002")   # 기본 0.2% (파인스크립트)
            base_tp_pct = Decimal("0.0025")  # 기본 0.25% (파인스크립트)
            
            sl_pct = base_sl_pct * Decimal(str(multipliers["sl"]))
            tp_pct = base_tp_pct * Decimal(str(multipliers["tp"]))
            
            if side == "buy":
                sl = position_entry_price * (1 - sl_pct)
                tp = position_entry_price * (1 + tp_pct)
                if price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", 
                             f"현재가:{price} <= SL:{sl} (진입가:{position_entry_price}, "
                             f"SL비율:{sl_pct*100:.3f}% [배수:{multipliers['sl']*100:.0f}%])")
                    close_position(contract)
                elif price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", 
                             f"현재가:{price} >= TP:{tp} (진입가:{position_entry_price}, "
                             f"TP비율:{tp_pct*100:.3f}% [배수:{multipliers['tp']*100:.0f}%])")
                    close_position(contract)
            else:
                sl = position_entry_price * (1 + sl_pct)
                tp = position_entry_price * (1 - tp_pct)
                if price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", 
                             f"현재가:{price} >= SL:{sl} (진입가:{position_entry_price}, "
                             f"SL비율:{sl_pct*100:.3f}% [배수:{multipliers['sl']*100:.0f}%])")
                    close_position(contract)
                elif price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", 
                             f"현재가:{price} <= TP:{tp} (진입가:{position_entry_price}, "
                             f"TP비율:{tp_pct*100:.3f}% [배수:{multipliers['tp']*100:.0f}%])")
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
            time.sleep(300)  # 5분마다 상태 갱신
        except Exception as e:
            log_debug("❌ 백업 포지션 루프 오류", str(e))
            time.sleep(300)

if __name__ == "__main__":
    log_initial_status()
    
    # Gate.io 실시간 가격 모니터링으로 심볼별 TP/SL 처리
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    
    # 백업 포지션 상태 갱신
    threading.Thread(target=backup_position_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"포트 {port}에서 실행 (파인스크립트 알림 전용)")
    log_debug("✅ TP/SL 가중치", "BTC 70%, ETH 80%, SOL 90%, 기타 100%")
    log_debug("✅ 기본 TP/SL", "TP 0.25%, SL 0.2% (파인스크립트)")
    log_debug("✅ 실제 TP/SL", "BTC 0.175%/0.14%, ETH 0.2%/0.16%, SOL 0.225%/0.18%")
    log_debug("🔥 실시간 TP/SL", "Gate.io 가격 기준 자동 TP/SL 처리")
    log_debug("✅ 진입신호", "파인스크립트 15초봉 극값 알림")
    log_debug("✅ 청산신호", "파인스크립트 1분봉 시그널 알림 + 실시간 TP/SL")
    log_debug("🔥 메인신호", "hybrid_enhanced → 50% 수량")
    log_debug("📊 백업신호", "backup_enhanced → 20% 수량")
    log_debug("✅ 진입 모드", "단일 진입 (Pyramiding=1)")
    log_debug("✅ 60초 쿨다운", "완벽한 중복 방지")
    log_debug("✅ 심볼 매핑", "모든 형태 지원 (.P, PERP 등)")
    log_debug("✅ 유연한 파싱", "전략 이름 제한 없음")
    log_debug("✅ 실거래 전용", "백테스트 불가 (알림 기반)")
    
    app.run(host="0.0.0.0", port=port, debug=False)
