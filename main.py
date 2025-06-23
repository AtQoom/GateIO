import os
import json
import time
import asyncio
import threading
import websockets
import logging
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi
from collections import OrderedDict, defaultdict, deque

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

# ----------- 고급 중복 방지 시스템 -----------
class AdvancedDuplicateFilter:
    def __init__(self):
        self.alert_history = defaultdict(lambda: deque(maxlen=100))
        self.processing_alerts = set()
        self.lock = threading.RLock()
        self.cleanup_interval = 300  # 5분마다 정리
        self.last_cleanup = time.time()
        self.duplicate_stats = defaultdict(int)
    
    def is_duplicate_or_processing(self, alert_data):
        with self.lock:
            # 정기 정리
            if time.time() - self.last_cleanup > self.cleanup_interval:
                self._cleanup_old_alerts()
            
            alert_id = alert_data.get("id", "")
            symbol = alert_data.get("symbol", "")
            side = alert_data.get("side", "")
            action = alert_data.get("action", "")
            
            # 1. ID 기반 중복 체크
            if alert_id in self.processing_alerts:
                log_debug("🚫 ID 중복 차단", f"ID {alert_id} 처리 중")
                self.duplicate_stats["id_duplicate"] += 1
                return True
            
            # 2. 내용 기반 중복 체크 (ID가 다르더라도)
            content_hash = self._generate_content_hash(alert_data)
            symbol_history = self.alert_history[symbol]
            
            current_time = time.time()
            for hist_time, hist_hash, hist_side, hist_action in symbol_history:
                if (current_time - hist_time < 30 and  # 30초 내
                    hist_hash == content_hash and
                    hist_side == side and 
                    hist_action == action):
                    log_debug("🚫 내용 중복 차단", f"{symbol} {side} {action} (Hash: {content_hash[:8]})")
                    self.duplicate_stats["content_duplicate"] += 1
                    return True
            
            # 3. 처리 중 목록에 추가
            self.processing_alerts.add(alert_id)
            
            # 4. 히스토리에 추가
            symbol_history.append((current_time, content_hash, side, action))
            
            return False
    
    def mark_processed(self, alert_id):
        with self.lock:
            self.processing_alerts.discard(alert_id)
    
    def _generate_content_hash(self, alert_data):
        """내용 기반 해시 생성"""
        content = f"{alert_data.get('symbol')}_{alert_data.get('side')}_{alert_data.get('action')}_{alert_data.get('price', 0):.2f}_{alert_data.get('signal_type', '')}"
        return hashlib.md5(content.encode()).hexdigest()[:8]
    
    def _cleanup_old_alerts(self):
        """오래된 알림 정리"""
        current_time = time.time()
        # 10분 이상 된 처리 중 알림 제거
        old_alerts = {aid for aid in self.processing_alerts 
                     if '_' in aid and len(aid.split('_')) > 1}
        
        for aid in list(old_alerts):
            try:
                # ID에서 타임스탬프 추출 (EN_timestamp_bar_signal_price 형식)
                parts = aid.split('_')
                if len(parts) >= 2:
                    timestamp = int(parts[1])
                    if current_time - timestamp > 600:  # 10분
                        self.processing_alerts.discard(aid)
            except (ValueError, IndexError):
                # 파싱 불가능한 ID는 제거
                self.processing_alerts.discard(aid)
        
        self.last_cleanup = current_time
        log_debug("🧹 중복 필터 정리", f"처리 중: {len(self.processing_alerts)}, 통계: {dict(self.duplicate_stats)}")
    
    def get_stats(self):
        with self.lock:
            return {
                "processing_count": len(self.processing_alerts),
                "history_symbols": len(self.alert_history),
                "duplicate_stats": dict(self.duplicate_stats)
            }

