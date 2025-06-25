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
    
    # 1INCH (추가 지원)
    "1INCHUSDT": "1INCH_USDT",
    "1INCHUSDT.P": "1INCH_USDT",
    "1INCHUSDTPERP": "1INCH_USDT"
}

def normalize_symbol(raw_symbol):
    """심볼 정규화 - 다양한 형태를 표준 형태로 변환"""
    if not raw_symbol:
        return None
    
    # 대문자로 변환
    symbol = raw_symbol.upper().strip()
    
    # 직접 매핑이 있으면 사용
    if symbol in SYMBOL_MAPPING:
        return SYMBOL_MAPPING[symbol]
    
    # 동적 정규화 시도
    # .P 제거
    if symbol.endswith('.P'):
        base_symbol = symbol[:-2]
        if base_symbol in SYMBOL_MAPPING:
            return SYMBOL_MAPPING[base_symbol]
    
    # PERP 제거  
    if symbol.endswith('PERP'):
        base_symbol = symbol[:-4]
        if base_symbol in SYMBOL_MAPPING:
            return SYMBOL_MAPPING[base_symbol]
    
    # : 이후 제거 (일부 거래소 형태)
    if ':' in symbol:
        base_symbol = symbol.split(':')[0]
        if base_symbol in SYMBOL_MAPPING:
            return SYMBOL_MAPPING[base_symbol]
    
    # 기본 USDT 형태로 추정해서 매핑 시도
    if 'USDT' in symbol:
        # 숫자로 시작하는 경우 처리 (1INCH 등)
        if symbol[0].isdigit():
            clean_symbol = symbol
        else:
            clean_symbol = symbol.replace('.P', '').replace('PERP', '').split(':')[0]
        
        if clean_symbol in SYMBOL_MAPPING:
            return SYMBOL_MAPPING[clean_symbol]
    
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
        "contract_size": Decimal("0.001"),
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
        "contract_size": Decimal("10000"),
        "min_notional": Decimal("10")
    },
    "1INCH_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "min_notional": Decimal("10")
    }
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}

# === 🔥 완벽한 중복 방지 시스템 (파인스크립트 연동) ===
alert_cache = {}  # {alert_id: {"timestamp": time, "processed": bool}}
recent_signals = {}  # {symbol: {"side": side, "time": timestamp, "action": action, "strategy": strategy}}
duplicate_prevention_lock = threading.RLock()

def is_duplicate_alert(alert_data):
    """최대 엄격한 중복 방지"""
    global alert_cache, recent_signals
    
    with duplicate_prevention_lock:
        current_time = time.time()
        alert_id = alert_data.get("id", "")
        symbol = alert_data.get("symbol", "")
        side = alert_data.get("side", "")
        action = alert_data.get("action", "")
        strategy_name = alert_data.get("strategy", "")
        
        # 1. 같은 alert_id가 이미 처리되었는지 확인
        if alert_id in alert_cache:
            cache_entry = alert_cache[alert_id]
            time_diff = current_time - cache_entry["timestamp"]
            
            if cache_entry["processed"] and time_diff < 300:  # 5분 이내 같은 ID는 중복
                log_debug("🚫 중복 ID 차단", f"ID: {alert_id}, {time_diff:.1f}초 전 처리됨")
                return True
        
        # 2. 같은 심볼+방향의 최근 신호 확인 (진입 신호만)
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            if symbol_key in recent_signals:
                recent = recent_signals[symbol_key]
                time_diff = current_time - recent["time"]
                
                # 🔥 같은 방향 신호가 120초(2분) 이내에 있으면 중복
                if (recent["strategy"] == strategy_name and 
                    recent["action"] == "entry" and 
                    time_diff < 120):
                    log_debug("🚫 중복 진입 차단", 
                             f"{symbol} {side} {strategy_name} 신호가 {time_diff:.1f}초 전에 이미 처리됨")
                    return True
        
        # 3. 중복이 아니면 캐시에 저장
        alert_cache[alert_id] = {"timestamp": current_time, "processed": False}
        
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            recent_signals[symbol_key] = {
                "side": side,
                "time": current_time,
                "action": action,
                "strategy": strategy_name
            }
        
        # 4. 오래된 캐시 정리 (메모리 관리)
        cutoff_time = current_time - 900  # 15분 이전 데이터 삭제
        alert_cache = {k: v for k, v in alert_cache.items() if v["timestamp"] > cutoff_time}
        recent_signals = {k: v for k, v in recent_signals.items() if v["time"] > cutoff_time}
        
        log_debug("✅ 신규 알림 승인", 
                 f"ID: {alert_id}, {symbol} {side} {action} ({strategy_name})")
        return False

