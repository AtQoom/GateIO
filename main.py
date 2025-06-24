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

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT",
    "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT"
}

SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.0001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10000"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
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
actual_entry_prices = {}

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

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT",
    "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT"
}

SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.0001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "min_notional": Decimal("10")
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10000"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
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
actual_entry_prices = {}

# === 🔥 중복 진입 방지 시스템 ===
alert_cache = {}  # {alert_id: timestamp}
recent_signals = {}  # {symbol: {"side": side, "time": timestamp, "action": action}}
duplicate_prevention_lock = threading.RLock()

def is_duplicate_alert(alert_id, symbol, side, action):
    """중복 알림 체크 및 방지"""
    global alert_cache  # 🔥 전역 변수로 명시적 선언
    
    with duplicate_prevention_lock:
        current_time = time.time()
        
        # 1. 같은 alert_id가 이미 처리되었는지 확인
        if alert_id in alert_cache:
            time_diff = current_time - alert_cache[alert_id]
            if time_diff < 60:  # 1분 이내 같은 ID는 중복
                log_debug("🚫 중복 알림 차단", f"ID: {alert_id}, {time_diff:.1f}초 전 처리됨")
                return True
        
        # 2. 같은 심볼의 최근 신호 확인 (진입 신호만)
        if action == "entry" and symbol in recent_signals:
            recent = recent_signals[symbol]
            time_diff = current_time - recent["time"]
            
            # 같은 방향 신호가 30초 이내에 있으면 중복
            if recent["side"] == side and recent["action"] == "entry" and time_diff < 30:
                log_debug("🚫 중복 진입 차단", f"{symbol} {side} 신호가 {time_diff:.1f}초 전에 이미 처리됨")
                return True
        
        # 3. 중복이 아니면 캐시에 저장
        alert_cache[alert_id] = current_time
        
        if action == "entry":
            recent_signals[symbol] = {
                "side": side,
                "time": current_time,
                "action": action
            }
        
        # 4. 오래된 캐시 정리 (메모리 관리) - 🔥 수정
        cutoff_time = current_time - 300  # 5분 이전 데이터 삭제
        alert_cache = {k: v for k, v in alert_cache.items() if v > cutoff_time}
        
        log_debug("✅ 신규 알림 승인", f"ID: {alert_id}, {symbol} {side} {action}")
        return False

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

def calculate_position_size(symbol):
    """
    순자산(Account Equity) 기반으로 포지션 크기 계산
    실제 레버리지 1배로 동작
    """
    cfg = SYMBOL_CONFIG[symbol]
    
    # 1. 순자산 조회 (전체 보유 자산)
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    
    if price <= 0 or equity <= 0:
        return Decimal("0")
    
    try:
        # 2. 순자산 전액을 기준으로 수량 계산 (레버리지 1배)
        raw_qty = equity / (price * cfg["contract_size"])
        
        # 3. 거래소 규칙에 맞게 수량 조정
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        
        # 4. 최소 주문 금액 체크
        order_value = final_qty * price * cfg["contract_size"]
        if order_value < cfg["min_notional"]:
            log_debug(f"⛔ 최소 주문 금액 미달 ({symbol})", f"{order_value} < {cfg['min_notional']} USDT")
            return Decimal("0")
        
        # 5. 로깅 (순자산 기반임을 명시)
        log_debug(f"📊 수량 계산 ({symbol})", 
                 f"순자산: {equity} USDT, 가격: {price}, 계약크기: {cfg['contract_size']}, "
                 f"수량(계약): {final_qty}, 투자금액: {order_value:.2f} USDT (레버리지 1배)")
        
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
        log_debug(f"📤 주문 시도 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약, 주문금액: {order_value:.2f} USDT (레버리지 1배)")
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        time.sleep(2)
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", f"{error_msg}")
        if retry > 0 and ("INVALID_PARAM" in error_msg or "POSITION_EMPTY" in error_msg or "INSUFFICIENT_AVAILABLE" in error_msg):
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
                if symbol in actual_entry_prices:
                    del actual_entry_prices[symbol]
                return True
            else:
                log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
                return False
        size = Decimal(str(pos.size))
        if size != 0:
            api_entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            actual_price = actual_entry_prices.get(symbol)
            entry_price = actual_price if actual_price else api_entry_price
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
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
            }
            if symbol in actual_entry_prices:
                del actual_entry_prices[symbol]
        return True
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e), exc_info=True)
        return False
    finally:
        position_lock.release()

def log_initial_status():
    """서버 시작시 초기 상태 로깅"""
    try:
        log_debug("🚀 서버 시작", "초기 상태 확인 중...")
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

