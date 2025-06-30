import os
import json
import time
import math
import asyncio
import threading
import websockets
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi

# =================== 로그 설정 ===================
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

# =================== 서버 및 API 설정 ===================
app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# =================== 심볼 매핑 설정 ===================
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

# =================== 심볼 설정 ===================
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

# =================== 전역 변수 ===================
position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}

# 중복 방지 시스템
alert_cache = {}
recent_signals = {}
duplicate_prevention_lock = threading.RLock()

# 실시간 가격 저장
real_time_prices = {}
price_lock = threading.RLock()

# =================== 유틸리티 함수 ===================
def normalize_symbol(raw_symbol):
    """심볼 정규화 - 다양한 형태를 표준 형태로 변환"""
    if not raw_symbol:
        log_debug("❌ 심볼 정규화", "입력 심볼이 비어있음")
        return None
    
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

def parse_simple_alert(message):
    """간단한 파이프 구분 메시지 파싱"""
    try:
        if message.startswith("ENTRY:"):
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

# =================== 개선된 동적 TP/SL 시스템 ===================
def get_tpsl_multipliers(symbol):
    """심볼별 TP/SL 가중치 반환 (4단계 세분화)"""
    if symbol == "BTC_USDT":
        return {"tp": 0.7, "sl": 0.7}  # 가장 타이트 (안전성 최고)
    elif symbol == "ETH_USDT":
        return {"tp": 0.8, "sl": 0.8}  # 타이트 (높은 안전성)
    elif symbol == "SOL_USDT":
        return {"tp": 0.9, "sl": 0.9}  # 적당 (중간 안전성)
    else:
        return {"tp": 1.0, "sl": 1.0}  # 여유로움 (일반/고위험)

def calculate_dynamic_tpsl(symbol, entry_time):
    """실거래 전용 동적 TP/SL 계산 (강화된 기준값)"""
    if not entry_time:
        # 기본값 반환 (진입 시간이 없는 경우)
        multipliers = get_tpsl_multipliers(symbol)
        return 0.005 * multipliers["tp"], 0.002 * multipliers["sl"]
    
    elapsed_minutes = (time.time() - entry_time) / 60
    multipliers = get_tpsl_multipliers(symbol)
    
    # 강화된 기준값 적용
    base_tp = 0.005 * multipliers["tp"]  # 기본 0.5%에 심볼별 가중치 적용
    base_sl = 0.002 * multipliers["sl"]  # 기본 0.2%에 심볼별 가중치 적용
    
    # 10분 전에는 초기값 유지
    if elapsed_minutes < 10:
        return base_tp, base_sl
    
    # 10분 후부터 동적 감소 (실거래 최적화)
    # TP: 1분마다 0.01%씩 감소 → 최소 0.25%
    tp_reductions = int(elapsed_minutes - 10)
    tp_pct = max(base_tp - (tp_reductions * 0.0001), 0.0025)
    
    # SL: 3분마다 0.01%씩 감소 → 최소 0.15%
    sl_reductions = int((elapsed_minutes - 10) / 3)
    sl_pct = max(base_sl - (sl_reductions * 0.0001), 0.0015)
    
    return tp_pct, sl_pct

def get_real_time_price(symbol):
    """실시간 가격 조회 (웹소켓 우선, 실패시 API)"""
    with price_lock:
        if symbol in real_time_prices:
            price_data = real_time_prices[symbol]
            # 5초 이내 데이터면 사용
            if time.time() - price_data["timestamp"] < 5:
                return price_data["price"]
    
    # 웹소켓 데이터가 없으면 API로 조회
    return get_price(symbol)