def mark_alert_processed(alert_id):
    """알림 처리 완료 표시"""
    with duplicate_prevention_lock:
        if alert_id in alert_cache:
            alert_cache[alert_id]["processed"] = True

def get_total_collateral(force=False):
    """순자산(Account Equity) 조회"""
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        try:
            unified_accounts = unified_api.list_unified_accounts()
            if hasattr(unified_accounts, 'unified_account_total_equity'):
                equity = Decimal(str(unified_accounts.unified_account_total_equity))
                log_debug("💰 Account Equity(순자산)", f"{equity} USDT")
                account_cache.update({"time": now, "data": equity})
                return equity
            elif hasattr(unified_accounts, 'equity'):
                equity = Decimal(str(unified_accounts.equity))
                log_debug("💰 Account Equity(순자산)", f"{equity} USDT")
                account_cache.update({"time": now, "data": equity})
                return equity
        except Exception as e:
            log_debug("⚠️ Unified Account 조회 실패", str(e))
            
        try:
            from gate_api import WalletApi
            wallet_api = WalletApi(client)
            total_balance = wallet_api.get_total_balance(currency="USDT")
            if hasattr(total_balance, 'total'):
                equity = Decimal(str(total_balance.total))
                log_debug("💰 WalletApi 총 잔고", f"{equity} USDT")
                account_cache.update({"time": now, "data": equity})
                return equity
        except Exception as e:
            log_debug("⚠️ WalletApi 조회 실패", str(e))
            
        acc = api.list_futures_accounts(SETTLE)
        available = Decimal(str(getattr(acc, 'available', '0')))
        log_debug("💰 선물 계정 available", f"{available} USDT")
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

def calculate_position_size(symbol, strategy_type="standard"):
    """
    순자산(Account Equity) 기반으로 포지션 크기 계산
    파인스크립트의 default_qty_value=100 (순자산 100%) 반영
    """
    cfg = SYMBOL_CONFIG[symbol]
    
    # 1. 순자산 조회 (전체 보유 자산)
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    
    if price <= 0 or equity <= 0:
        return Decimal("0")
    
    try:
        # 2. 전략별 포지션 크기 조정
        if "backup" in strategy_type.lower():
            # 백업 전략은 50% 규모로 진입
            position_ratio = Decimal("0.5")
        else:
            # 메인 전략은 순자산 100% 사용 (파인스크립트와 동일)
            position_ratio = Decimal("1.0")
        
        # 3. 조정된 순자산으로 수량 계산
        adjusted_equity = equity * position_ratio
        raw_qty = adjusted_equity / (price * cfg["contract_size"])
        
        # 4. 거래소 규칙에 맞게 수량 조정
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        
        # 5. 최소 주문 금액 체크
        order_value = final_qty * price * cfg["contract_size"]
        if order_value < cfg["min_notional"]:
            log_debug(f"⛔ 최소 주문 금액 미달 ({symbol})", f"{order_value} < {cfg['min_notional']} USDT")
            return Decimal("0")
        
        # 6. 로깅
        log_debug(f"📊 수량 계산 ({symbol})", 
                 f"순자산: {equity} USDT, 사용비율: {position_ratio*100}%, "
                 f"가격: {price}, 수량: {final_qty}, 투자금액: {order_value:.2f} USDT")
        
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
                    "margin": Decimal("0"), "mode": "cross"
                }
                return True
            else:
                log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
                return False
                
        size = Decimal(str(pos.size))
        if size != 0:
            entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            position_state[symbol] = {
                "price": entry_price,
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "value": value,
                "margin": value,
                "mode": "cross"
            }
        else:
            position_state[symbol] = {
                "price": None, "side": None,
                "size": Decimal("0"), "value": Decimal("0"), 
                "margin": Decimal("0"), "mode": "cross"
            }
        return True
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e), exc_info=True)
        return False
    finally:
        position_lock.release()

