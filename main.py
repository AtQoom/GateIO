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

# 🔥 심볼별 TP/SL 가중치 설정 (수정됨)
def get_tpsl_multipliers(symbol):
    """심볼별 TP/SL 가중치 반환"""
    if symbol == "BTC_USDT":
        return {"tp": 0.7, "sl": 0.7}    # BTC: 70%
    elif symbol == "ETH_USDT":
        return {"tp": 0.8, "sl": 0.8}    # ETH: 80%
    elif symbol == "SOL_USDT":
        return {"tp": 0.9, "sl": 0.9}    # SOL: 90%
    else:
        return {"tp": 1.0, "sl": 1.0}    # 기타: 100%

def parse_simple_alert(message):
    """간단한 파이프 구분 메시지 파싱"""
    try:
        if message.startswith("ENTRY:"):
            # ENTRY:long|BTCUSDT|Single_LONG|50000|1
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
    """🔥 강화된 심볼 정규화 - 다양한 형태를 표준 형태로 변환"""
    if not raw_symbol:
        log_debug("❌ 심볼 정규화", "입력 심볼이 비어있음")
        return None
    
    # 대문자로 변환하고 공백 제거
    symbol = str(raw_symbol).upper().strip()
    log_debug("🔍 심볼 정규화 시작", f"원본: '{raw_symbol}' -> 정리: '{symbol}'")
    
    # 직접 매핑이 있으면 사용
    if symbol in SYMBOL_MAPPING:
        result = SYMBOL_MAPPING[symbol]
        log_debug("✅ 직접 매핑 성공", f"'{symbol}' -> '{result}'")
        return result
    
    # .P 제거 시도
    if symbol.endswith('.P'):
        base_symbol = symbol[:-2]
        log_debug("🔍 .P 제거 시도", f"'{symbol}' -> '{base_symbol}'")
        if base_symbol in SYMBOL_MAPPING:
            result = SYMBOL_MAPPING[base_symbol]
            log_debug("✅ .P 제거 후 매핑 성공", f"'{base_symbol}' -> '{result}'")
            return result
    
    # PERP 제거 시도
    if symbol.endswith('PERP'):
        base_symbol = symbol[:-4]
        log_debug("🔍 PERP 제거 시도", f"'{symbol}' -> '{base_symbol}'")
        if base_symbol in SYMBOL_MAPPING:
            result = SYMBOL_MAPPING[base_symbol]
            log_debug("✅ PERP 제거 후 매핑 성공", f"'{base_symbol}' -> '{result}'")
            return result
    
    # : 이후 제거 시도
    if ':' in symbol:
        base_symbol = symbol.split(':')[0]
        log_debug("🔍 : 이후 제거 시도", f"'{symbol}' -> '{base_symbol}'")
        if base_symbol in SYMBOL_MAPPING:
            result = SYMBOL_MAPPING[base_symbol]
            log_debug("✅ : 제거 후 매핑 성공", f"'{base_symbol}' -> '{result}'")
            return result
    
    log_debug("❌ 심볼 매핑 실패", f"'{symbol}' 매핑을 찾을 수 없음")
    return None