def check_tpsl_conditions(symbol):
    """TP/SL 조건 체크 및 실행"""
    pos = position_state.get(symbol, {})
    if not pos.get("side") or not pos.get("entry_time"):
        return False
    
    current_price = get_real_time_price(symbol)
    if current_price <= 0:
        return False
    
    entry_price = pos["price"]
    side = pos["side"]
    tp_pct, sl_pct = calculate_dynamic_tpsl(symbol, pos["entry_time"])
    
    # TP/SL 가격 계산
    if side == "buy":  # 롱 포지션
        tp_price = entry_price * (1 + tp_pct)
        sl_price = entry_price * (1 - sl_pct)
        
        if current_price >= tp_price:
            log_debug(f"🎯 TP 달성 ({symbol})", 
                     f"현재가: {current_price}, TP: {tp_price} ({tp_pct*100:.3f}%)")
            return close_position(symbol)
        elif current_price <= sl_price:
            log_debug(f"🛑 SL 달성 ({symbol})", 
                     f"현재가: {current_price}, SL: {sl_price} ({sl_pct*100:.3f}%)")
            return close_position(symbol)
    else:  # 숏 포지션
        tp_price = entry_price * (1 - tp_pct)
        sl_price = entry_price * (1 + sl_pct)
        
        if current_price <= tp_price:
            log_debug(f"🎯 TP 달성 ({symbol})", 
                     f"현재가: {current_price}, TP: {tp_price} ({tp_pct*100:.3f}%)")
            return close_position(symbol)
        elif current_price >= sl_price:
            log_debug(f"🛑 SL 달성 ({symbol})", 
                     f"현재가: {current_price}, SL: {sl_price} ({sl_pct*100:.3f}%)")
            return close_position(symbol)
    
    return False

