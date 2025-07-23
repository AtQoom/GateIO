"""
Gate.io 자동매매 서버 v6.12 - 5단계 피라미딩 완전 대응

주요 기능:
1. 피라미딩 5회 진입 (0.2%→0.4%→1.2%→4.8%→9.6%)
2. 심볼별 가중치 적용 (BTC 0.5, ETH 0.6, SOL 0.8, PEPE/DOGE 1.2)
3. 시간 경과 TP/SL 감소 (15초마다)
4. 추가 진입 건너뛰기 로직
5. 2초 전 신호 및 확정 신호 구분
"""

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

# ========================================
# 1. 로깅 설정
# ========================================

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Werkzeug 로깅 비활성화
logging.getLogger('werkzeug').setLevel(logging.ERROR)

def log_debug(tag, msg, exc_info=False):
    """디버그 로깅"""
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception("")

# ========================================
# 2. Flask 앱 및 API 설정
# ========================================

app = Flask(__name__)

# API 설정
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

# Gate.io API 클라이언트
config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# ========================================
# 3. 상수 및 설정
# ========================================

# 쿨다운 설정
COOLDOWN_SECONDS = 12  # 14 → 12로 변경

# 심볼 매핑
SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "BTCUSDT.P": "BTC_USDT", "BTCUSDTPERP": "BTC_USDT", "BTC_USDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", "ETHUSDT.P": "ETH_USDT", "ETHUSDTPERP": "ETH_USDT", "ETH_USDT": "ETH_USDT",
    "SOLUSDT": "SOL_USDT", "SOLUSDT.P": "SOL_USDT", "SOLUSDTPERP": "SOL_USDT", "SOL_USDT": "SOL_USDT",
    "ADAUSDT": "ADA_USDT", "ADAUSDT.P": "ADA_USDT", "ADAUSDTPERP": "ADA_USDT", "ADA_USDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "SUIUSDT.P": "SUI_USDT", "SUIUSDTPERP": "SUI_USDT", "SUI_USDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "LINKUSDT.P": "LINK_USDT", "LINKUSDTPERP": "LINK_USDT", "LINK_USDT": "LINK_USDT",
    "PEPEUSDT": "PEPE_USDT", "PEPEUSDT.P": "PEPE_USDT", "PEPEUSDTPERP": "PEPE_USDT", "PEPE_USDT": "PEPE_USDT",
    "XRPUSDT": "XRP_USDT", "XRPUSDT.P": "XRP_USDT", "XRPUSDTPERP": "XRP_USDT", "XRP_USDT": "XRP_USDT",
    "DOGEUSDT": "DOGE_USDT", "DOGEUSDT.P": "DOGE_USDT", "DOGEUSDTPERP": "DOGE_USDT", "DOGE_USDT": "DOGE_USDT",
}

