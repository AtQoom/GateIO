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
    # BTC: 최소 0.001 BTC, 1계약 = 0.0001 BTC이므로 최소 10계약
    "BTC_USDT": {
        "min_qty": Decimal("1"),         # 최소 주문 수량: 10계약 (= 0.001 BTC)
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("0.0001"), # 계약 크기: 0.0001 BTC
        "min_notional": Decimal("10")     # 최소 주문 금액: 10 USDT
    },
    # ETH: 최소 0.01 ETH, 1계약 = 0.01 ETH이므로 최소 1계약
    "ETH_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약 (= 0.01 ETH)
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("0.01"), # 계약 크기: 0.01 ETH
        "min_notional": Decimal("10")
    },
    # ADA: 추정 최소 10 ADA, 1계약 = 10 ADA이므로 최소 1계약
    "ADA_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약 (= 10 ADA)
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("10"),   # 계약 크기: 10 ADA
        "min_notional": Decimal("10")
    },
    # SUI: 추정 최소 1 SUI, 1계약 = 1 SUI이므로 최소 1계약
    "SUI_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약 (= 1 SUI)
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("1"),    # 계약 크기: 1 SUI
        "min_notional": Decimal("10")
    },
    # LINK: 추정 최소 1 LINK, 1계약 = 1 LINK이므로 최소 1계약
    "LINK_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약 (= 1 LINK)
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("1"),    # 계약 크기: 1 LINK
        "min_notional": Decimal("10")
    },
    # SOL: 추정 최소 0.1 SOL, 1계약 = 0.1 SOL이므로 최소 1계약
    "SOL_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약 (= 0.1 SOL)
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("0.1"),  # 계약 크기: 0.1 SOL
        "min_notional": Decimal("10")
    },
    # 🔥 PEPE: 최소 10,000 PEPE, 1계약 = 10,000 PEPE이므로 최소 1계약
    "PEPE_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약 (= 10,000 PEPE)
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("1000000"), # 계약 크기: 10,000 PEPE
        "min_notional": Decimal("10")     # 최소 주문 금액: 10 USDT
    },
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}

# === 🔥 피라미딩 2 지원 중복 방지 시스템 ===
alert_cache = {}  # {alert_id: {"timestamp": time, "processed": bool}}
recent_signals = {}  # {symbol: {"side": side, "time": timestamp, "action": action, "strategy": strategy, "count": int}}
duplicate_prevention_lock = threading.RLock()

def is_duplicate_alert(alert_data):
    """피라미딩 2 지원 중복 방지 - 같은 방향 최대 2번까지 허용"""
    global alert_cache, recent_signals
    
    with duplicate_prevention_lock:
        current_time = time.time()
        alert_id = alert_data.get("id", "")
        symbol = alert_data.get("symbol", "")
        side = alert_data.get("side", "")
        action = alert_data.get("action", "")
        strategy_name = alert_data.get("strategy", "")
        position_count = alert_data.get("position_count", 1)
        
        # 1. 같은 alert_id가 이미 처리되었는지 확인
        if alert_id in alert_cache:
            cache_entry = alert_cache[alert_id]
            time_diff = current_time - cache_entry["timestamp"]
            
            if cache_entry["processed"] and time_diff < 300:  # 5분 이내 같은 ID는 중복
                log_debug("🚫 중복 ID 차단", f"ID: {alert_id}, {time_diff:.1f}초 전 처리됨")
                return True
        
        # 2. 피라미딩 2 지원 - 같은 방향 최대 2번까지 허용
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            if symbol_key in recent_signals:
                recent = recent_signals[symbol_key]
                time_diff = current_time - recent["time"]
                current_count = recent.get("count", 0)
                
                # 🔥 같은 방향 신호 - 120초 이내이고 이미 2번 진입했으면 차단
                if (recent["strategy"] == strategy_name and 
                    recent["action"] == "entry" and 
                    time_diff < 120 and 
                    current_count >= 2):
                    log_debug("🚫 피라미딩 한계 차단", 
                             f"{symbol} {side} {strategy_name} 이미 2번 진입 완료 (최근: {time_diff:.1f}초 전)")
                    return True
                
                # 🔥 14초 이내 동일 신호는 중복으로 간주
                if (recent["strategy"] == strategy_name and 
                    recent["action"] == "entry" and 
                    time_diff < 14):
                    log_debug("🚫 중복 진입 차단", 
                             f"{symbol} {side} {strategy_name} 신호가 {time_diff:.1f}초 전에 이미 처리됨")
                    return True
        
        # 3. 중복이 아니면 캐시에 저장
        alert_cache[alert_id] = {"timestamp": current_time, "processed": False}
        
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            # 피라미딩 카운트 업데이트
            if symbol_key in recent_signals:
                recent_signals[symbol_key]["count"] = position_count
                recent_signals[symbol_key]["time"] = current_time
            else:
                recent_signals[symbol_key] = {
                    "side": side,
                    "time": current_time,
                    "action": action,
                    "strategy": strategy_name,
                    "count": position_count
                }
        
        # 4. 오래된 캐시 정리 (메모리 관리)
        cutoff_time = current_time - 900  # 15분 이전 데이터 삭제
        alert_cache = {k: v for k, v in alert_cache.items() if v["timestamp"] > cutoff_time}
        recent_signals = {k: v for k, v in recent_signals.items() if v["time"] > cutoff_time}
        
        log_debug("✅ 신규 알림 승인", 
                 f"ID: {alert_id}, {symbol} {side} {action} ({strategy_name}) 포지션#{position_count}")
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