# =================== 웹소켓 실시간 가격 모니터링 ===================
async def price_listener():
    """Gate.io 웹소켓으로 실시간 가격 수신 및 TP/SL 처리"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    
    while True:
        try:
            log_debug("🔌 웹소켓 연결 시도", f"URI: {uri}")
            
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                # 구독 메시지 전송
                subscribe_msg = {
                    "method": "ticker.subscribe",
                    "params": symbols,
                    "id": 1
                }
                await websocket.send(json.dumps(subscribe_msg))
                log_debug("✅ 웹소켓 구독 완료", f"심볼: {symbols}")
                
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        
                        # 티커 데이터 처리
                        if data.get("method") == "ticker.update":
                            params = data.get("params", [])
                            if len(params) >= 2:
                                symbol = params[0]
                                ticker_data = params[1]
                                price = Decimal(str(ticker_data.get("last", "0")))
                                
                                if price > 0:
                                    # 실시간 가격 저장
                                    with price_lock:
                                        real_time_prices[symbol] = {
                                            "price": price,
                                            "timestamp": time.time()
                                        }
                                    
                                    # 해당 심볼의 TP/SL 체크
                                    if symbol in position_state:
                                        check_tpsl_conditions(symbol)
                        
                        # ping 응답
                        elif data.get("method") == "server.ping":
                            pong_msg = {"method": "server.pong", "params": [], "id": data.get("id")}
                            await websocket.send(json.dumps(pong_msg))
                            
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        log_debug("❌ 웹소켓 메시지 처리 오류", str(e))
                        continue
                        
        except Exception as e:
            log_debug("❌ 웹소켓 연결 실패", f"{str(e)}, 10초 후 재연결")
            await asyncio.sleep(10)

# =================== 중복 방지 시스템 ===================
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
        
        # 같은 alert_id가 이미 처리되었는지 확인
        if alert_id in alert_cache:
            cache_entry = alert_cache[alert_id]
            time_diff = current_time - cache_entry["timestamp"]
            
            if cache_entry["processed"] and time_diff < 300:
                log_debug("🚫 중복 ID 차단", f"ID: {alert_id}, {time_diff:.1f}초 전 처리됨")
                return True
        
        # 단일 진입 - 같은 방향 신호 중복 확인
        if action == "entry":
            symbol_key = f"{symbol}_{side}"
            if symbol_key in recent_signals:
                recent = recent_signals[symbol_key]
                time_diff = current_time - recent["time"]
                
                if (recent["strategy"] == strategy_name and 
                    recent["action"] == "entry" and 
                    time_diff < 60):
                    log_debug("🚫 중복 진입 차단", 
                             f"{symbol} {side} {strategy_name} 신호가 {time_diff:.1f}초 전에 이미 처리됨")
                    return True
        
        # 중복이 아니면 캐시에 저장
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
        cutoff_time = current_time - 900
        alert_cache = {k: v for k, v in alert_cache.items() if v["timestamp"] > cutoff_time}
        recent_signals = {k: v for k, v in recent_signals.items() if v["time"] > cutoff_time}
        
        return False

def mark_alert_processed(alert_id):
    """알림 처리 완료 표시"""
    with duplicate_prevention_lock:
        if alert_id in alert_cache:
            alert_cache[alert_id]["processed"] = True

# =================== 계정 및 거래 함수 ===================
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
        log_debug("❌ 총 자산 조회 실패", str(e))
        return Decimal("0")

def get_price(symbol):
    """현재 가격 조회 (API)"""
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker or len(ticker) == 0:
            return Decimal("0")
        price_str = str(ticker[0].last).upper().replace("E", "e")
        price = Decimal(price_str).normalize()
        return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
        return Decimal("0")

def calculate_position_size(symbol, strategy_type="standard"):
    """순자산 기반 포지션 크기 계산"""
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral(force=True)
    price = get_real_time_price(symbol)
    
    if price <= 0 or equity <= 0:
        return Decimal("0")
    
    try:
        # 전략별 포지션 크기 조정
        if "backup" in strategy_type.lower():
            position_ratio = Decimal("0.5")
        else:
            position_ratio = Decimal("1.0")
        
        adjusted_equity = equity * position_ratio
        raw_qty = adjusted_equity / (price * cfg["contract_size"])
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        
        order_value = final_qty * price * cfg["contract_size"]
        if order_value < cfg["min_notional"]:
            return Decimal("0")
        
        log_debug(f"📊 수량 계산 ({symbol})", 
                 f"순자산: {equity}, 비율: {position_ratio*100}%, 수량: {final_qty}")
        
        return final_qty
        
    except Exception as e:
        log_debug(f"❌ 수량 계산 오류 ({symbol})", str(e))
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
            
        price = get_real_time_price(symbol)
        order_value = qty_dec * price * cfg["contract_size"]
        
        if order_value < cfg["min_notional"]:
            return False
            
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        
        log_debug(f"📤 주문 시도 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        
        time.sleep(2)
        update_position_state(symbol)
        return True
        
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol})", str(e))
        
        if retry > 0 and any(keyword in str(e) for keyword in ["INVALID_PARAM", "POSITION_EMPTY", "INSUFFICIENT_AVAILABLE"]):
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
                    "mode": "cross", "entry_time": None
                }
                return True
            else:
                return False
                
        size = Decimal(str(pos.size))
        if size != 0:
            position_entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            
            # 기존 진입 시간 유지 또는 새로 설정
            existing_entry_time = position_state.get(symbol, {}).get("entry_time")
            entry_time = existing_entry_time if existing_entry_time else time.time()
            
            if not existing_entry_time:
                log_debug(f"🕐 진입 시간 설정 ({symbol})", 
                         f"새 포지션: {datetime.fromtimestamp(entry_time).strftime('%H:%M:%S')}")
            
            position_state[symbol] = {
                "price": position_entry_price,
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "value": value,
                "margin": value,
                "mode": "cross",
                "entry_time": entry_time
            }
        else:
            position_state[symbol] = {
                "price": None, "side": None, "size": Decimal("0"), 
                "value": Decimal("0"), "margin": Decimal("0"), 
                "mode": "cross", "entry_time": None
            }
        return True
        
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
        return False
    finally:
        position_lock.release()

def close_position(symbol):
    """포지션 청산"""
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        return False
        
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "포지션 청산 실행")
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
        log_debug("🚀 서버 초기화", "상태 확인 중...")
        equity = get_total_collateral(force=True)
        log_debug("💰 총 자산(초기)", f"{equity} USDT")
        
        for symbol in SYMBOL_CONFIG:
            if update_position_state(symbol, timeout=3):
                pos = position_state.get(symbol, {})
                if pos.get("side"):
                    log_debug(f"📊 초기 포지션 ({symbol})", 
                             f"방향: {pos['side']}, 수량: {pos['size']}, 진입가: {pos['price']}")
                else:
                    log_debug(f"📊 초기 포지션 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug("❌ 초기 상태 로깅 실패", str(e))

def backup_position_loop():
    """백업 포지션 상태 갱신"""
    while True:
        try:
            for sym in SYMBOL_CONFIG:
                update_position_state(sym, timeout=1)
            time.sleep(300)  # 5분마다 상태 갱신
        except Exception:
            time.sleep(300)

# =================== Flask 라우트 ===================
@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    """헬스체크 엔드포인트"""
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    """메인 웹훅 처리"""
    symbol = None
    alert_id = None
    raw_data = ""
    
    try:
        log_debug("🔄 웹훅 시작", "신호 수신")
        
        # Raw 데이터 확인
        try:
            raw_data = request.get_data(as_text=True)
            log_debug("📄 Raw 데이터", f"길이: {len(raw_data)}")
        except Exception as e:
            log_debug("❌ Raw 데이터 읽기 실패", str(e))
            raw_data = ""
        
        if not raw_data or raw_data.strip() == "":
            return jsonify({"error": "Empty data"}), 400
        
        # 메시지 파싱
        data = None
        
        # 간단한 파이프 구분 메시지 파싱
        if raw_data.startswith("ENTRY:") or raw_data.startswith("EXIT:"):
            data = parse_simple_alert(raw_data.strip())
            if not data:
                return jsonify({"error": "Simple message parsing failed"}), 400
        else:
            # JSON 파싱
            try:
                data = request.get_json(force=True)
                if data is None:
                    data = json.loads(raw_data)
            except Exception as e:
                log_debug("❌ JSON 파싱 실패", str(e))
                return jsonify({"error": "JSON parsing failed", "raw_data": raw_data[:200]}), 400
                
        if not data:
            return jsonify({"error": "Empty parsed data"}), 400
        
        # 필드 추출 및 검증
        alert_id = data.get("id", "")
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower() if data.get("side") else ""
        action = data.get("action", "").lower() if data.get("action") else ""
        strategy_name = data.get("strategy", "")
        
        missing_fields = []
        if not raw_symbol: missing_fields.append("symbol")
        if not side: missing_fields.append("side")
        if not action: missing_fields.append("action")
            
        if missing_fields:
            return jsonify({"error": f"Missing required fields: {missing_fields}"}), 400
        
        # 심볼 정규화
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": f"Symbol not supported: {raw_symbol}"}), 400
        
        # 중복 방지 체크
        if is_duplicate_alert(data):
            return jsonify({"status": "duplicate_ignored", "message": "중복 알림 무시됨"})
        
        # 청산 신호 처리
        if action == "exit":
            log_debug(f"🔄 청산 신호 처리 ({symbol})", f"전략: {strategy_name}")
            
            update_position_state(symbol, timeout=1)
            current_side = position_state.get(symbol, {}).get("side")
            
            if not current_side:
                success = True
                log_debug(f"⚠️ 청산 건너뜀 ({symbol})", "포지션 없음")
            else:
                success = close_position(symbol)
            
            if success and alert_id:
                mark_alert_processed(alert_id)
                
            return jsonify({
                "status": "success" if success else "error", 
                "action": "exit",
                "symbol": symbol,
                "strategy": strategy_name
            })
        
        # 진입 신호 처리
        if action == "entry" and side in ["long", "short"]:
            log_debug(f"🎯 진입 신호 처리 ({symbol})", f"{side} 방향, 전략: {strategy_name}")
            
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
                    log_debug("🔄 역포지션 처리", f"현재: {current_side} → 목표: {desired_side}")
                    if not close_position(symbol):
                        return jsonify({"status": "error", "message": "역포지션 청산 실패"})
                    time.sleep(3)
                    update_position_state(symbol)
            
            # 수량 계산 및 주문 실행
            qty = calculate_position_size(symbol, strategy_name)
            if qty <= 0:
                return jsonify({"status": "error", "message": "수량 계산 오류"})
            
            success = place_order(symbol, desired_side, qty)
            
            if success and alert_id:
                mark_alert_processed(alert_id)
            
            return jsonify({
                "status": "success" if success else "error", 
                "action": "entry",
                "symbol": symbol,
                "side": side,
                "qty": float(qty),
                "strategy": strategy_name,
                "entry_mode": "single"
            })
        
        return jsonify({"error": f"Invalid action: {action}"}), 400
        
    except Exception as e:
        log_debug(f"❌ 웹훅 전체 실패 ({symbol or 'unknown'})", str(e))
        
        if alert_id:
            mark_alert_processed(alert_id)
            
        return jsonify({
            "status": "error", 
            "message": str(e),
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
                    position_info = {k: float(v) if isinstance(v, Decimal) else v 
                                   for k, v in pos.items()}
                    positions[sym] = position_info
        
        # 중복 방지 상태
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
        
        # 동적 TP/SL 정보
        dynamic_tpsl_info = {}
        for symbol in SYMBOL_CONFIG:
            pos = position_state.get(symbol, {})
            if pos.get("side") and pos.get("entry_time"):
                tp, sl = calculate_dynamic_tpsl(symbol, pos["entry_time"])
                elapsed_minutes = (time.time() - pos["entry_time"]) / 60
                multipliers = get_tpsl_multipliers(symbol)
                
                dynamic_tpsl_info[symbol] = {
                    "elapsed_minutes": round(elapsed_minutes, 1),
                    "current_tp_pct": tp * 100,
                    "current_sl_pct": sl * 100,
                    "min_tp_pct": 0.25,  # 0.2% → 0.25%
                    "min_sl_pct": 0.15,  # 0.1% → 0.15%
                    "multiplier": {
                        "tp": multipliers["tp"],
                        "sl": multipliers["sl"]
                    },
                    "initial_tp_pct": 0.5 * multipliers["tp"] * 100,
                    "initial_sl_pct": 0.2 * multipliers["sl"] * 100
                }
        
        # 실시간 가격 정보
        price_info = {}
        with price_lock:
            for symbol, data in real_time_prices.items():
                age = time.time() - data["timestamp"]
                price_info[symbol] = {
                    "price": float(data["price"]),
                    "age_seconds": round(age, 1),
                    "is_fresh": age < 5
                }
        
        return jsonify({
            "status": "running",
            "mode": "live_trading_dynamic_tpsl_websocket",
            "timestamp": datetime.now().isoformat(),
            "margin_balance": float(equity),
            "positions": positions,
            "duplicate_prevention": duplicate_stats,
            "dynamic_tpsl_info": dynamic_tpsl_info,
            "real_time_prices": price_info,
            "websocket_connected": len(real_time_prices) > 0,
            "features": {
                "live_trading_only": True,
                "pinescript_alerts": True,
                "server_tpsl": True,  # TP/SL은 서버에서 자동 처리
                "dynamic_tpsl": True,
                "websocket_prices": True,
                "single_entry": True,
                "enhanced_logging": True
            }
        })
    except Exception as e:
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

@app.route("/dynamic-tpsl", methods=["GET"])
def dynamic_tpsl_status():
    """동적 TP/SL 상태 조회"""
    try:
        dynamic_info = {}
        
        for symbol in SYMBOL_CONFIG:
            pos = position_state.get(symbol, {})
            if pos.get("side") and pos.get("entry_time"):
                entry_time = pos["entry_time"]
                elapsed_minutes = (time.time() - entry_time) / 60
                tp_pct, sl_pct = calculate_dynamic_tpsl(symbol, entry_time)
                
                # 초기값 계산
                multipliers = get_tpsl_multipliers(symbol)
                initial_tp = 0.005 * multipliers["tp"]  # 0.5% 기준
                initial_sl = 0.002 * multipliers["sl"]  # 0.2% 기준
                
                # 다음 변화 시점 계산
                next_tp_change = math.ceil(elapsed_minutes - 10) + 10 if elapsed_minutes >= 10 else 10
                next_sl_change = math.ceil((elapsed_minutes - 10) / 3) * 3 + 10 if elapsed_minutes >= 10 else 10
                
                dynamic_info[symbol] = {
                    "side": pos["side"],
                    "entry_price": float(pos["price"]),
                    "entry_time": datetime.fromtimestamp(entry_time).strftime('%Y-%m-%d %H:%M:%S'),
                    "elapsed_minutes": round(elapsed_minutes, 1),
                    "initial_tp_pct": initial_tp * 100,
                    "initial_sl_pct": initial_sl * 100,
                    "current_tp_pct": tp_pct * 100,
                    "current_sl_pct": sl_pct * 100,
                    "min_tp_pct": 0.25,  # 0.2% → 0.25%
                    "min_sl_pct": 0.15,  # 0.1% → 0.15%
                    "next_tp_change_at": next_tp_change,
                    "next_sl_change_at": next_sl_change,
                    "tp_at_min": tp_pct <= 0.0025,  # 0.002 → 0.0025
                    "sl_at_min": sl_pct <= 0.0015,  # 0.001 → 0.0015
                    "multiplier": {
                        "tp": multipliers["tp"],
                        "sl": multipliers["sl"]
                    }
                }
        
        return jsonify({
            "status": "success",
            "timestamp": datetime.now().isoformat(),
            "dynamic_tpsl_enabled": True,
            "positions_with_dynamic_tpsl": dynamic_info,
            "rules": {
                "tp_reduction": "10분부터 1분마다 0.01%씩 감소",
                "sl_reduction": "10분부터 3분마다 0.01%씩 감소",
                "tp_minimum": "0.25%",  # 0.2% → 0.25%
                "sl_minimum": "0.15%"   # 0.1% → 0.15%
            },
            "base_rates_enhanced": {
                "tp_base": "0.5% (기존 0.4%에서 강화)",
                "sl_base": "0.2% (기존 0.15%에서 강화)"
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/trading-info", methods=["GET"])
def trading_info():
    """실거래 설정 정보 조회"""
    return jsonify({
        "trading_mode": "live_trading_only",
        "tp_sl_handling": "server_automatic",
        "entry_signals": "pinescript_alerts",
        "exit_signals": "pinescript_signals_only",  # TP/SL은 서버에서 자동 처리
        "symbol_multipliers": {
            "BTC_USDT": get_tpsl_multipliers("BTC_USDT"),
            "ETH_USDT": get_tpsl_multipliers("ETH_USDT"),
            "SOL_USDT": get_tpsl_multipliers("SOL_USDT"),
            "others": get_tpsl_multipliers("ADA_USDT")
        },
        "base_rates": {
            "tp_pct": 0.5,   # 강화된 기준: 0.4% → 0.5%
            "sl_pct": 0.2    # 강화된 기준: 0.15% → 0.2%
        },
        "dynamic_tpsl_rules": {
            "tp_reduction": "10분 후부터 1분마다 0.01%씩 감소",
            "sl_reduction": "10분 후부터 3분마다 0.01%씩 감소", 
            "tp_minimum": "0.25%",  # 0.2% → 0.25%
            "sl_minimum": "0.15%",  # 0.1% → 0.15%
            "initial_period": "10분간 초기값 유지"
        },
        "actual_tpsl_by_symbol": {
            "BTC_USDT": {"tp": "0.35%", "sl": "0.14%"},
            "ETH_USDT": {"tp": "0.4%", "sl": "0.16%"},
            "SOL_USDT": {"tp": "0.45%", "sl": "0.18%"},
            "others": {"tp": "0.5%", "sl": "0.2%"}
        },
        "compatibility": {
            "pinescript_version": "v5.0",
            "alert_format": "ENTRY:side|symbol|strategy|price|count",
            "exit_alert_format": "EXIT:side|symbol|reason|price|pnl_pct",
            "tp_sl_alerts": "disabled (서버에서 자동 처리)",
            "symbol_mapping": "auto_detected"
        }
    })
def get_current_prices():
    """현재 가격 조회"""
    try:
        prices = {}
        with price_lock:
            for symbol, data in real_time_prices.items():
                age = time.time() - data["timestamp"]
                prices[symbol] = {
                    "price": float(data["price"]),
                    "timestamp": data["timestamp"],
                    "age_seconds": round(age, 1),
                    "source": "websocket" if age < 5 else "stale"
                }
        
        # 웹소켓 데이터가 없는 심볼은 API로 조회
        for symbol in SYMBOL_CONFIG:
            if symbol not in prices:
                api_price = get_price(symbol)
                if api_price > 0:
                    prices[symbol] = {
                        "price": float(api_price),
                        "timestamp": time.time(),
                        "age_seconds": 0,
                        "source": "api"
                    }
        
        return jsonify({
            "status": "success",
            "timestamp": datetime.now().isoformat(),
            "prices": prices,
            "websocket_symbols": len([p for p in prices.values() if p["source"] == "websocket"])
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =================== 메인 실행 ===================
if __name__ == "__main__":
    log_initial_status()
    
    # 웹소켓 실시간 가격 모니터링 시작
    log_debug("🚀 웹소켓 시작", "동적 TP/SL 실시간 모니터링 스레드 시작")
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    
    # 백업 포지션 상태 갱신 시작
    log_debug("🚀 백업 루프 시작", "포지션 상태 갱신 스레드 시작")
    threading.Thread(target=backup_position_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", 
             f"포트 {port}에서 실행 (실거래 전용 + 강화된 동적 TP/SL + 웹소켓)\n"
             f"✅ 강화된 동적 TP/SL: 서버에서 Gate.io 웹소켓 기준으로 자동 처리\n"
             f"   📊 기준값 강화: TP 0.5% (기존 0.4%), SL 0.2% (기존 0.15%)\n"
             f"   🎯 4단계 가중치 시스템:\n"
             f"      - BTC: 70% (TP 0.35%, SL 0.14%) - 최고 안전성\n"
             f"      - ETH: 80% (TP 0.4%, SL 0.16%) - 높은 안전성\n"
             f"      - SOL: 90% (TP 0.45%, SL 0.18%) - 중간 안전성\n"
             f"      - 기타: 100% (TP 0.5%, SL 0.2%) - 일반/고위험\n"
             f"   ⏰ 동적 감소: 10분 후 TP 1분마다, SL 3분마다 0.01%씩\n"
             f"   🛡️ 최소값: TP 0.25%, SL 0.15% (안전성 강화)\n"
             f"✅ 진입신호: 파인스크립트 15초봉 극값 알림\n"
             f"✅ 청산신호: 파인스크립트 1분봉 시그널 알림 (TP/SL 제외)\n"
             f"✅ 진입 모드: 단일 진입 (역포지션시 청산 후 재진입)\n"
             f"✅ 실시간 가격: Gate.io 웹소켓 연동\n"
             f"✅ 중복 방지: 완벽한 알림 시스템 연동\n"
             f"✅ 심볼 매핑: 모든 형태 지원 (.P, PERP 등)\n"
             f"✅ 실거래 전용: 백테스트 불가 (알림 기반)\n"
             f"✅ 강화된 로깅: 모든 단계별 상세 로그")
    
    app.run(host="0.0.0.0", port=port, debug=False)