# ----------- 포지션 동기화 관리자 -----------
class PositionSyncManager:
    def __init__(self):
        self.sync_lock = threading.RLock()
        self.last_sync_time = {}
        self.sync_failures = defaultdict(int)
        self.max_failures = 5
    
    def sync_position_with_retry(self, symbol, max_retries=3):
        """재시도 로직이 있는 포지션 동기화"""
        with self.sync_lock:
            for attempt in range(max_retries):
                try:
                    if update_position_state(symbol, timeout=10):
                        self.last_sync_time[symbol] = time.time()
                        self.sync_failures[symbol] = 0
                        return True
                    else:
                        log_debug(f"🔄 포지션 동기화 재시도 ({symbol})", f"시도 {attempt + 1}/{max_retries}")
                        if attempt < max_retries - 1:
                            time.sleep(2 ** attempt)  # 지수 백오프
                except Exception as e:
                    log_debug(f"❌ 포지션 동기화 오류 ({symbol})", f"시도 {attempt + 1}: {str(e)}")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
            
            self.sync_failures[symbol] += 1
            if self.sync_failures[symbol] >= self.max_failures:
                log_debug(f"🚨 포지션 동기화 연속 실패 ({symbol})", f"{self.sync_failures[symbol]}회")
                # 여기에 긴급 알림 로직 추가 가능
            
            return False
    
    def is_sync_fresh(self, symbol, max_age=30):
        """포지션 정보가 최신인지 확인"""
        last_sync = self.last_sync_time.get(symbol, 0)
        return time.time() - last_sync < max_age
    
    def get_sync_status(self):
        """동기화 상태 반환"""
        current_time = time.time()
        status = {}
        for symbol in SYMBOL_CONFIG:
            last_sync = self.last_sync_time.get(symbol, 0)
            failures = self.sync_failures.get(symbol, 0)
            status[symbol] = {
                "last_sync_ago": current_time - last_sync,
                "failures": failures,
                "is_fresh": self.is_sync_fresh(symbol)
            }
        return status

# 전역 인스턴스
duplicate_filter = AdvancedDuplicateFilter()
sync_manager = PositionSyncManager()

# ----------- 수정된 알림 검증 시스템 -----------
def validate_alert_data(data):
    """알림 데이터 무결성 검증 (heartbeat/sync 예외 처리)"""
    try:
        # heartbeat와 sync 타입은 간단한 검증만
        alert_type = data.get("type", "")
        if alert_type in ["heartbeat", "sync"]:
            # 기본 필드만 체크
            required_fields = ["type", "symbol", "timestamp"]
            for field in required_fields:
                if field not in data:
                    return False, f"Missing required field for {alert_type}: {field}"
            
            # 타임스탬프 검증만
            try:
                alert_time = int(data.get("timestamp", 0)) / 1000
                current_time = time.time()
                time_diff = abs(current_time - alert_time)
                if time_diff > 900:  # heartbeat/sync는 15분 허용
                    return False, f"Alert time difference too large: {time_diff:.1f}s"
            except (ValueError, TypeError):
                return False, "Invalid timestamp"
            
            return True, "Valid heartbeat/sync"
        
        # 거래 신호 (entry/exit)는 기존 검증 로직 적용
        # 1. 필수 필드 체크
        required_fields = ["id", "symbol", "side", "action", "price", "timestamp"]
        for field in required_fields:
            if field not in data:
                return False, f"Missing required field: {field}"
        
        # 2. 기본 데이터 타입 검증
        try:
            float(data.get("price", 0))
            int(data.get("timestamp", 0))
        except (ValueError, TypeError):
            return False, "Invalid price or timestamp format"
        
        # 3. 체크섬 검증 (있는 경우)
        if "checksum" in data and data.get("checksum"):
            expected_checksum = calculate_server_checksum(
                data.get("symbol"), data.get("side"), 
                data.get("action"), data.get("price"), 
                data.get("timestamp")
            )
            if str(data.get("checksum")) != str(expected_checksum):
                log_debug("❌ 체크섬 불일치", f"Expected: {expected_checksum}, Got: {data.get('checksum')}")
                return False, "Checksum mismatch"
        
        # 4. 타임스탬프 검증 (5분 이내)
        try:
            alert_time = int(data.get("timestamp", 0)) / 1000
            current_time = time.time()
            time_diff = abs(current_time - alert_time)
            if time_diff > 300:  # 5분
                log_debug("❌ 타임스탬프 오류", f"시간 차이: {time_diff:.1f}초")
                return False, f"Alert time difference too large: {time_diff:.1f}s"
        except (ValueError, TypeError):
            return False, "Invalid timestamp"
        
        # 5. 심볼 검증
        symbol = BINANCE_TO_GATE_SYMBOL.get(data.get("symbol", "").upper().replace(".P", ""))
        if not symbol or symbol not in SYMBOL_CONFIG:
            return False, f"Invalid or unsupported symbol: {data.get('symbol')}"
        
        # 6. 가격 합리성 체크
        try:
            price = float(data.get("price", 0))
            if price <= 0:
                return False, "Invalid price: must be positive"
            
            current_price = float(get_price(symbol))
            if current_price > 0:
                price_diff = abs(price - current_price) / current_price
                if price_diff > 0.05:  # 5% 이상 차이
                    log_debug("⚠️ 가격 편차 감지", f"Alert: {price}, Current: {current_price}, Diff: {price_diff*100:.2f}%")
        except (ValueError, TypeError, ZeroDivisionError):
            log_debug("⚠️ 현재가 조회 실패", f"Symbol: {symbol}")
        
        # 7. side/action 검증
        valid_sides = ["long", "short"]
        valid_actions = ["entry", "exit"]
        if data.get("side") not in valid_sides:
            return False, f"Invalid side: {data.get('side')}"
        if data.get("action") not in valid_actions:
            return False, f"Invalid action: {data.get('action')}"
        
        return True, "Valid"
        
    except Exception as e:
        log_debug("❌ 알림 검증 중 오류", str(e))
        return False, f"Validation error: {str(e)}"