def close_position(symbol):
    """포지션 청산"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 청산 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "size=0 주문")
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "")
        if symbol in actual_entry_prices:
            del actual_entry_prices[symbol]
        time.sleep(1)
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
        return False
    finally:
        position_lock.release()

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    """헬스체크 엔드포인트"""
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    """TradingView 웹훅 처리 (중복 방지 기능 포함)"""
    symbol = None
    try:
        log_debug("🔄 웹훅 시작", "요청 수신")
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
        data = request.get_json()
        log_debug("📥 웹훅 데이터", json.dumps(data))
        
        # === 🔥 중복 방지 체크 ===
        alert_id = data.get("id", "")
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        reason = data.get("reason", "")
        prevent_duplicate = data.get("prevent_duplicate", False)
        copy_number = data.get("copy", 0)
        attempt_number = data.get("attempt", 1)  # 🔥 추가: 시도 번호
        
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400
        
        # 중복 방지가 활성화된 경우 체크
        if prevent_duplicate and copy_number == 0:  # 원본 알림만 체크 (copy는 무시)
            if is_duplicate_alert(alert_id, symbol, side, action):
                return jsonify({"status": "duplicate_ignored", "message": "중복 알림 무시됨"})
        elif copy_number > 0:
            # copy 알림은 로그만 남기고 무시
            log_debug("📋 Copy 알림 무시", f"copy={copy_number}, ID={alert_id}")
            return jsonify({"status": "copy_ignored", "message": "Copy 알림 무시됨"})

        # 🔥 추가: 시도 번호 로깅
        if attempt_number > 1:
            log_debug("🔄 재시도 알림", f"attempt={attempt_number}, {symbol} {side} {action}")

        if action == "exit":
            update_position_state(symbol, timeout=1)
            current_side = position_state.get(symbol, {}).get("side")
            if reason == "reverse_signal":
                success = close_position(symbol)
            else:
                if side == "long" and current_side == "buy":
                    success = close_position(symbol)
                elif side == "short" and current_side == "sell":
                    success = close_position(symbol)
                else:
                    log_debug(f"❌ 청산 실패 ({symbol})", f"현재 포지션: {current_side}, 요청 side: {side}")
                    success = False
            log_debug(f"🔁 청산 결과 ({symbol})", f"성공: {success}")
            return jsonify({"status": "success" if success else "error"})
        
        if side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "Invalid side/action"}), 400
        if not update_position_state(symbol, timeout=1):
            return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
        current_side = position_state.get(symbol, {}).get("side")
        desired_side = "buy" if side == "long" else "sell"
        
        # === 🔥 포지션 상태 기반 중복 진입 체크 ===
        if current_side and current_side == desired_side:
            log_debug("🚫 동일 방향 포지션 존재", f"현재: {current_side}, 요청: {desired_side} - 진입 취소 (attempt: {attempt_number})")
            return jsonify({"status": "duplicate_position", "message": "동일 방향 포지션이 이미 존재함", "attempt": attempt_number})
        
        if current_side and current_side != desired_side:
            log_debug("🔄 역포지션 처리", f"현재: {current_side} → 목표: {desired_side}")
            if not close_position(symbol):
                log_debug("❌ 역포지션 청산 실패", "")
                return jsonify({"status": "error", "message": "역포지션 청산 실패"})
            time.sleep(3)
            if not update_position_state(symbol):
                log_debug("❌ 역포지션 후 상태 갱신 실패", "")
        qty = calculate_position_size(symbol)
        log_debug(f"🧮 수량 계산 완료 ({symbol})", f"{qty} 계약")
        if qty <= 0:
            log_debug("❌ 수량 오류", f"계산된 수량: {qty}")
            return jsonify({"status": "error", "message": "수량 계산 오류"})
        success = place_order(symbol, desired_side, qty)
        log_debug(f"📨 최종 결과 ({symbol})", f"주문 성공: {success}, attempt: {attempt_number}")
        return jsonify({
            "status": "success" if success else "error", 
            "qty": float(qty), 
            "duplicate_prevention": prevent_duplicate,
            "attempt": attempt_number
        })
    except Exception as e:
        log_debug(f"❌ 웹훅 전체 실패 ({symbol or 'unknown'})", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

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
                    positions[sym] = {k: float(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
        
        # 중복 방지 상태 정보 추가
        with duplicate_prevention_lock:
            duplicate_stats = {
                "alert_cache_size": len(alert_cache),
                "recent_signals_size": len(recent_signals),
                "recent_signals": {k: {"side": v["side"], "action": v["action"], "age": time.time() - v["time"]} 
                                 for k, v in recent_signals.items()}
            }
        
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "margin_balance": float(equity),
            "positions": positions,
            "actual_entry_prices": {k: float(v) for k, v in actual_entry_prices.items()},
            "duplicate_prevention": duplicate_stats
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

async def send_ping(ws):
    """웹소켓 핑 전송"""
    while True:
        try:
            await ws.ping()
        except Exception:
            break
        await asyncio.sleep(30)

async def price_listener():
    """실시간 가격 모니터링"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60
    log_debug("📡 웹소켓 시작", f"URI: {uri}, 심볼: {len(symbols)}개")
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
    """실시간 가격 데이터 처리 및 SL/TP 체크"""
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
            cfg = SYMBOL_CONFIG[contract]
            if side == "buy":
                sl = entry * (1 - cfg["sl_pct"])
                tp = entry * (1 + cfg["tp_pct"])
                if price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{price} <= SL:{sl} (진입가:{entry})")
                    close_position(contract)
                elif price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{price} >= TP:{tp} (진입가:{entry})")
                    close_position(contract)
            else:
                sl = entry * (1 + cfg["sl_pct"])
                tp = entry * (1 - cfg["tp_pct"])
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
            time.sleep(300)
        except Exception:
            time.sleep(300)

if __name__ == "__main__":
    log_initial_status()
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    threading.Thread(target=backup_position_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"포트 {port}에서 실행 (중복 진입 방지 활성화)")
    app.run(host="0.0.0.0", port=port, debug=False)