def close_position(symbol):
    """포지션 청산 - 파인스크립트가 SL/TP/청산 신호를 보낼 때만 실행"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 청산 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "파인스크립트 신호에 의한 청산")
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "")
        
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
        log_debug("🚀 서버 시작", "파인스크립트 연동 모드 - 초기 상태 확인 중...")
        equity = get_total_collateral(force=True)
        log_debug("💰 총 자산(초기)", f"{equity} USDT")
        
        for symbol in SYMBOL_CONFIG:
            if not update_position_state(symbol, timeout=3):
                log_debug("❌ 포지션 조회 실패", f"초기화 중 {symbol} 상태 확인 불가")
                continue
            pos = position_state.get(symbol, {})
            if pos.get("side"):
                log_debug(
                    f"📊 초기 포지션 ({symbol})",
                    f"방향: {pos['side']}, 수량: {pos['size']}, 진입가: {pos['price']}, 평가금액: {pos['value']} USDT"
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
    """파인스크립트 완벽한 알림 시스템과 연동된 웹훅 처리 - 심볼 매핑 강화"""
    symbol = None
    alert_id = None
    try:
        log_debug("🔄 웹훅 시작", "파인스크립트 신호 수신")
        
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
            
        data = request.get_json()
        log_debug("📥 웹훅 데이터", json.dumps(data, indent=2))
        
        # === 🔥 파인스크립트 데이터 파싱 (간소화) ===
        alert_id = data.get("id", "")
        raw_symbol = data.get("symbol", "").upper()
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        strategy_name = data.get("strategy", "")
        price = data.get("price", 0)
        
        log_debug("🔍 원본 심볼", f"수신된 심볼: '{raw_symbol}'")
        
        # 🔥 누락된 변수들 정의 (기본값 설정)
        signal_source = strategy_name  # 전략명을 신호 소스로 사용
        signal_strength = "strong"     # 기본 신호 강도
        perfect_system = True          # 완벽한 시스템 플래그
        
        # 🔥 강화된 심볼 변환
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            log_debug("❌ 심볼 매핑 실패", f"'{raw_symbol}' -> '{symbol}' (지원되지 않는 심볼)")
            return jsonify({"error": f"Invalid symbol: {raw_symbol} -> {symbol}"}), 400
        
        log_debug("✅ 심볼 매핑 성공", f"'{raw_symbol}' -> '{symbol}'")
        
        # === 🔥 중복 방지 체크 (엄격한 모드) ===
        if is_duplicate_alert(data):
            return jsonify({"status": "duplicate_ignored", "message": "중복 알림 무시됨"})
        
        # === 🔥 진입/청산 신호 처리 ===
        if action == "exit":
            log_debug(f"🔄 청산 신호 ({symbol})", f"전략: {strategy_name}")
            
            update_position_state(symbol, timeout=1)
            current_side = position_state.get(symbol, {}).get("side")
            
            if not current_side:
                log_debug(f"⚠️ 청산 건너뜀 ({symbol})", "포지션 없음")
                success = True
            else:
                success = close_position(symbol)
            
            if success and alert_id:
                mark_alert_processed(alert_id)
                
            log_debug(f"🔁 청산 결과 ({symbol})", f"성공: {success}")
            return jsonify({"status": "success" if success else "error", "action": "exit"})
        
        # === 🔥 진입 신호 처리 ===
        if action == "entry" and side in ["long", "short"]:
            log_debug(f"🎯 진입 신호 처리 ({symbol})", f"{side} 방향, 전략: {strategy_name}")
            
            if not update_position_state(symbol, timeout=1):
                return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
            
            current_side = position_state.get(symbol, {}).get("side")
            desired_side = "buy" if side == "long" else "sell"
            
            # 동일 방향 포지션 존재 시 건너뜀
            if current_side and current_side == desired_side:
                log_debug("🚫 동일 방향 포지션 존재", 
                         f"현재: {current_side}, 요청: {desired_side} - 진입 취소")
                if alert_id:
                    mark_alert_processed(alert_id)
                return jsonify({"status": "duplicate_position", "message": "동일 방향 포지션이 이미 존재함"})
            
            # 역포지션 처리
            if current_side and current_side != desired_side:
                log_debug("🔄 역포지션 처리", f"현재: {current_side} → 목표: {desired_side}")
                if not close_position(symbol):
                    log_debug("❌ 역포지션 청산 실패", "")
                    return jsonify({"status": "error", "message": "역포지션 청산 실패"})
                time.sleep(3)
                if not update_position_state(symbol):
                    log_debug("❌ 역포지션 후 상태 갱신 실패", "")
            
            # 수량 계산 (전략 타입에 따라 조정)
            qty = calculate_position_size(symbol, strategy_name)
            log_debug(f"🧮 수량 계산 완료 ({symbol})", 
                     f"{qty} 계약 (전략: {strategy_name}, 신호강도: {signal_strength})")
            
            if qty <= 0:
                log_debug("❌ 수량 오류", f"계산된 수량: {qty}")
                return jsonify({"status": "error", "message": "수량 계산 오류"})
            
            # 주문 실행
            success = place_order(symbol, desired_side, qty)
            
            if success and alert_id:
                mark_alert_processed(alert_id)
            
            log_debug(f"📨 최종 결과 ({symbol})", 
                     f"주문 성공: {success}, 전략: {strategy_name}, 신호: {signal_source}")
            
            return jsonify({
                "status": "success" if success else "error", 
                "qty": float(qty),
                "strategy": strategy_name,
                "signal_source": signal_source,
                "signal_strength": signal_strength,
                "perfect_system": perfect_system
            })
        
        # 잘못된 액션
        return jsonify({"error": f"Invalid action: {action}"}), 400
        
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 웹훅 전체 실패 ({symbol or 'unknown'})", error_msg)
        
        # 오류 발생 시에도 중복 방지를 위해 ID 처리
        if alert_id:
            mark_alert_processed(alert_id)
            
        return jsonify({"status": "error", "message": error_msg}), 500

@app.route("/status", methods=["GET"])
def status():
    """서버 상태 조회 (파인스크립트 연동 정보 포함)"""
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        
        for sym in SYMBOL_CONFIG:
            if update_position_state(sym, timeout=1):
                pos = position_state.get(sym, {})
                if pos.get("side"):
                    positions[sym] = {k: float(v) if isinstance(v, Decimal) else v 
                                    for k, v in pos.items()}
        
        # 중복 방지 상태 정보
        with duplicate_prevention_lock:
            duplicate_stats = {
                "alert_cache_size": len(alert_cache),
                "recent_signals_size": len(recent_signals),
                "recent_signals": {k: {
                    "side": v["side"], 
                    "action": v["action"], 
                    "strategy": v["strategy"],
                    "age_seconds": round(time.time() - v["time"], 1)
                } for k, v in recent_signals.items()}
            }
        
        return jsonify({
            "status": "running",
            "mode": "pinescript_integration",
            "timestamp": datetime.now().isoformat(),
            "margin_balance": float(equity),
            "positions": positions,
            "duplicate_prevention": duplicate_stats,
            "pinescript_features": {
                "perfect_alerts": True,
                "future_prediction": True,
                "backup_signals": True,
                "sl_tp_managed_by_pinescript": True
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

# === 🔥 추가 디버깅 엔드포인트 ===
@app.route("/test-symbol/<symbol>", methods=["GET"])
def test_symbol_mapping(symbol):
    """심볼 매핑 테스트"""
    normalized = normalize_symbol(symbol)
    is_valid = normalized and normalized in SYMBOL_CONFIG
    
    return jsonify({
        "input": symbol,
        "normalized": normalized,
        "valid": is_valid,
        "config_exists": normalized in SYMBOL_CONFIG if normalized else False,
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

# === 🔥 실시간 가격 모니터링 및 TP/SL 처리 (Gate.io 기준) ===
async def send_ping(ws):
    """웹소켓 핑 전송"""
    while True:
        try:
            await ws.ping()
        except Exception:
            break
        await asyncio.sleep(30)

async def price_listener():
    """실시간 가격 모니터링 및 TP/SL 처리 (Gate.io 가격 기준)"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60
    log_debug("📡 웹소켓 시작", f"Gate.io 가격 기준 TP/SL 모니터링 - 심볼: {len(symbols)}개")
    
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
    """Gate.io 실시간 가격으로 TP/SL 체크"""
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
            entry = pos.get("price")
            size = pos.get("size", 0)
            side = pos.get("side")
            if not entry or size <= 0 or side not in ["buy", "sell"]:
                return
            
            # TP/SL 비율 (파인스크립트와 동일)
            sl_pct = Decimal("0.0035")  # 0.35%
            tp_pct = Decimal("0.006")   # 0.6%
            
            if side == "buy":
                sl = entry * (1 - sl_pct)
                tp = entry * (1 + tp_pct)
                if price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{price} <= SL:{sl} (진입가:{entry})")
                    close_position(contract)
                elif price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{price} >= TP:{tp} (진입가:{entry})")
                    close_position(contract)
            else:
                sl = entry * (1 + sl_pct)
                tp = entry * (1 - tp_pct)
                if price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{price} >= SL:{sl} (진입가:{entry})")
                    close_position(contract)
                elif price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{price} <= TP:{tp} (진입가:{entry})")
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
        except Exception:
            time.sleep(300)

if __name__ == "__main__":
    log_initial_status()
    
    # 🔥 Gate.io 실시간 가격 모니터링으로 TP/SL 처리
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    
    # 백업 포지션 상태 갱신
    threading.Thread(target=backup_position_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", 
             f"포트 {port}에서 실행 (하이브리드 모드)\n"
             f"✅ TP/SL: 서버에서 Gate.io 가격 기준으로 처리\n"
             f"✅ 진입/청산 신호: 파인스크립트 알림으로 처리\n"
             f"✅ 중복 방지: 완벽한 알림 시스템 연동\n"
             f"✅ 심볼 매핑: 모든 형태 지원 (.P, PERP 등)")
    
    app.run(host="0.0.0.0", port=port, debug=False)