def calculate_server_checksum(symbol, side, action, price, timestamp):
    """서버 측 체크섬 계산 (Pine Script와 동일한 로직)"""
    try:
        result = (len(symbol) + len(side) + len(action) + 
                 round(float(price) * 1000) + round(int(timestamp) / 1000))
        return str(result)
    except (ValueError, TypeError):
        return "0"

# ----------- 기존 함수들 (개선됨) -----------
def get_total_collateral(force=False):
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
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    if price <= 0 or equity <= 0:
        return Decimal("0")
    try:
        acc = api.list_futures_accounts(SETTLE)
        available = Decimal(str(getattr(acc, 'available', '0')))
        raw_qty = available / (price * cfg["contract_size"])
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        order_value = final_qty * price * cfg["contract_size"]
        if order_value < cfg["min_notional"]:
            log_debug(f"⛔ 최소 주문 금액 미달 ({symbol})", f"{order_value} < {cfg['min_notional']} USDT")
            return Decimal("0")
        log_debug(f"📊 수량 계산 ({symbol})", f"가용자산: {available}, 가격: {price}, 계약크기: {cfg['contract_size']}, 수량(계약): {final_qty}")
        return final_qty
    except Exception as e:
        log_debug(f"❌ 수량 계산 오류 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def place_order(symbol, side, qty, reduce_only=False, retry=3):
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
        log_debug(f"📤 주문 시도 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약, 주문금액: {order_value:.2f} USDT")
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        time.sleep(2)
        sync_manager.sync_position_with_retry(symbol)
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

def close_position(symbol):
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
        sync_manager.sync_position_with_retry(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
        return False
    finally:
        position_lock.release()

# ----------- 신호 처리 함수 -----------
def process_trading_signal(data):
    """실제 거래 신호 처리 로직"""
    raw = data.get("symbol", "").upper().replace(".P", "")
    symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
    if not symbol or symbol not in SYMBOL_CONFIG:
        return jsonify({"error": "Invalid symbol", "symbol": raw}), 400
    
    side = data.get("side", "").lower()
    action = data.get("action", "").lower()
    reason = data.get("reason", "")
    signal_type = data.get("signal_type", "unknown")
    alert_id = data.get("id", "unknown")

    log_debug(f"🎯 신호 처리 시작 ({symbol})", f"Side: {side}, Action: {action}, Type: {signal_type}, ID: {alert_id}")

    # 청산 처리
    if action == "exit":
        if not sync_manager.sync_position_with_retry(symbol, max_retries=2):
            log_debug(f"❌ 포지션 동기화 실패 ({symbol})", "청산 처리 중단")
            return jsonify({"status": "error", "message": "포지션 동기화 실패", "alert_id": alert_id}), 500
        
        current_side = position_state.get(symbol, {}).get("side")
    desired_side = "buy" if side == "long" else "sell"
    
    # 피라미딩 제한 체크 (계약 수만 체크, 자산 비율 제외)
    current_size = position_state.get(symbol, {}).get("size", Decimal("0"))
    if current_side == desired_side and current_size >= 2:
        log_debug("⛔ 피라미딩 제한", f"{symbol} {desired_side} 이미 {current_size}계약 진입")
        return jsonify({
            "status": "pyramiding_limit",
            "current_size": float(current_size),
            "alert_id": alert_id
        })

    # 역포지션 처리
    if current_side and current_side != desired_side:
        log_debug("🔄 역포지션 처리", f"현재: {current_side} → 목표: {desired_side}")
        if not close_position(symbol):
            log_debug("❌ 역포지션 청산 실패", "")
            return jsonify({"status": "error", "message": "역포지션 청산 실패", "alert_id": alert_id})
        time.sleep(3)
        if not sync_manager.sync_position_with_retry(symbol):
            log_debug("❌ 역포지션 후 상태 갱신 실패", "")

    # 수량 계산
    qty = calculate_position_size(symbol)
    log_debug(f"🧮 수량 계산 완료 ({symbol})", f"{qty} 계약")
    if qty <= 0:
        log_debug("❌ 수량 오류", f"계산된 수량: {qty}")
        return jsonify({"status": "error", "message": "수량 계산 오류", "alert_id": alert_id})

    # 주문 실행
    success = place_order(symbol, desired_side, qty)
    
    result = {
        "status": "success" if success else "error",
        "action": "entry",
        "symbol": symbol,
        "side": side,
        "qty": float(qty),
        "signal_type": signal_type,
        "alert_id": alert_id
    }
    
    log_debug(f"📨 신호 처리 완료 ({symbol})", f"성공: {success}, 결과: {result}")
    return jsonify(result)

# ----------- 라우트 핸들러들 -----------
@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/", methods=["POST"])
def enhanced_webhook():
    alert_id = None
    symbol = None
    
    try:
        log_debug("🔄 웹훅 시작", f"Request from {request.remote_addr}")
        
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
        
        data = request.get_json()
        alert_type = data.get("type", "unknown")
        alert_id = data.get("id", f"auto_{alert_type}_{int(time.time())}")
        
        log_debug("📥 웹훅 데이터", f"Type: {alert_type}, ID: {alert_id}")

        # 1. 알림 데이터 검증 (heartbeat/sync 구분)
        is_valid, validation_msg = validate_alert_data(data)
        if not is_valid:
            log_debug("❌ 알림 검증 실패", f"ID: {alert_id}, Reason: {validation_msg}")
            return jsonify({"error": validation_msg, "alert_id": alert_id}), 400
        
        # 2. heartbeat/sync 처리 (거래 없이 상태만 반환)
        if alert_type in ["heartbeat", "sync"]:
            log_debug("💓 상태 알림 수신", f"Type: {alert_type}, Symbol: {data.get('symbol')}")
            return jsonify({
                "status": "received", 
                "type": alert_type,
                "symbol": data.get("symbol"),
                "timestamp": data.get("timestamp")
            })
        
        # 3. 거래 신호만 중복 체크 적용
        if duplicate_filter.is_duplicate_or_processing(data):
            return jsonify({"status": "duplicate", "alert_id": alert_id})
        
        try:
            # 4. 거래 신호 처리
            result = process_trading_signal(data)
            
            # 5. 성공적으로 처리됨을 표시
            duplicate_filter.mark_processed(alert_id)
            
            return result
            
        except Exception as e:
            # 처리 실패시 중복 필터에서 제거
            duplicate_filter.mark_processed(alert_id)
            raise e
            
    except Exception as e:
        log_debug(f"❌ 웹훅 처리 실패 ({symbol or 'unknown'})", f"ID: {alert_id}, Error: {str(e)}")
        if alert_id and not alert_id.startswith("auto_"):
            duplicate_filter.mark_processed(alert_id)
        return jsonify({"status": "error", "message": str(e), "alert_id": alert_id}), 500

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        
        # 모든 심볼의 포지션 상태 수집
        for sym in SYMBOL_CONFIG:
            if sync_manager.sync_position_with_retry(sym, max_retries=1):
                pos = position_state.get(sym, {})
                if pos.get("side"):
                    positions[sym] = {k: float(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
        
        # 중복 필터 및 동기화 상태
        duplicate_stats = duplicate_filter.get_stats()
        sync_status = sync_manager.get_sync_status()
        
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "margin_balance": float(equity),
            "positions": positions,
            "actual_entry_prices": {k: float(v) for k, v in actual_entry_prices.items()},
            "duplicate_filter": duplicate_stats,
            "sync_status": sync_status,
            "system_health": {
                "api_responsive": True,  # API 호출이 성공했으므로
                "position_sync_ok": all(status["is_fresh"] for status in sync_status.values()),
                "duplicate_filter_ok": duplicate_stats["processing_count"] < 100
            }
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/health", methods=["GET"])
def health_check():
    """향상된 헬스체크 엔드포인트"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "v3.0",
        "checks": {}
    }
    
    try:
        # API 연결 체크
        try:
            balance = get_total_collateral(force=True)
            health_status["checks"]["api_connection"] = {
                "status": "ok",
                "balance": float(balance)
            }
        except Exception as e:
            health_status["checks"]["api_connection"] = {
                "status": "error",
                "error": str(e)
            }
            health_status["status"] = "degraded"
        
        # 포지션 동기화 체크
        sync_status = sync_manager.get_sync_status()
        stale_symbols = [sym for sym, status in sync_status.items() if not status["is_fresh"]]
        
        if stale_symbols:
            health_status["checks"]["position_sync"] = {
                "status": "stale",
                "stale_symbols": stale_symbols
            }
            health_status["status"] = "degraded"
        else:
            health_status["checks"]["position_sync"] = {"status": "ok"}
        
        # 중복 필터 상태 체크
        duplicate_stats = duplicate_filter.get_stats()
        if duplicate_stats["processing_count"] > 50:
            health_status["checks"]["duplicate_filter"] = {
                "status": "warning",
                "processing_count": duplicate_stats["processing_count"]
            }
            health_status["status"] = "degraded"
        else:
            health_status["checks"]["duplicate_filter"] = {"status": "ok"}
        
        # 환경 변수 체크
        if not API_KEY or not API_SECRET:
            health_status["checks"]["credentials"] = {"status": "missing"}
            health_status["status"] = "error"
        else:
            health_status["checks"]["credentials"] = {"status": "ok"}
        
    except Exception as e:
        health_status["status"] = "error"
        health_status["error"] = str(e)
    
    status_code = 200 if health_status["status"] == "healthy" else 503
    return jsonify(health_status), status_code

@app.route("/debug", methods=["GET"])
def debug_account():
    try:
        acc = api.list_futures_accounts(SETTLE)
        debug_info = {
            "raw_response": str(acc),
            "total": str(getattr(acc, 'total', '없음')),
            "available": str(getattr(acc, 'available', '없음')),
            "margin_balance": str(getattr(acc, 'margin_balance', '없음')),
            "equity": str(getattr(acc, 'equity', '없음')),
            "duplicate_filter_stats": duplicate_filter.get_stats(),
            "sync_manager_stats": sync_manager.get_sync_status()
        }
        return jsonify(debug_info)
    except Exception as e:
        return jsonify({"error": str(e)})

# ----------- 초기화 및 백그라운드 작업 -----------
def log_initial_status():
    try:
        log_debug("🚀 서버 시작", "초기 상태 확인 중...")
        equity = get_total_collateral(force=True)
        log_debug("💰 총 자산(초기)", f"{equity} USDT")
        
        for symbol in SYMBOL_CONFIG:
            if not sync_manager.sync_position_with_retry(symbol, max_retries=2):
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

async def send_ping(ws):
    while True:
        try:
            await ws.ping()
        except Exception:
            break
        await asyncio.sleep(30)

async def price_listener():
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
            if not sync_manager.is_sync_fresh(contract, max_age=60):
                # 60초마다 포지션 상태 갱신
                sync_manager.sync_position_with_retry(contract, max_retries=1)
            
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
    """백업 포지션 동기화 루프"""
    while True:
        try:
            for sym in SYMBOL_CONFIG:
                sync_manager.sync_position_with_retry(sym, max_retries=1)
            time.sleep(300)  # 5분마다
        except Exception:
            time.sleep(300)

# ----------- 메인 실행 -----------
if __name__ == "__main__":
    log_initial_status()
    
    # 백그라운드 스레드 시작
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    threading.Thread(target=backup_position_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"포트 {port}에서 실행 중 (개선된 버전 v3.0)")
    app.run(host="0.0.0.0", port=port, debug=False).get(symbol, {}).get("side")
        
        if reason == "reverse_signal":
            success = close_position(symbol)
            log_debug(f"🔁 역신호 청산 ({symbol})", f"성공: {success}")
        else:
            if side == "long" and current_side == "buy":
                success = close_position(symbol)
                log_debug(f"🔁 롱 청산 ({symbol})", f"성공: {success}")
            elif side == "short" and current_side == "sell":
                success = close_position(symbol)
                log_debug(f"🔁 숏 청산 ({symbol})", f"성공: {success}")
            else:
                log_debug(f"❌ 청산 불일치 ({symbol})", f"현재: {current_side}, 요청: {side}")
                success = False
        
        return jsonify({
            "status": "success" if success else "error",
            "action": "exit",
            "symbol": symbol,
            "alert_id": alert_id
        })

    # 진입 처리
    if side not in ["long", "short"] or action not in ["entry", "exit"]:
        return jsonify({"error": "Invalid side/action", "side": side, "action": action}), 400
    
    # 포지션 상태 동기화
    if not sync_manager.sync_position_with_retry(symbol, max_retries=2):
        return jsonify({"status": "error", "message": "포지션 동기화 실패", "alert_id": alert_id}), 500
    
    current_side = position_state