def get_current_position_count(symbol):
    """현재 포지션 개수 조회 (Gate.io API 기준)"""
    try:
        pos = api.get_position(SETTLE, symbol)
        size = Decimal(str(pos.size))
        if size == 0:
            return 0
        # Gate.io는 단일 포지션이므로 1 반환 (파인스크립트가 피라미딩 관리)
        return 1
    except Exception as e:
        if "POSITION_NOT_FOUND" in str(e):
            return 0
        log_debug(f"❌ 포지션 개수 조회 실패 ({symbol})", str(e))
        return 0

def calculate_position_size(symbol, strategy_type="standard"):
    """
    순자산(Account Equity) 기반으로 포지션 크기 계산
    파인스크립트의 default_qty_value=100 (순자산 100%) 반영
    피라미딩 2 지원 - 수량은 수정하지 않음 (레버리지로 조절)
    """
    cfg = SYMBOL_CONFIG[symbol]
    
    # 1. 순자산 조회 (전체 보유 자산)
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    
    if price <= 0 or equity <= 0:
        return Decimal("0")
    
    try:
        # 2. 전략별 포지션 크기 조정 (수량은 그대로 유지)
        if "backup" in strategy_type.lower():
            # 백업 전략은 50% 규모로 진입
            position_ratio = Decimal("0.5")
        else:
            # 메인 전략은 순자산 100% 사용 (파인스크립트와 동일)
            position_ratio = Decimal("1.0")
        
        # 3. 조정된 순자산으로 수량 계산 (피라미딩을 위해 수량 유지)
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
        
        # 6. 로깅 (피라미딩 정보 포함)
        current_count = get_current_position_count(symbol)
        log_debug(f"📊 수량 계산 ({symbol})", 
                 f"순자산: {equity} USDT, 사용비율: {position_ratio*100}%, "
                 f"가격: {price}, 수량: {final_qty}, 투자금액: {order_value:.2f} USDT, "
                 f"현재 포지션: {current_count}/2")
        
        return final_qty
        
    except Exception as e:
        log_debug(f"❌ 수량 계산 오류 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    """주문 실행 (피라미딩 2 지원)"""
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
                 f"{side.upper()} {float(qty_dec)} 계약, 주문금액: {order_value:.2f} USDT, "
                 f"피라미딩: {current_count + 1}/2")
        
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약 (피라미딩 #{current_count + 1})")
        
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
            entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            position_state[symbol] = {
                "price": entry_price,
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "value": value,
                "margin": value,
                "mode": "cross",
                "count": 1  # Gate.io는 단일 포지션
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
    """포지션 청산 - 파인스크립트가 SL/TP/청산 신호를 보낼 때만 실행"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 청산 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "파인스크립트 신호에 의한 청산")
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "전체 포지션 청산 (피라미딩 포함)")
        
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
        log_debug("🚀 서버 시작", "파인스크립트 피라미딩 2 연동 모드 - 초기 상태 확인 중...")
        equity = get_total_collateral(force=True)
        log_debug("💰 총 자산(초기)", f"{equity} USDT")
        
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
                    f"평가금액: {pos['value']} USDT, 포지션수: {count}/2"
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
    """파인스크립트 피라미딩 2 지원 웹훅 처리"""
    symbol = None
    alert_id = None
    try:
        log_debug("🔄 웹훅 시작", "파인스크립트 피라미딩 2 신호 수신")
        
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
            
        data = request.get_json()
        log_debug("📥 웹훅 데이터", json.dumps(data, indent=2))
        
        # === 🔥 파인스크립트 데이터 파싱 (피라미딩 지원) ===
        alert_id = data.get("id", "")
        raw_symbol = data.get("symbol", "").upper()
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        strategy_name = data.get("strategy", "")
        price = data.get("price", 0)
        position_count = data.get("position_count", 1)  # 피라미딩 정보
        
        log_debug("🔍 원본 심볼", f"수신된 심볼: '{raw_symbol}', 포지션#{position_count}")
        
        # 🔥 강화된 심볼 변환
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            log_debug("❌ 심볼 매핑 실패", f"'{raw_symbol}' -> '{symbol}' (지원되지 않는 심볼)")
            return jsonify({"error": f"Invalid symbol: {raw_symbol} -> {symbol}"}), 400
        
        log_debug("✅ 심볼 매핑 성공", f"'{raw_symbol}' -> '{symbol}'")
        
        # === 🔥 피라미딩 2 지원 중복 방지 체크 ===
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
        
        # === 🔥 피라미딩 2 지원 진입 신호 처리 ===
        if action == "entry" and side in ["long", "short"]:
            log_debug(f"🎯 피라미딩 진입 신호 ({symbol})", f"{side} 방향, 전략: {strategy_name}, 포지션#{position_count}")
            
            if not update_position_state(symbol, timeout=1):
                return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
            
            current_side = position_state.get(symbol, {}).get("side")
            current_count = get_current_position_count(symbol)
            desired_side = "buy" if side == "long" else "sell"
            
            # 🔥 피라미딩 2 로직 - 같은 방향 최대 2번까지 허용
            if current_side and current_side == desired_side:
                if current_count >= 2:
                    log_debug("🚫 피라미딩 한계 도달", 
                             f"현재: {current_side} x{current_count}, 요청: {desired_side} - 진입 불가 (최대 2개)")
                    if alert_id:
                        mark_alert_processed(alert_id)
                    return jsonify({"status": "pyramiding_limit", "message": "피라미딩 한계 도달 (최대 2개)"})
                else:
                    log_debug("✅ 피라미딩 진입 허용", 
                             f"현재: {current_side} x{current_count}, 요청: {desired_side} - 추가 진입")
            
            # 역포지션 처리 (기존 포지션 전체 청산)
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
                     f"{qty} 계약 (전략: {strategy_name}, 피라미딩#{position_count})")
            
            if qty <= 0:
                log_debug("❌ 수량 오류", f"계산된 수량: {qty}")
                return jsonify({"status": "error", "message": "수량 계산 오류"})
            
            # 주문 실행
            success = place_order(symbol, desired_side, qty)
            
            if success and alert_id:
                mark_alert_processed(alert_id)
            
            log_debug(f"📨 최종 결과 ({symbol})", 
                     f"주문 성공: {success}, 전략: {strategy_name}, 피라미딩#{position_count}")
            
            return jsonify({
                "status": "success" if success else "error", 
                "qty": float(qty),
                "strategy": strategy_name,
                "position_count": position_count,
                "pyramiding_mode": "enabled",
                "max_positions": 2
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
    """서버 상태 조회 (피라미딩 2 정보 포함)"""
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        
        for sym in SYMBOL_CONFIG:
            if update_position_state(sym, timeout=1):
                pos = position_state.get(sym, {})
                if pos.get("side"):
                    positions[sym] = {k: float(v) if isinstance(v, Decimal) else v 
                                    for k, v in pos.items()}
        
        # 중복 방지 상태 정보 (피라미딩 포함)
        with duplicate_prevention_lock:
            duplicate_stats = {
                "alert_cache_size": len(alert_cache),
                "recent_signals_size": len(recent_signals),
                "recent_signals": {k: {
                    "side": v["side"], 
                    "action": v["action"], 
                    "strategy": v["strategy"],
                    "count": v.get("count", 1),
                    "age_seconds": round(time.time() - v["time"], 1)
                } for k, v in recent_signals.items()}
            }
        
        return jsonify({
            "status": "running",
            "mode": "pinescript_pyramiding_2",
            "timestamp": datetime.now().isoformat(),
            "margin_balance": float(equity),
            "positions": positions,
            "duplicate_prevention": duplicate_stats,
            "pinescript_features": {
                "perfect_alerts": True,
                "future_prediction": True,
                "backup_signals": True,
                "pyramiding": 2,
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
    """중복 방지 캐시 초기화 (피라미딩 정보 포함)"""
    global alert_cache, recent_signals
    with duplicate_prevention_lock:
        alert_cache.clear()
        recent_signals.clear()
    log_debug("🗑️ 캐시 초기화", "모든 중복 방지 캐시가 초기화되었습니다 (피라미딩 정보 포함)")
    return jsonify({"status": "cache_cleared", "message": "중복 방지 캐시가 초기화되었습니다"})

@app.route("/pyramiding-status", methods=["GET"])
def pyramiding_status():
    """피라미딩 상태 조회"""
    try:
        pyramiding_info = {}
        
        for symbol in SYMBOL_CONFIG:
            current_count = get_current_position_count(symbol)
            pos = position_state.get(symbol, {})
            
            pyramiding_info[symbol] = {
                "current_positions": current_count,
                "max_positions": 2,
                "can_add_position": current_count < 2,
                "side": pos.get("side"),
                "size": float(pos.get("size", 0)) if pos.get("size") else 0,
                "value": float(pos.get("value", 0)) if pos.get("value") else 0
            }
        
        return jsonify({
            "pyramiding_enabled": True,
            "max_positions_per_symbol": 2,
            "symbols": pyramiding_info
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    log_debug("📡 웹소켓 시작", f"Gate.io 가격 기준 TP/SL 모니터링 - 심볼: {len(symbols)}개 (피라미딩 2 지원)")
    
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
    """Gate.io 실시간 가격으로 TP/SL 체크 (피라미딩 포지션 포함)"""
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
            count = pos.get("count", 0)
            
            if not entry or size <= 0 or side not in ["buy", "sell"]:
                return
            
            # TP/SL 비율 (파인스크립트와 동일)
            sl_pct = Decimal("0.0035")  # 0.35%
            tp_pct = Decimal("0.006")   # 0.6%
            
            if side == "buy":
                sl = entry * (1 - sl_pct)
                tp = entry * (1 + tp_pct)
                if price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{price} <= SL:{sl} (진입가:{entry}, 포지션:{count}개)")
                    close_position(contract)
                elif price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{price} >= TP:{tp} (진입가:{entry}, 포지션:{count}개)")
                    close_position(contract)
            else:
                sl = entry * (1 + sl_pct)
                tp = entry * (1 - tp_pct)
                if price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{price} >= SL:{sl} (진입가:{entry}, 포지션:{count}개)")
                    close_position(contract)
                elif price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{price} <= TP:{tp} (진입가:{entry}, 포지션:{count}개)")
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
             f"포트 {port}에서 실행 (피라미딩 2 하이브리드 모드)\n"
             f"✅ TP/SL: 서버에서 Gate.io 가격 기준으로 처리\n"
             f"✅ 진입/청산 신호: 파인스크립트 알림으로 처리\n"
             f"✅ 피라미딩: 같은 방향 최대 2번 진입 지원\n"
             f"✅ 중복 방지: 완벽한 알림 시스템 연동\n"
             f"✅ 심볼 매핑: 모든 형태 지원 (.P, PERP 등)")
    
    app.run(host="0.0.0.0", port=port, debug=False)