SYMBOL_CONFIG = {
    # BTC: 최소 0.001 BTC, 1계약 = 0.0001 BTC이므로 최소 10계약
    "BTC_USDT": {
        "min_qty": Decimal("1"),         # 최소 주문 수량: 1계약
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("0.0001"), # 계약 크기: 0.0001 BTC
        "min_notional": Decimal("10")     # 최소 주문 금액: 10 USDT
    },
    # ETH: 최소 0.01 ETH, 1계약 = 0.01 ETH이므로 최소 1계약
    "ETH_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("0.01"), # 계약 크기: 0.01 ETH
        "min_notional": Decimal("10")
    },
    # ADA: 추정 최소 10 ADA, 1계약 = 10 ADA이므로 최소 1계약
    "ADA_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("10"),   # 계약 크기: 10 ADA
        "min_notional": Decimal("10")
    },
    # SUI: 추정 최소 1 SUI, 1계약 = 1 SUI이므로 최소 1계약
    "SUI_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("1"),    # 계약 크기: 1 SUI
        "min_notional": Decimal("10")
    },
    # LINK: 추정 최소 1 LINK, 1계약 = 1 LINK이므로 최소 1계약
    "LINK_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("1"),    # 계약 크기: 1 LINK
        "min_notional": Decimal("10")
    },
    # SOL: 추정 최소 0.1 SOL, 1계약 = 0.1 SOL이므로 최소 1계약
    "SOL_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("1"),    # 계약 크기: 0.1 SOL (수정됨)
        "min_notional": Decimal("10")
    },
    # PEPE: 최소 10,000 PEPE, 1계약 = 10,000 PEPE이므로 최소 1계약
    "PEPE_USDT": {
        "min_qty": Decimal("1"),          # 최소 주문 수량: 1계약
        "qty_step": Decimal("1"),         # 주문 수량 단위: 1계약
        "contract_size": Decimal("10000000"), # 계약 크기: 10,000,000 PEPE
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

# === 🔥 중복 방지 시스템 (단일 진입으로 단순화) ===
alert_cache = {}  # {alert_id: {"timestamp": time, "processed": bool}}
recent_signals = {}  # {symbol: {"side": side, "time": timestamp, "action": action, "strategy": strategy}}
duplicate_prevention_lock = threading.RLock()

def is_duplicate_alert(alert_data):
    """단일 진입 중복 방지"""
    global alert_cache, recent_signals
    
    with duplicate_prevention_lock:
        current_time = time.time()
        alert_id = alert_data.get("id", "")
        symbol = alert_data.get("symbol", "")
        side = alert_data.get("side", "")
        action = alert_data.get("action", "")
        strategy_name = alert_data.get("strategy", "")
        
        log_debug("🔍 중복 체크 시작", f"ID: {alert_id}, Symbol: {symbol}, Side: {side}, Action: {action}")
        
        # 1. 같은 alert_id가 이미 처리되었는지 확인
        if alert_id in alert_cache:
            cache_entry = alert_cache[alert_id]
            time_diff = current_time - cache_entry["timestamp"]
            
            if cache_entry["processed"] and time_diff < 300:  # 5분 이내 같은 ID는 중복
                log_debug("🚫 중복 ID 차단", f"ID: {alert_id}, {time_diff:.1f}초 전 처리됨")
                return True
        
        # 2. 단일 진입 - 같은 방향 신호 중복 확인
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            if symbol_key in recent_signals:
                recent = recent_signals[symbol_key]
                time_diff = current_time - recent["time"]
                
                # 60초 이내 동일 신호는 중복으로 간주
                if (recent["strategy"] == strategy_name and 
                    recent["action"] == "entry" and 
                    time_diff < 60):
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
            log_debug("✅ 알림 처리 완료", f"ID: {alert_id}")

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
    순자산(Account Equity) 기반으로 포지션 크기 계산 (단일 진입)
    파인스크립트의 default_qty_value=100 (순자산 100%) 반영
    """
    cfg = SYMBOL_CONFIG[symbol]
    
    # 1. 순자산 조회 (전체 보유 자산)
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    
    if price <= 0 or equity <= 0:
        log_debug(f"❌ 수량 계산 불가 ({symbol})", f"가격: {price}, 순자산: {equity}")
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
    """주문 실행 (단일 진입)"""
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
            position_entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            position_state[symbol] = {
                "price": position_entry_price,
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
    """포지션 청산 - 파인스크립트가 청산 신호를 보낼 때만 실행"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 청산 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "파인스크립트 신호에 의한 청산")
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
        log_debug("🚀 서버 시작", "파인스크립트 단일 진입 연동 모드 - 초기 상태 확인 중...")
        equity = get_total_collateral(force=True)
        log_debug("💰 총 자산(초기)", f"{equity} USDT")
        
        for symbol in SYMBOL_CONFIG:
            if not update_position_state(symbol, timeout=3):
                log_debug("❌ 포지션 조회 실패", f"초기화 중 {symbol} 상태 확인 불가")
                continue
            pos = position_state.get(symbol, {})
            if pos.get("side"):
                # 심볼별 TP/SL 가중치 표시
                multipliers = get_tpsl_multipliers(symbol)
                tp_pct = 0.004 * multipliers["tp"]  # 0.4% 기본
                sl_pct = 0.0015 * multipliers["sl"]  # 0.15% 기본
                log_debug(
                    f"📊 초기 포지션 ({symbol})",
                    f"방향: {pos['side']}, 수량: {pos['size']}, 진입가: {pos['price']}, "
                    f"평가금액: {pos['value']} USDT, TP: {tp_pct*100:.2f}%, SL: {sl_pct*100:.2f}%"
                )
            else:
                multipliers = get_tpsl_multipliers(symbol)
                tp_pct = 0.004 * multipliers["tp"]
                sl_pct = 0.0015 * multipliers["sl"]
                log_debug(f"📊 초기 포지션 ({symbol})", f"포지션 없음, TP: {tp_pct*100:.2f}%, SL: {sl_pct*100:.2f}%")
    except Exception as e:
        log_debug("❌ 초기 상태 로깅 실패", str(e), exc_info=True)

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    """헬스체크 엔드포인트"""
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    """🔥 단일 진입 웹훅 처리"""
    symbol = None
    alert_id = None
    raw_data = ""
    
    try:
        log_debug("🔄 웹훅 시작", "파인스크립트 단일 진입 신호 수신")
        
        # === 🔥 Raw 데이터 먼저 확인 ===
        try:
            raw_data = request.get_data(as_text=True)
            log_debug("📄 Raw 데이터", f"길이: {len(raw_data)}")
            if len(raw_data) > 0:
                log_debug("📄 Raw 내용", f"내용: {raw_data[:500]}...")
        except Exception as e:
            log_debug("❌ Raw 데이터 읽기 실패", str(e))
            raw_data = ""
        
        # 빈 데이터 체크
        if not raw_data or raw_data.strip() == "":
            log_debug("❌ 빈 데이터", "Raw 데이터가 비어있음")
            return jsonify({"error": "Empty data"}), 400
        
        # === 🔥 메시지 파싱 (간단한 형태와 JSON 모두 지원) ===
        data = None
        
        # 1차 시도: 간단한 파이프 구분 메시지 파싱
        if raw_data.startswith("ENTRY:") or raw_data.startswith("EXIT:"):
            data = parse_simple_alert(raw_data.strip())
            if data:
                log_debug("✅ 간단 메시지 파싱 성공", f"Action: {data.get('action')}, Symbol: {data.get('symbol')}")
            else:
                log_debug("❌ 간단 메시지 파싱 실패", f"Raw: {raw_data[:100]}")
                return jsonify({"error": "Simple message parsing failed"}), 400
        else:
            # 2차 시도: JSON 파싱 (기존 방식)
            try:
                data = request.get_json(force=True)
                if data is None:
                    data = json.loads(raw_data)
                log_debug("✅ JSON 파싱 성공", "JSON 데이터 추출 완료")
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                log_debug("❌ JSON 파싱 실패", f"오류: {str(e)}, Raw: {raw_data[:100]}")
                return jsonify({
                    "error": "JSON parsing failed but data exists", 
                    "raw_data": raw_data[:200],
                    "suggestion": "데이터가 JSON 형식이 아닙니다. 간단한 메시지 형식(ENTRY:/EXIT:)을 사용하세요."
                }), 400
            except Exception as e:
                log_debug("❌ 예상치 못한 파싱 오류", f"오류: {str(e)}, Raw 데이터: {raw_data}")
                return jsonify({"error": "Parsing error", "raw_data": raw_data[:200]}), 500
                
        if not data:
            log_debug("❌ 빈 파싱 결과", "파싱된 데이터가 비어있음")
            return jsonify({"error": "Empty parsed data"}), 400
            
        log_debug("📥 웹훅 데이터", json.dumps(data, indent=2, ensure_ascii=False, default=str))
        
        # === 🔥 필드별 상세 검사 ===
        log_debug("🔍 데이터 검사", f"키들: {list(data.keys())}")
        
        alert_id = data.get("id", "")
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower() if data.get("side") else ""
        action = data.get("action", "").lower() if data.get("action") else ""
        strategy_name = data.get("strategy", "")
        price = data.get("price", 0)
        position_count = data.get("position_count", 1)
        
        log_debug("🔍 필드 추출", f"ID: '{alert_id}', Symbol: '{raw_symbol}', Side: '{side}', Action: '{action}'")
        log_debug("🔍 추가 필드", f"Strategy: '{strategy_name}', Price: {price}, Position: {position_count}")
        
        # 필수 필드 검증
        missing_fields = []
        if not raw_symbol:
            missing_fields.append("symbol")
        if not side:
            missing_fields.append("side")
        if not action:
            missing_fields.append("action")
            
        if missing_fields:
            log_debug("❌ 필수 필드 누락", f"누락된 필드: {missing_fields}")
            return jsonify({"error": f"Missing required fields: {missing_fields}"}), 400
        
        log_debug("✅ 필수 필드 검증 통과", "모든 필드가 존재함")
        
        # 🔥 강화된 심볼 변환
        log_debug("🔍 심볼 정규화 시작", f"원본: '{raw_symbol}'")
        symbol = normalize_symbol(raw_symbol)
        
        if not symbol:
            log_debug("❌ 심볼 정규화 실패", f"'{raw_symbol}' -> None")
            return jsonify({"error": f"Symbol normalization failed: {raw_symbol}"}), 400
            
        if symbol not in SYMBOL_CONFIG:
            log_debug("❌ 심볼 설정 없음", f"'{symbol}' not in {list(SYMBOL_CONFIG.keys())}")
            return jsonify({"error": f"Symbol not supported: {symbol}"}), 400
        
        log_debug("✅ 심볼 매핑 성공", f"'{raw_symbol}' -> '{symbol}'")
        
        # === 🔥 중복 방지 체크 ===
        if is_duplicate_alert(data):
            log_debug("🚫 중복 알림 차단", f"Symbol: {symbol}, Side: {side}, Action: {action}")
            return jsonify({"status": "duplicate_ignored", "message": "중복 알림 무시됨"})
        
        log_debug("✅ 중복 체크 통과", "신규 알림으로 확인됨")
        
        # === 🔥 진입/청산 신호 처리 ===
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
                "strategy": strategy_name
            })
        
        # === 🔥 단일 진입 신호 처리 ===
        if action == "entry" and side in ["long", "short"]:
            log_debug(f"🎯 단일 진입 신호 처리 시작 ({symbol})", 
                     f"{side} 방향, 전략: {strategy_name}")
            
            # 심볼별 TP/SL 정보 표시
            multipliers = get_tpsl_multipliers(symbol)
            tp_pct = 0.004 * multipliers["tp"]  # 0.4% 기본
            sl_pct = 0.0015 * multipliers["sl"]  # 0.15% 기본
            log_debug(f"📊 TP/SL 설정 ({symbol})", 
                     f"TP: {tp_pct*100:.2f}%, SL: {sl_pct*100:.2f}% (가중치: {multipliers['tp']*100:.0f}%)")
            
            if not update_position_state(symbol, timeout=1):
                log_debug(f"❌ 포지션 상태 조회 실패 ({symbol})", "")
                return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
            
            current_side = position_state.get(symbol, {}).get("side")
            desired_side = "buy" if side == "long" else "sell"
            
            log_debug(f"📊 현재 상태 ({symbol})", f"현재: {current_side}, 요청: {desired_side}")
            
            # 기존 포지션이 있으면 청산 후 진입
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
            
            # 수량 계산
            log_debug(f"🧮 수량 계산 시작 ({symbol})", f"전략: {strategy_name}")
            qty = calculate_position_size(symbol, strategy_name)
            log_debug(f"🧮 수량 계산 완료 ({symbol})", f"{qty} 계약 (전략: {strategy_name})")
            
            if qty <= 0:
                log_debug("❌ 수량 오류", f"계산된 수량: {qty}")
                return jsonify({"status": "error", "message": "수량 계산 오류"})
            
            # 주문 실행
            log_debug(f"📤 주문 실행 시작 ({symbol})", f"{desired_side} {qty} 계약")
            success = place_order(symbol, desired_side, qty)
            
            if success and alert_id:
                mark_alert_processed(alert_id)
            
            log_debug(f"📨 최종 결과 ({symbol})", 
                     f"주문 성공: {success}, 전략: {strategy_name}")
            
            return jsonify({
                "status": "success" if success else "error", 
                "action": "entry",
                "symbol": symbol,
                "side": side,
                "qty": float(qty),
                "strategy": strategy_name,
                "entry_mode": "single",
                "tp_pct": tp_pct * 100,
                "sl_pct": sl_pct * 100
            })
        
        # 잘못된 액션
        log_debug("❌ 잘못된 액션", f"Action: {action}, 지원되는 액션: entry, exit")
        return jsonify({"error": f"Invalid action: {action}"}), 400
        
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 웹훅 전체 실패 ({symbol or 'unknown'})", error_msg, exc_info=True)
        
        # 오류 발생 시에도 중복 방지를 위해 ID 처리
        if alert_id:
            mark_alert_processed(alert_id)
            
        return jsonify({
            "status": "error", 
            "message": error_msg,
            "raw_data": raw_data[:200] if raw_data else "unavailable"
        }), 500