# 심볼별 설정 (파인스크립트 v6.12 가중치 완전 반영)
SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("0.0001"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.5,
        "sl_mult": 0.5
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("0.01"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.6,
        "sl_mult": 0.6
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("1"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.8,
        "sl_mult": 0.8
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("10"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("1"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("1"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("10000000"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.2,
        "sl_mult": 1.2
    },
    "XRP_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("10"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
    "DOGE_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("10"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.2,  # PEPE와 동일한 가중치
        "sl_mult": 1.2
    },
}

# ========================================
# 4. 전역 변수
# ========================================

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
recent_signals = {}
signal_lock = threading.RLock()
tpsl_storage = {}
tpsl_lock = threading.RLock()
pyramid_tracking = {}  # 심볼별 추가 진입 추적

# ========================================
# 5. 핵심 유틸리티 함수
# ========================================

def normalize_symbol(raw_symbol):
    """심볼 정규화"""
    if not raw_symbol:
        return None
    
    symbol = str(raw_symbol).upper().strip()
    
    # 직접 매핑 확인
    if symbol in SYMBOL_MAPPING:
        return SYMBOL_MAPPING[symbol]
    
    # 접미사 제거 후 재시도
    for suffix in ['.P', 'PERP']:
        if symbol.endswith(suffix):
            base = symbol[:-len(suffix)]
            if base in SYMBOL_MAPPING:
                return SYMBOL_MAPPING[base]
    
    return None

def get_total_collateral(force=False):
    """총 자산 조회"""
    now = time.time()
    if not force and account_cache["time"] > now - 30 and account_cache["data"]:
        return account_cache["data"]
    
    try:
        # Unified API 시도
        try:
            unified = unified_api.list_unified_accounts()
            for attr in ['unified_account_total_equity', 'equity']:
                if hasattr(unified, attr):
                    equity = Decimal(str(getattr(unified, attr)))
                    account_cache.update({"time": now, "data": equity})
                    return equity
        except Exception:
            pass
        
        # Futures API로 폴백
        acc = api.list_futures_accounts(SETTLE)
        available = Decimal(str(getattr(acc, 'available', '0')))
        account_cache.update({"time": now, "data": available})
        return available
        
    except Exception as e:
        log_debug("❌ 자산 조회 실패", str(e))
        return Decimal("0")

def get_price(symbol):
    """현재 가격 조회"""
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if ticker and len(ticker) > 0:
            return Decimal(str(ticker[0].last))
        return Decimal("0")
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
        return Decimal("0")

# ========================================
# 6. TP/SL 저장 및 관리
# ========================================

def store_tp_sl(symbol, tp, sl, entry_number):
    """TP/SL 저장 (진입 단계별)"""
    with tpsl_lock:
        if symbol not in tpsl_storage:
            tpsl_storage[symbol] = {}
        
        tpsl_storage[symbol][entry_number] = {
            "tp": tp, 
            "sl": sl, 
            "time": time.time(),
            "entry_time": time.time()
        }

def get_tp_sl(symbol, entry_number=None):
    """저장된 TP/SL 조회"""
    with tpsl_lock:
        if symbol in tpsl_storage:
            if entry_number and entry_number in tpsl_storage[symbol]:
                data = tpsl_storage[symbol][entry_number]
                return data["tp"], data["sl"], data["entry_time"]
            elif tpsl_storage[symbol]:
                latest_entry = max(tpsl_storage[symbol].keys())
                data = tpsl_storage[symbol][latest_entry]
                return data["tp"], data["sl"], data["entry_time"]
    
    # 기본값 반환
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    default_tp = Decimal("0.006") * Decimal(str(cfg["tp_mult"]))
    default_sl = Decimal("0.04") * Decimal(str(cfg["sl_mult"]))
    return default_tp, default_sl, time.time()

# ========================================
# 7. 중복 신호 체크
# ========================================

def is_duplicate(data):
    """중복 신호 체크 - 완전 수정 버전"""
    with signal_lock:
        now = time.time()
        symbol = data.get("symbol", "")
        side = data.get("side", "")
        action = data.get("action", "")
        signal_id = data.get("id", "")
        
        # 두 필드명 모두 지원
        is_pre_raw = data.get("is_pre", data.get("is_pre_signal", "false"))
        is_pre_signal = str(is_pre_raw).lower() == "true"
        
        if action == "entry":
            # 신호 ID 중복 체크 (먼저 체크)
            if signal_id and signal_id in recent_signals:
                signal_data = recent_signals[signal_id]
                if now - signal_data["time"] < 5:
                    return True
            
            # 방향별 쿨다운 체크
            key = f"{symbol}_{side}"
            
            if key in recent_signals:
                last_signal = recent_signals[key]
                time_diff = now - last_signal["time"]
                
                # 단순 쿨다운만 적용 (12초)
                if time_diff < 12:
                    return True
            
            # 신호 기록
            recent_signals[key] = {"time": now, "id": signal_id}
            
            # 신호 ID 기록
            if signal_id:
                recent_signals[signal_id] = {"time": now}
            
            # 반대 방향 제거
            opposite = f"{symbol}_{'short' if side == 'long' else 'long'}"
            recent_signals.pop(opposite, None)
        
        # 오래된 기록 정리
        recent_signals.update({
            k: v for k, v in recent_signals.items() 
            if now - v["time"] < 300
        })
        
        return False

# ========================================
# 8. 포지션 크기 계산
# ========================================

def calculate_position_size(symbol, signal_type, data=None):
    """포지션 크기 계산 (5단계 피라미딩) - 수정됨"""
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    
    if equity <= 0 or price <= 0:
        return Decimal("0")
    
    # 현재 진입 횟수
    entry_count = 0
    if symbol in position_state:
        entry_count = position_state[symbol].get("entry_count", 0)
    
    # 수정된 5단계 진입 비율 (실제 20%부터)
    entry_ratios = [
        Decimal("20"),     # 1차: 20%
        Decimal("40"),     # 2차: 40% 
        Decimal("120"),    # 3차: 120%
        Decimal("480"),    # 4차: 480%
        Decimal("960"),    # 5차: 960%
    ]
    
    if entry_count >= len(entry_ratios):
        log_debug(f"⚠️ 최대 진입 도달 ({symbol})", f"진입 횟수: {entry_count}")
        return Decimal("0")
    
    ratio = entry_ratios[entry_count]
    next_entry_number = entry_count + 1
    
    log_debug(f"📊 수량 계산 ({symbol})", 
             f"진입 #{next_entry_number}/5, 비율: {float(ratio)}%")
    
    # 수량 계산 (레버리지 1배 기준)
    adjusted = equity * ratio / 100  # 20% = 20/100 = 0.2
    raw_qty = adjusted / (price * cfg["contract_size"])
    qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
    final_qty = max(qty, cfg["min_qty"])
    
    # 최소 주문금액 체크
    value = final_qty * price * cfg["contract_size"]
    if value < cfg["min_notional"]:
        return Decimal("0")
    
    return final_qty

# ========================================
# 9. 포지션 상태 관리
# ========================================

def update_position_state(symbol):
    """포지션 상태 업데이트"""
    with position_lock:
        try:
            pos = api.get_position(SETTLE, symbol)
            size = Decimal(str(pos.size))
            
            if size != 0:
                # 기존 데이터 유지
                existing_count = position_state.get(symbol, {}).get("entry_count", 0)
                existing_time = position_state.get(symbol, {}).get("entry_time", time.time())
                
                position_state[symbol] = {
                    "price": Decimal(str(pos.entry_price)),
                    "side": "buy" if size > 0 else "sell",
                    "size": abs(size),
                    "value": abs(size) * Decimal(str(pos.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"],
                    "entry_count": existing_count,
                    "entry_time": existing_time
                }
            else:
                position_state[symbol] = {
                    "price": None, 
                    "side": None, 
                    "size": Decimal("0"), 
                    "value": Decimal("0"),
                    "entry_count": 0,
                    "entry_time": None
                }
                pyramid_tracking.pop(symbol, None)
                return True
            return False
        except Exception as e:
            if "POSITION_NOT_FOUND" in str(e):
                position_state[symbol] = {
                    "price": None, 
                    "side": None, 
                    "size": Decimal("0"), 
                    "value": Decimal("0"),
                    "entry_count": 0,
                    "entry_time": None
                }
                pyramid_tracking.pop(symbol, None)
                return True
            log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))
            return False

# ========================================
# 10. 주문 실행
# ========================================

def place_order(symbol, side, qty, entry_number):
    """주문 실행"""
    with position_lock:
        try:
            cfg = SYMBOL_CONFIG[symbol]
            qty_dec = Decimal(str(qty)).quantize(cfg["qty_step"], rounding=ROUND_DOWN)
            
            if qty_dec < cfg["min_qty"]:
                return False
            
            size = float(qty_dec) if side == "buy" else -float(qty_dec)
            order = FuturesOrder(
                contract=symbol, 
                size=size, 
                price="0", 
                tif="ioc", 
                reduce_only=False
            )
            
            api.create_futures_order(SETTLE, order)
            
            # 진입 횟수 증가
            current_count = position_state.get(symbol, {}).get("entry_count", 0)
            position_state.setdefault(symbol, {})["entry_count"] = current_count + 1
            position_state[symbol]["entry_time"] = time.time()
            
            log_debug(f"✅ 주문 성공 ({symbol})", 
                     f"{side.upper()} {float(qty_dec)} 계약 (진입 #{current_count + 1}/5)")
            
            time.sleep(2)
            update_position_state(symbol)
            return True
            
        except Exception as e:
            log_debug(f"❌ 주문 실패 ({symbol})", str(e))
            return False

def close_position(symbol, reason="manual"):
    """포지션 청산"""
    with position_lock:
        try:
            api.create_futures_order(
                SETTLE, 
                FuturesOrder(
                    contract=symbol, 
                    size=0, 
                    price="0", 
                    tif="ioc", 
                    close=True
                )
            )
            
            log_debug(f"✅ 청산 완료 ({symbol})", f"이유: {reason}")
            
            # 진입 정보 초기화
            if symbol in position_state:
                position_state[symbol]["entry_count"] = 0
                position_state[symbol]["entry_time"] = None
            
            # 관련 데이터 정리
            with signal_lock:
                keys = [k for k in recent_signals.keys() if k.startswith(symbol + "_")]
                for k in keys:
                    recent_signals.pop(k, None)
            
            with tpsl_lock:
                tpsl_storage.pop(symbol, None)
            
            pyramid_tracking.pop(symbol, None)
            
            time.sleep(1)
            update_position_state(symbol)
            return True
            
        except Exception as e:
            log_debug(f"❌ 청산 실패 ({symbol})", str(e))
            return False

# ========================================
# 11. Flask 라우트
# ========================================

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    """캐시 초기화"""
    with signal_lock:
        recent_signals.clear()
    with tpsl_lock:
        tpsl_storage.clear()
    pyramid_tracking.clear()
    return jsonify({"status": "cache_cleared"})

@app.before_request
def log_request():
    """요청 로깅"""
    if request.path != "/ping":
        log_debug("🌐 요청", f"{request.method} {request.path}")

@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    """웹훅 처리"""
    try:
        # ========== 1. 데이터 파싱 ==========
        raw_data = request.get_data(as_text=True)
        if not raw_data:
            return jsonify({"error": "Empty data"}), 400
        
        data = None
        
        # JSON 파싱 시도
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            # Form 데이터 시도
            if request.form:
                data = request.form.to_dict()
            # URL 디코딩 시도
            elif "&" not in raw_data and "=" not in raw_data:
                try:
                    import urllib.parse
                    decoded = urllib.parse.unquote(raw_data)
                    data = json.loads(decoded)
                except Exception:
                    pass
        
        if not data:
            # 트레이딩뷰 플레이스홀더 체크
            if "{{" in raw_data and "}}" in raw_data:
                return jsonify({
                    "error": "TradingView placeholder detected",
                    "solution": "Use {{strategy.order.alert_message}} in TradingView alert message field"
                }), 400
            return jsonify({"error": "Failed to parse data"}), 400
        
        # ========== 2. 필드 추출 ==========
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        signal_type = data.get("signal", "none")
        entry_number = int(data.get("entry_num", 1))
        tp_pct = float(data.get("tp_pct", 0))
        sl_pct = float(data.get("sl_pct", 0))
        is_pre_signal = str(data.get("is_pre", "false")).lower()
        signal_id = data.get("id", "")
        
        # ========== 3. 심볼 정규화 ==========
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": f"Invalid symbol: {raw_symbol}"}), 400
        
        # ========== 4. 중복 체크 ==========
        if is_duplicate(data):
            log_debug(f"🔄 중복 신호 무시 ({symbol})", 
                     f"{side} {action}, 2초전: {is_pre_signal}, ID: {signal_id}")
            return jsonify({"status": "duplicate_ignored"}), 200
        
        # ========== 5. 청산 처리 ==========
        if action == "exit":
            # TP/SL 청산은 서버에서 처리하므로 무시
            if data.get("reason") in ["TP", "SL"]:
                return jsonify({"status": "ignored", "reason": "tp_sl_handled_by_server"}), 200
            
            update_position_state(symbol)
            if position_state.get(symbol, {}).get("side"):
                close_position(symbol, data.get("reason", "signal"))
            return jsonify({"status": "success", "action": "exit"})
        
        # ========== 6. 진입 처리 ==========
        if action == "entry" and side in ["long", "short"]:
            # 진입 단계별 TP/SL (파인스크립트와 동일)
            tp_map = [0.006, 0.004, 0.003, 0.002, 0.0015]
            sl_map = [0.04, 0.038, 0.035, 0.033, 0.03]
            
            # 포지션 확인
            update_position_state(symbol)
            current = position_state.get(symbol, {}).get("side")
            desired = "buy" if side == "long" else "sell"
            
            # 반대 포지션 청산
            if current and current != desired:
                if not close_position(symbol, "reverse"):
                    return jsonify({
                        "status": "error", 
                        "message": "Failed to close opposite position"
                    })
                time.sleep(1)
                update_position_state(symbol)
            
            # 최대 5회 진입 체크
            entry_count = position_state.get(symbol, {}).get("entry_count", 0)
            if entry_count >= 5:
                return jsonify({
                    "status": "max_entries", 
                    "message": "Maximum 5 entries reached"
                })
            
            # 추가 진입 건너뛰기 로직
            if entry_count > 0:  # 추가 진입인 경우
                # 추가 진입 추적 정보 초기화
                if symbol not in pyramid_tracking:
                    pyramid_tracking[symbol] = {
                        "signal_count": 0,
                        "last_entered": False
                    }
                
                tracking = pyramid_tracking[symbol]
                
                # 건너뛰기 로직 적용
                should_skip = False
                
                # 추가 진입 신호 카운트 증가
                tracking["signal_count"] += 1
                signal_count = tracking["signal_count"]
                
                # 가격 조건 체크
                current_price = get_price(symbol)
                avg_price = position_state[symbol]["price"]
                price_ok = False
                
                if current == "buy" and current_price < avg_price:
                    price_ok = True
                elif current == "sell" and current_price > avg_price:
                    price_ok = True
                
                # 건너뛰기 로직
                if signal_count == 1:  # 첫 번째 추가 진입 신호
                    should_skip = True
                    skip_reason = "첫 번째 추가 진입 신호 건너뛰기"
                elif signal_count == 2:  # 두 번째 추가 진입 신호
                    should_skip = not price_ok
                    skip_reason = "가격 조건 미충족" if should_skip else ""
                else:  # 세 번째 이후
                    if tracking["last_entered"]:
                        should_skip = True
                        skip_reason = "이전 진입함 - 건너뛰기"
                    else:
                        should_skip = not price_ok
                        skip_reason = "가격 조건 미충족" if should_skip else ""
                
                if should_skip:
                    tracking["last_entered"] = False
                    log_debug(f"⏭️ 추가 진입 건너뛰기 ({symbol})", 
                             f"신호 #{signal_count}, 이유: {skip_reason}")
                    return jsonify({
                        "status": "skipped",
                        "symbol": symbol,
                        "signal_count": signal_count,
                        "reason": skip_reason,
                        "current_price": float(current_price),
                        "avg_price": float(avg_price),
                        "price_ok": price_ok
                    })
                else:
                    tracking["last_entered"] = True
            
            # 진입 번호 계산
            if entry_number > 0 and entry_number <= 5:
                actual_entry_number = entry_number
            else:
                actual_entry_number = entry_count + 1
            
            # TP/SL 계산
            entry_idx = actual_entry_number - 1
            if entry_idx < len(tp_map):
                # 심볼별 가중치 적용
                symbol_weight = SYMBOL_CONFIG[symbol]["tp_mult"]
                tp = Decimal(str(tp_map[entry_idx])) * Decimal(str(symbol_weight))
                sl = Decimal(str(sl_map[entry_idx])) * Decimal(str(symbol_weight))
                
                # TP/SL 저장
                store_tp_sl(symbol, tp, sl, actual_entry_number)
            else:
                # 기본값 사용
                tp = Decimal("0.006") * Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
                sl = Decimal("0.04") * Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
                store_tp_sl(symbol, tp, sl, actual_entry_number)
            
            # 수량 계산 및 주문
            qty = calculate_position_size(symbol, signal_type, data)
            if qty <= 0:
                return jsonify({
                    "status": "error", 
                    "message": "Invalid quantity"
                })
            
            success = place_order(symbol, desired, qty, actual_entry_number)
            
            # 상세 응답
            return jsonify({
                "status": "success" if success else "error",
                "action": "entry",
                "symbol": symbol,
                "side": side,
                "qty": float(qty),
                "entry_count": entry_count + (1 if success else 0),
                "entry_number": actual_entry_number,
                "signal_type": signal_type,
                "tp_pct": float(tp) * 100,
                "sl_pct": float(sl) * 100,
                "is_pre_signal": is_pre_signal,
                "signal_id": signal_id,
                "pyramid_info": pyramid_tracking.get(symbol) if entry_count > 0 else None
            })
        
        return jsonify({"error": "Invalid action"}), 400
        
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    """서버 상태"""
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
            pos = position_state.get(sym, {})
            if pos.get("side"):
                # 모든 진입의 TP/SL 정보 수집
                entry_count = pos.get("entry_count", 0)
                tp_sl_info = []
                
                for i in range(1, entry_count + 1):
                    tp, sl, entry_time = get_tp_sl(sym, i)
                    tp_sl_info.append({
                        "entry": i,
                        "tp_pct": float(tp) * 100,
                        "sl_pct": float(sl) * 100,
                        "elapsed_seconds": int(time.time() - entry_time)
                    })
                
                positions[sym] = {
                    "side": pos["side"],
                    "size": float(pos["size"]),
                    "price": float(pos["price"]),
                    "value": float(pos["value"]),
                    "entry_count": entry_count,
                    "symbol_multiplier": SYMBOL_CONFIG[sym]["tp_mult"],
                    "tp_sl_info": tp_sl_info,
                    "pyramid_tracking": pyramid_tracking.get(sym)
                }
        
        return jsonify({
            "status": "running",
            "version": "v6.12",
            "balance": float(equity),
            "positions": positions,
            "cooldown": COOLDOWN_SECONDS,
            "max_entries": 5,
            "symbol_weights": {sym: cfg["tp_mult"] for sym, cfg in SYMBOL_CONFIG.items()}
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========================================
# 12. WebSocket 모니터링
# ========================================

async def price_monitor():
    """실시간 가격 모니터링 및 TP/SL 체크"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                # 구독
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                
                log_debug("📡 웹소켓", f"구독 완료: {symbols}")
                
                # 메시지 처리
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    data = json.loads(msg)
                    
                    # 에러 처리
                    if data.get("event") == "error":
                        log_debug("❌ 웹소켓 에러", data.get("message", "Unknown error"))
                        continue
                    
                    # 구독 확인
                    if data.get("event") == "subscribe":
                        log_debug("✅ 구독 확인", data.get("channel", ""))
                        continue
                    
                    # 가격 데이터 처리
                    result = data.get("result")
                    if not result:
                        continue
                    
                    if isinstance(result, list):
                        for item in result:
                            check_tp_sl(item)
                    elif isinstance(result, dict):
                        check_tp_sl(result)
                        
        except asyncio.TimeoutError:
            log_debug("⏱️ 웹소켓 타임아웃", "재연결 시도...")
        except websockets.exceptions.ConnectionClosed:
            log_debug("🔌 웹소켓 연결 종료", "재연결 시도...")
        except Exception as e:
            log_debug("❌ 웹소켓 오류", str(e))
        
        await asyncio.sleep(5)

def check_tp_sl(ticker):
    """TP/SL 체크 (15초마다 감소)"""
    try:
        symbol = ticker.get("contract")
        price = Decimal(str(ticker.get("last", "0")))

        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0:
            return

        with position_lock:
            pos = position_state.get(symbol, {})
            entry_price = pos.get("price")
            side = pos.get("side")
            entry_count = pos.get("entry_count", 0)

            if not entry_price or not side or entry_count == 0:
                return

            # 심볼별 가중치
            symbol_weight = Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
            
            # 마지막 진입의 TP/SL 가져오기
            original_tp, original_sl, entry_time = get_tp_sl(symbol, entry_count)
            
            # 시간 경과 계산
            time_elapsed = time.time() - entry_time
            periods_15s = int(time_elapsed / 15)
            
            # TP 감소: 0.002% * 가중치
            tp_decay_weighted = Decimal("0.00002") * symbol_weight
            tp_reduction = Decimal(str(periods_15s)) * tp_decay_weighted
            adjusted_tp = max(Decimal("0.0012"), original_tp - tp_reduction)
            
            # SL 감소: 0.004% * 가중치
            sl_decay_weighted = Decimal("0.00004") * symbol_weight
            sl_reduction = Decimal(str(periods_15s)) * sl_decay_weighted
            adjusted_sl = max(Decimal("0.0009"), original_sl - sl_reduction)

            # TP/SL 트리거 체크
            tp_triggered = False
            sl_triggered = False

            if side == "buy":
                tp_price = entry_price * (1 + adjusted_tp)
                sl_price = entry_price * (1 - adjusted_sl)
                if price >= tp_price:
                    tp_triggered = True
                elif price <= sl_price:
                    sl_triggered = True
            else:
                tp_price = entry_price * (1 - adjusted_tp)
                sl_price = entry_price * (1 + adjusted_sl)
                if price <= tp_price:
                    tp_triggered = True
                elif price >= sl_price:
                    sl_triggered = True

            if tp_triggered:
                log_debug(f"🎯 TP 트리거 ({symbol})", 
                         f"가격: {price}, TP: {tp_price} ({adjusted_tp*100:.3f}%, "
                         f"{periods_15s*15}초 경과, 진입 #{entry_count})")
                close_position(symbol, "TP")
            elif sl_triggered:
                log_debug(f"🛑 SL 트리거 ({symbol})", 
                         f"가격: {price}, SL: {sl_price} ({adjusted_sl*100:.3f}%, "
                         f"{periods_15s*15}초 경과, 진입 #{entry_count})")
                close_position(symbol, "SL")

    except Exception as e:
        log_debug(f"❌ TP/SL 체크 오류 ({ticker.get('contract', 'Unknown')})", str(e))

# ========================================
# 13. 백그라운드 모니터링
# ========================================

def position_monitor():
    """포지션 상태 주기적 모니터링"""
    while True:
        try:
            time.sleep(300)  # 5분마다
            
            total_value = Decimal("0")
            active_positions = []
            
            for symbol in SYMBOL_CONFIG:
                if update_position_state(symbol):
                    pos = position_state.get(symbol, {})
                    if pos.get("side"):
                        total_value += pos["value"]
                        entry_count = pos.get("entry_count", 0)
                        tp_mult = SYMBOL_CONFIG[symbol]["tp_mult"]
                        
                        # 추가 진입 추적 정보
                        pyramid_info = ""
                        if symbol in pyramid_tracking:
                            tracking = pyramid_tracking[symbol]
                            pyramid_info = f", 신호: {tracking['signal_count']}회"
                        
                        active_positions.append(
                            f"{symbol}: {pos['side']} {pos['size']} @ {pos['price']} "
                            f"(진입 #{entry_count}/5, 가중치: {tp_mult}{pyramid_info})"
                        )
            
            if active_positions:
                equity = get_total_collateral()
                exposure_pct = (total_value / equity * 100) if equity > 0 else 0
                log_debug("📊 포지션 현황", 
                         f"활성: {len(active_positions)}개, "
                         f"총 가치: {total_value:.2f} USDT, "
                         f"노출도: {exposure_pct:.1f}%")
                for pos_info in active_positions:
                    log_debug("  └", pos_info)
                
        except Exception as e:
            log_debug("❌ 포지션 모니터링 오류", str(e))

def system_monitor():
    """시스템 상태 주기적 체크"""
    while True:
        try:
            time.sleep(3600)  # 1시간마다
            
            # 메모리 정리
            with signal_lock:
                now = time.time()
                recent_signals.update({
                    k: v for k, v in recent_signals.items() 
                    if now - v["time"] < 3600
                })
            
            with tpsl_lock:
                # 활성 포지션만 유지
                active_symbols = [sym for sym, pos in position_state.items() 
                                 if pos.get("side")]
                tpsl_storage.update({
                    k: v for k, v in tpsl_storage.items() 
                    if k in active_symbols
                })
            
            # 추가 진입 추적 정보 정리
            active_symbols = [sym for sym, pos in position_state.items() 
                             if pos.get("side")]
            pyramid_tracking.update({
                k: v for k, v in pyramid_tracking.items() 
                if k in active_symbols
            })
            
            # 시스템 상태 로그
            log_debug("🔧 시스템 상태", 
                     f"신호 캐시: {len(recent_signals)}개, "
                     f"TP/SL 저장소: {len(tpsl_storage)}개 심볼, "
                     f"추가진입 추적: {len(pyramid_tracking)}개 심볼")
            
        except Exception as e:
            log_debug("❌ 시스템 모니터링 오류", str(e))

# ========================================
# 14. 메인 실행
# ========================================

if __name__ == "__main__":
    """서버 시작"""
    
    log_debug("🚀 서버 시작", "v6.12 - 5단계 피라미딩 완전 대응")
    log_debug("📊 설정", f"심볼: {len(SYMBOL_CONFIG)}개, 쿨다운: {COOLDOWN_SECONDS}초, 최대 진입: 5회")
    
    # 심볼별 가중치 로그
    log_debug("🎯 심볼별 가중치", "")
    for symbol, cfg in SYMBOL_CONFIG.items():
        tp_weight = cfg["tp_mult"]
        symbol_name = symbol.replace("_USDT", "")
        log_debug(f"  └ {symbol_name}", f"TP/SL 가중치: {tp_weight*100}%")
    
    # 전략 설정 로그
    log_debug("📈 기본 설정", "익절률: 0.6%, 손절률: 4.0%")
    log_debug("🔄 TP/SL 감소", "15초마다 TP -0.002%*가중치, SL -0.004%*가중치")
    log_debug("📊 진입 비율", "1차: 20%, 2차: 40%, 3차: 120%, 4차: 480%, 5차: 960%")
    log_debug("📉 단계별 TP", "1차: 0.6%, 2차: 0.4%, 3차: 0.3%, 4차: 0.2%, 5차: 0.15%")
    log_debug("📉 단계별 SL", "1차: 4.0%, 2차: 3.8%, 3차: 3.5%, 4차: 3.3%, 5차: 3.0%")
    log_debug("🔄 추가진입", "1차: 건너뛰기, 2차: 가격 유리시, 3차+: 이전 진입시 건너뛰기")
    
    # 초기 자산 확인
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산", f"{equity:.2f} USDT")
    
    # 초기 포지션 확인
    active_count = 0
    for symbol in SYMBOL_CONFIG:
        if update_position_state(symbol):
            pos = position_state.get(symbol, {})
            if pos.get("side"):
                active_count += 1
                entry_count = pos.get("entry_count", 0)
                tp_mult = SYMBOL_CONFIG[symbol]["tp_mult"]
                log_debug(f"📊 포지션 ({symbol})", 
                         f"{pos['side']} {pos['size']} @ {pos['price']} "
                         f"(진입 #{entry_count}/5, 가중치: {tp_mult})")
    
    if active_count == 0:
        log_debug("📊 포지션", "활성 포지션 없음")
    
    # 백그라운드 작업 시작
    log_debug("🔄 백그라운드 작업", "시작...")
    
    threading.Thread(
        target=position_monitor, 
        daemon=True, 
        name="PositionMonitor"
    ).start()
    
    threading.Thread(
        target=system_monitor, 
        daemon=True, 
        name="SystemMonitor"
    ).start()
    
    threading.Thread(
        target=lambda: asyncio.run(price_monitor()), 
        daemon=True, 
        name="PriceMonitor"
    ).start()
    
    # Flask 실행
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹서버", f"포트 {port}에서 실행")
    log_debug("✅ 준비 완료", "파인스크립트 v6.12 웹훅 대기중...")
    
    app.run(host="0.0.0.0", port=port, debug=False)