@app.route("/status", methods=["GET"])
def status():
    """서버 상태 조회 (심볼별 TP/SL 정보 포함)"""
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        
        for sym in SYMBOL_CONFIG:
            if update_position_state(sym, timeout=1):
                pos = position_state.get(sym, {})
                if pos.get("side"):
                    position_info = {k: float(v) if isinstance(v, Decimal) else v 
                                   for k, v in pos.items()}
                    
                    # 심볼별 TP/SL 정보 추가
                    multipliers = get_tpsl_multipliers(sym)
                    tp_pct = 0.004 * multipliers["tp"]
                    sl_pct = 0.0015 * multipliers["sl"]
                    position_info["tp_pct"] = tp_pct * 100
                    position_info["sl_pct"] = sl_pct * 100
                    position_info["multiplier"] = multipliers
                    
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
                    "age_seconds": round(time.time() - v["time"], 1)
                } for k, v in recent_signals.items()}
            }
        
        # 심볼별 TP/SL 설정 정보
        tpsl_info = {}
        for symbol in SYMBOL_CONFIG:
            multipliers = get_tpsl_multipliers(symbol)
            tp_pct = 0.004 * multipliers["tp"]
            sl_pct = 0.0015 * multipliers["sl"]
            tpsl_info[symbol] = {
                "tp_pct": tp_pct * 100,
                "sl_pct": sl_pct * 100,
                "multiplier": multipliers
            }
        
        return jsonify({
            "status": "running",
            "mode": "pinescript_single_entry_weighted_tpsl",
            "timestamp": datetime.now().isoformat(),
            "margin_balance": float(equity),
            "positions": positions,
            "duplicate_prevention": duplicate_stats,
            "symbol_mappings": SYMBOL_MAPPING,
            "tpsl_settings": tpsl_info,
            "pinescript_features": {
                "perfect_alerts": True,
                "future_prediction": True,
                "backup_signals": True,
                "pyramiding": 1,
                "entry_timeframe": "15S",
                "exit_timeframe": "1M",
                "tp_sl_managed_by_pinescript": True,
                "enhanced_logging": True,
                "weighted_tpsl": True
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
    
    tpsl_info = {}
    if normalized:
        multipliers = get_tpsl_multipliers(normalized)
        tp_pct = 0.004 * multipliers["tp"]
        sl_pct = 0.0015 * multipliers["sl"]
        tpsl_info = {
            "tp_pct": tp_pct * 100,
            "sl_pct": sl_pct * 100,
            "multiplier": multipliers
        }
    
    return jsonify({
        "input": symbol,
        "normalized": normalized,
        "valid": is_valid,
        "config_exists": normalized in SYMBOL_CONFIG if normalized else False,
        "tpsl_settings": tpsl_info,
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
    
    # 백업 포지션 상태 갱신
    threading.Thread(target=backup_position_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"포트 {port}에서 실행 (수정된 가중치 TP/SL)")
    log_debug("✅ TP/SL 가중치", "BTC 70%, ETH 80%, SOL 90%, 기타 100%")
    log_debug("✅ 기본 TP/SL", "TP 0.4%, SL 0.15%")
    log_debug("✅ 실제 TP/SL", "BTC 0.28%/0.105%, ETH 0.32%/0.12%, SOL 0.36%/0.135%")
    log_debug("✅ 진입신호", "파인스크립트 15초봉 극값 알림")
    log_debug("✅ 청산신호", "파인스크립트 1분봉 시그널 알림")
    log_debug("✅ 진입 모드", "단일 진입 (Pyramiding=1)")
    log_debug("✅ 중복 방지", "완벽한 알림 시스템 연동")
    log_debug("✅ 심볼 매핑", "모든 형태 지원 (.P, PERP 등)")
    log_debug("✅ 실거래 전용", "백테스트 불가 (알림 기반)")
    
    app.run(host="0.0.0.0", port=port, debug=False)
