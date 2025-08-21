#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.25 - TP 시스템 간소화
간단한 WebSocket 백업 TP만 사용, 복잡한 API TP 시스템 제거
"""
import os
import json
import time
import asyncio
import threading
import websockets
import logging
import sys
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi
from gate_api import exceptions as gate_api_exceptions
import queue
import pytz
import urllib.parse 

# ========
# 1. 로깅 설정
# ========
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception("")

# ========
# 2. Flask 앱 및 API 설정
# ========
try:
    app = Flask(__name__)
    logger.info("Flask 앱 초기화 성공")
except Exception as e:
    logger.error(f"Flask 앱 초기화 실패: {e}")
    try:
        app = Flask("gate_trading_server")
        logger.info("대안 Flask 앱 초기화 성공")
    except Exception as e2:
        logger.critical(f"Flask 앱 초기화 완전 실패: {e2}")
        sys.exit(1)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

if not API_KEY or not API_SECRET:
    logger.critical("API_KEY 또는 API_SECRET이 설정되지 않았습니다.")
    sys.exit(1)

try:
    config = Configuration(key=API_KEY, secret=API_SECRET)
    client = ApiClient(config)
    api = FuturesApi(client)
    unified_api = UnifiedApi(client)
    logger.info("Gate.io API 초기화 성공")
except Exception as e:
    logger.critical(f"Gate.io API 초기화 실패: {e}")
    sys.exit(1)

# ========
# 3. 상수 및 설정
# ========
COOLDOWN_SECONDS = 14
PRICE_DEVIATION_LIMIT_PCT = Decimal("0.0005")
MAX_SLIPPAGE_TICKS = 10
KST = pytz.timezone('Asia/Seoul')

SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "BTCUSDT.P": "BTC_USDT", "BTCUSDTPERP": "BTC_USDT", "BTC_USDT": "BTC_USDT", "BTC": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", "ETHUSDT.P": "ETH_USDT", "ETHUSDTPERP": "ETH_USDT", "ETH_USDT": "ETH_USDT", "ETH": "ETH_USDT",
    "SOLUSDT": "SOL_USDT", "SOLUSDT.P": "SOL_USDT", "SOLUSDTPERP": "SOL_USDT", "SOL_USDT": "SOL_USDT", "SOL": "SOL_USDT",
    "ADAUSDT": "ADA_USDT", "ADAUSDT.P": "ADA_USDT", "ADAUSDTPERP": "ADA_USDT", "ADA_USDT": "ADA_USDT", "ADA": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "SUIUSDT.P": "SUI_USDT", "SUIUSDTPERP": "SUI_USDT", "SUI_USDT": "SUI_USDT", "SUI": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "LINKUSDT.P": "LINK_USDT", "LINKUSDTPERP": "LINK_USDT", "LINK_USDT": "LINK_USDT", "LINK": "LINK_USDT",
    "PEPEUSDT": "PEPE_USDT", "PEPEUSDT.P": "PEPE_USDT", "PEPEUSDTPERP": "PEPE_USDT", "PEPE_USDT": "PEPE_USDT", "PEPE": "PEPE_USDT",
    "XRPUSDT": "XRP_USDT", "XRPUSDT.P": "XRP_USDT", "XRPUSDTPERP": "XRP_USDT", "XRP_USDT": "XRP_USDT", "XRP": "XRP_USDT",
    "DOGEUSDT": "DOGE_USDT", "DOGEUSDT.P": "DOGE_USDT", "DOGEUSDTPERP": "DOGE_USDT", "DOGE_USDT": "DOGE_USDT", "DOGE": "DOGE_USDT",
    "ONDOUSDT": "ONDO_USDT", "ONDOUSDT.P": "ONDO_USDT", "ONDOUSDTPERP": "ONDO_USDT", "ONDO_USDT": "ONDO_USDT", "ONDO": "ONDO_USDT",
}

SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"), "min_notional": Decimal("5"), "tp_mult": 0.55, "sl_mult": 0.55, "tick_size": Decimal("0.1")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"), "min_notional": Decimal("5"), "tp_mult": 0.65, "sl_mult": 0.65, "tick_size": Decimal("0.01")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 0.8, "sl_mult": 0.8, "tick_size": Decimal("0.001")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.0001")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.001")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.001")},
    "PEPE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000000"), "min_notional": Decimal("5"), "tp_mult": 1.2, "sl_mult": 1.2, "tick_size": Decimal("0.00000001"), "price_multiplier": Decimal("100000000.0")},
    "XRP_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.0001")},
    "DOGE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.2, "sl_mult": 1.2, "tick_size": Decimal("0.00001")},
    "ONDO_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.0001")}
}

# ========
# 4. 🔥 간소화된 양방향 상태 관리
# ========
position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
recent_signals = {}
signal_lock = threading.RLock()
tpsl_storage = {}
tpsl_lock = threading.RLock()
task_q = queue.Queue(maxsize=100)
WORKER_COUNT = min(6, max(2, os.cpu_count() * 2))

def get_default_pos_side_state():
    """간소화된 기본 상태"""
    return {
        "price": None, "size": Decimal("0"), "value": Decimal("0"), "entry_count": 0,
        "normal_entry_count": 0, "premium_entry_count": 0, "rescue_entry_count": 0,
        "entry_time": None, 'last_entry_ratio': Decimal("0")
    }

def initialize_states():
    with position_lock, tpsl_lock:
        for sym in SYMBOL_CONFIG:
            if sym not in position_state:
                position_state[sym] = {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()}
            if sym not in tpsl_storage:
                tpsl_storage[sym] = {"long": {}, "short": {}}

# ========
# 5. 핵심 유틸리티 함수
# ========
def _get_api_response(api_call, *args, **kwargs):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return api_call(*args, **kwargs)
        except Exception as e:
            if isinstance(e, gate_api_exceptions.ApiException):
                error_msg = f"API Error {e.status}: {e.body if hasattr(e, 'body') else e.reason}"
            else:
                error_msg = str(e)
            
            if attempt < max_retries - 1:
                log_debug("⚠️ API 호출 재시도", f"시도 {attempt+1}/{max_retries}: {error_msg}, 잠시 후 재시도")
                time.sleep(1)
            else:
                log_debug("❌ API 호출 최종 실패", error_msg, exc_info=True)
    return None

def normalize_symbol(raw_symbol):
    """🔥 강화된 심볼 정규화 (SOL 문제 해결)"""
    if not raw_symbol:
        return None
    
    symbol = str(raw_symbol).upper().strip()
    
    # 기본 매핑 먼저 시도
    normalized = SYMBOL_MAPPING.get(symbol)
    if normalized:
        return normalized
        
    # SOL 관련 추가 체크
    if "SOL" in symbol:
        return "SOL_USDT"
    
    # 다른 변형들도 체크
    clean_symbol = symbol.replace('.P', '').replace('PERP', '').replace('USDT', '')
    for key, value in SYMBOL_MAPPING.items():
        if clean_symbol in key:
            return value
    
    log_debug("⚠️ 심볼 정규화 실패", f"'{raw_symbol}' → 매핑되지 않은 심볼")
    return symbol

def get_total_collateral(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 30 and account_cache["data"]:
        return account_cache["data"]
    
    acc = _get_api_response(api.list_futures_accounts, SETTLE)
    equity = Decimal(str(getattr(acc, 'total', '0'))) if acc else Decimal("0")
    account_cache.update({"time": now, "data": equity})
    return equity

def get_price(symbol):
    ticker = _get_api_response(api.list_futures_tickers, SETTLE, contract=symbol)
    if ticker and isinstance(ticker, list) and len(ticker) > 0:
        return Decimal(str(ticker[0].last))
    return Decimal("0")

# ========
# 6. 파인스크립트 연동 함수
# ========
def get_signal_type_multiplier(signal_type):
    if "premium" in signal_type: return Decimal("2.0")
    if "rescue" in signal_type: return Decimal("1.5")
    return Decimal("1.0")

def get_entry_weight_from_score(score):
    try:
        score = Decimal(str(score))
        if score <= 10: return Decimal("0.25")
        elif score <= 30: return Decimal("0.35")
        elif score <= 50: return Decimal("0.50")
        elif score <= 70: return Decimal("0.65")
        elif score <= 90: return Decimal("0.80")
        else: return Decimal("1.00")
    except Exception: return Decimal("0.25")

def get_ratio_by_index(idx):
    ratios = [Decimal("5.0"), Decimal("10.0"), Decimal("25.0"), Decimal("60.0"), Decimal("200.0")]
    return ratios[min(idx, len(ratios) - 1)]

def get_tp_by_index(idx):
    tps = [Decimal("0.005"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
    return tps[min(idx, len(tps) - 1)]

def get_sl_by_index(idx):
    sls = [Decimal("0.04"), Decimal("0.038"), Decimal("0.035"), Decimal("0.033"), Decimal("0.03")]
    return sls[min(idx, len(sls) - 1)]

# ========
# 7. 🔥 평단가 매칭 수량 계산 함수 추가
# ========
def calculate_qty_to_match_avg_price(symbol, tv_expected_avg):
    """TradingView 예상 평단가에 맞추는 수량 계산"""
    try:
        # 현재 서버 포지션 정보
        pos_side_state = position_state.get(symbol, {})
        side = "long" if pos_side_state.get("long", {}).get("size", Decimal(0)) > 0 else "short"
        current_pos = pos_side_state.get(side, {})
        
        current_qty = current_pos.get('size', Decimal(0))
        current_avg = current_pos.get('price', Decimal(0))
        
        # 현재 시장가
        current_price = get_price(symbol)
        
        if current_qty == 0 or current_avg == 0:
            # 첫 진입이면 정상 계산
            return None
        
        # TV 예상 평단가에 맞는 수량 역계산
        target_avg = Decimal(str(tv_expected_avg))
        
        if current_price == target_avg:
            return Decimal(0)  # 추가 진입 불필요
        
        # 수량 역계산 공식
        additional_qty = (current_avg - target_avg) * current_qty / (target_avg - current_price)
        
        # 음수면 불가능
        if additional_qty <= 0:
            return None
        
        # 최소수량, 계약단위 조정
        cfg = SYMBOL_CONFIG[symbol]
        final_qty = max(additional_qty, cfg["min_qty"])
        
        # 최소 명목가치 확인
        notional = final_qty * current_price * cfg["contract_size"]
        if notional < cfg["min_notional"]:
            min_qty_for_notional = cfg["min_notional"] / (current_price * cfg["contract_size"])
            final_qty = max(final_qty, min_qty_for_notional)
        
        log_debug(f"📊 평단가 매칭 수량 계산 ({symbol})", 
                  f"목표평단: {target_avg:.8f}, 계산수량: {final_qty}")
        
        return final_qty
        
    except Exception as e:
        log_debug(f"❌ 평단가 매칭 계산 오류 ({symbol})", str(e))
        return None

# ========
# 8. 양방향 TP/SL 관리
# ========
def store_tp_sl(symbol, side, tp, sl, slippage_pct, entry_number):
    with tpsl_lock: 
        tpsl_storage.setdefault(symbol, {"long": {}, "short": {}}).setdefault(side, {})[entry_number] = {
            "tp": tp, "sl": sl, "entry_slippage_pct": slippage_pct, "entry_time": time.time()
        }

def get_tp_sl(symbol, side, entry_number=None):
    with tpsl_lock:
        side_storage = tpsl_storage.get(symbol, {}).get(side, {})
        if side_storage:
            if entry_number and entry_number in side_storage:
                data = side_storage[entry_number]
                return data["tp"], data["sl"], data["entry_slippage_pct"], data["entry_time"]
            elif side_storage:
                data = side_storage[max(side_storage.keys())]
                return data["tp"], data["sl"], data["entry_slippage_pct"], data["entry_time"]
    
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    return (Decimal("0.005") * Decimal(str(cfg["tp_mult"])), 
            Decimal("0.04") * Decimal(str(cfg["sl_mult"])), 
            Decimal("0"), 
            time.time())

# ========
# 9. 중복 신호 체크
# ========
def is_duplicate(data):
    with signal_lock:
        now = time.time()
        symbol = data.get('symbol')
        side = data.get('side')
        
        if not symbol or not side:
            return False
            
        symbol_id = f"{symbol}_{side}"
        
        last_signal = recent_signals.get(symbol_id)
        if last_signal and (now - last_signal.get("last_processed_time", 0) < COOLDOWN_SECONDS):
            return True
        
        recent_signals[symbol_id] = {"last_processed_time": now}
        recent_signals.update({k: v for k, v in recent_signals.items() if now - v.get("last_processed_time", 0) < 300})
        
        return False

# ========
# 10. 수량 계산
# ========
def calculate_position_size(symbol, signal_type, entry_score=50, current_signal_count=0):
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    if equity <= 0 or price <= 0:
        return Decimal("0")
    
    base_ratio = get_ratio_by_index(current_signal_count)
    signal_multiplier = get_signal_type_multiplier(signal_type)
    score_weight = get_entry_weight_from_score(entry_score)
    
    final_position_ratio = base_ratio * signal_multiplier * score_weight
    contract_value = price * cfg["contract_size"]
    
    if contract_value <= 0:
        return Decimal("0")
    
    base_qty = (equity * final_position_ratio / Decimal("100") / contract_value).quantize(Decimal('1'), rounding=ROUND_DOWN)
    qty_with_min = max(base_qty, cfg["min_qty"])
    
    if qty_with_min * contract_value < cfg["min_notional"]:
        final_qty = (cfg["min_notional"] / contract_value).quantize(Decimal('1'), rounding=ROUND_DOWN) + Decimal("1")
    else:
        final_qty = qty_with_min
        
    return final_qty

# ========
# 11. 양방향 포지션 상태 관리
# ========
def update_all_position_states():
    with position_lock:
        all_positions_from_api = _get_api_response(api.list_positions, SETTLE)
        if all_positions_from_api is None:
            log_debug("❌ 포지션 업데이트 실패", "API 호출 실패")
            return
            
        active_positions_set = set()
        for pos_info in all_positions_from_api:
            raw_symbol = pos_info.contract
            api_side = pos_info.mode
            if api_side == 'dual_long':
                side = 'long'
            elif api_side == 'dual_short':
                side = 'short'
            else:
                continue
            
            # 🔥 수정: 강화된 normalize_symbol 함수 사용
            symbol = normalize_symbol(raw_symbol)
            if not symbol:
                log_debug(f"⚠️ 알 수 없는 심볼", f"원본: {raw_symbol}, 정규화 실패")
                continue
                
            # 🔥 수정: 정규화된 심볼로 SYMBOL_CONFIG 확인
            if symbol not in SYMBOL_CONFIG:
                log_debug(f"⚠️ 미지원 심볼", f"원본: {raw_symbol}, 정규화: {symbol}")
                continue
                
            if symbol not in position_state:
                initialize_states()
            
            current_side_state = position_state[symbol][side]
            current_side_state["price"] = Decimal(str(pos_info.entry_price))
            current_side_state["size"] = Decimal(str(pos_info.size))
            current_side_state["value"] = Decimal(str(pos_info.size)) * Decimal(str(pos_info.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"]
            
            # 수동 포지션 감지
            if current_side_state["entry_count"] == 0 and current_side_state["size"] > 0:
                log_debug("🔄 수동 포지션 감지", f"{symbol} {side.upper()} 포지션")
                current_side_state["entry_count"] = 1
                current_side_state["entry_time"] = time.time()
                
            active_positions_set.add((symbol, side))
            
        # 유령 포지션 정리
        for symbol, sides in position_state.items():
            for side in ["long", "short"]:
                if (symbol, side) not in active_positions_set and sides[side]["size"] > 0:
                    log_debug(f"👻 유령 포지션 정리", f"{symbol} {side.upper()} 포지션 삭제")
                    position_state[symbol][side] = get_default_pos_side_state()
                    if symbol in tpsl_storage and side in tpsl_storage[symbol]:
                        tpsl_storage[symbol][side].clear()

# ========
# 12. 양방향 주문 실행
# ========
def place_order(symbol, side, qty, signal_type, final_position_ratio=Decimal("0"), tv_sync_data=None):
    with position_lock:
        try:
            if side == "long":
                order_size = int(qty)
            else:
                order_size = -int(qty)
            
            order = FuturesOrder(
                contract=symbol, 
                size=order_size, 
                price="0", 
                tif="ioc"
            )
            
            result = _get_api_response(api.create_futures_order, SETTLE, order)
            if not result:
                log_debug(f"❌ 주문 실행 실패 ({symbol}_{side.upper()})", "API 호출 실패")
                return False
            
            log_debug(f"✅ 주문 전송 성공 ({symbol}_{side.upper()})", f"주문 ID: {getattr(result, 'id', 'Unknown')}")
            
            pos_side_state = position_state.setdefault(symbol, {
                "long": get_default_pos_side_state(), 
                "short": get_default_pos_side_state()
            })[side]
            
            pos_side_state["entry_count"] += 1
            
            if "premium" in signal_type:
                pos_side_state["premium_entry_count"] += 1
            elif "normal" in signal_type:
                pos_side_state["normal_entry_count"] += 1
            elif "rescue" in signal_type:
                pos_side_state["rescue_entry_count"] += 1
                
            if "rescue" not in signal_type and final_position_ratio > 0:
                pos_side_state['last_entry_ratio'] = final_position_ratio
                
            pos_side_state["entry_time"] = time.time()
            
            time.sleep(2)
            update_all_position_states()
            
            return True
            
        except Exception as e:
            log_debug(f"❌ 주문 생성 오류 ({symbol}_{side.upper()})", str(e), exc_info=True)
            return False

def close_position(symbol, side, reason="manual"):
    with position_lock:
        try:
            # 🔥 수정: 현재 포지션 사이즈를 직접 조회해서 반대 주문
            pos_side_state = position_state.get(symbol, {}).get(side, {})
            current_size = pos_side_state.get("size", Decimal("0"))
            
            if current_size <= 0:
                log_debug(f"⚠️ 청산 주문 생략 ({symbol}_{side.upper()})", "포지션 없음")
                return True
            
            # 포지션 크기의 반대 주문으로 청산
            if side == "long":
                order_size = -int(current_size)  # 롱 포지션 청산은 음수
            else:
                order_size = int(current_size)   # 숏 포지션 청산은 양수
            
            order = FuturesOrder(
                contract=symbol, 
                size=order_size, 
                price="0", 
                tif="ioc"
            )
            
            result = _get_api_response(api.create_futures_order, SETTLE, order)
            if not result:
                log_debug(f"❌ 청산 주문 실행 실패 ({symbol}_{side.upper()})", "API 호출 실패")
                return False
            
            log_debug(f"✅ 청산 주문 전송 성공 ({symbol}_{side.upper()})", f"사유: {reason}, 수량: {order_size}")
            
            # 내부 상태 초기화
            pos_side_state = position_state.setdefault(symbol, {
                "long": get_default_pos_side_state(), 
                "short": get_default_pos_side_state()
            })
            pos_side_state[side] = get_default_pos_side_state()
            
            if symbol in tpsl_storage and side in tpsl_storage[symbol]:
                tpsl_storage[symbol][side].clear()
                
            with signal_lock:
                recent_signals.pop(f"{symbol}_{side}", None)
            return True
            
        except Exception as e:
            log_debug(f"❌ 청산 주문 생성 오류 ({symbol}_{side.upper()})", str(e), exc_info=True)
            return False

# ========
# 13. Flask 라우트
# ========
@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    with signal_lock: 
        recent_signals.clear()
    with tpsl_lock: 
        tpsl_storage.clear()
    log_debug("🔄 캐시 초기화", "모든 신호, TP/SL 캐시가 초기화되었습니다.")
    return jsonify({"status": "cache_cleared"})

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral(force=True)
        update_all_position_states()
        active_positions = {}
        
        with position_lock:
            for symbol, sides in position_state.items():
                for side, pos_data in sides.items():
                    if pos_data and pos_data.get("size", Decimal("0")) > 0:
                        pos_key = f"{symbol}_{side.upper()}"
                        active_positions[pos_key] = {
                            "side": side, "size": float(pos_data["size"]), "price": float(pos_data["price"]),
                            "value": float(pos_data["value"]), "entry_count": pos_data.get("entry_count", 0),
                            "normal_entry_count": pos_data.get("normal_entry_count", 0),
                            "premium_entry_count": pos_data.get("premium_entry_count", 0),
                            "rescue_entry_count": pos_data.get("rescue_entry_count", 0),
                            "last_entry_ratio": float(pos_data.get('last_entry_ratio', Decimal("0")))
                        }
        
        return jsonify({
            "status": "running", "version": "v6.25_simplified_tp",
            "current_time_kst": datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            "balance_usdt": float(equity), "active_positions": active_positions,
            "tp_system": "Simple WebSocket Backup Only",
            "queue_info": {"size": task_q.qsize(), "max_size": task_q.maxsize}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 중 오류 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.get_data(as_text=True))
        log_debug("📬 웹훅 수신", f"수신 데이터: {data}")
        
        action = data.get("action", "").lower()
        symbol = normalize_symbol(data.get("symbol", ""))
        side = data.get("side", "").lower()
        
        if not all([action, symbol, side]):
            log_debug("❌ 유효하지 않은 웹훅", f"필수 필드 누락: {data}")
            return jsonify({"error": "Invalid payload"}), 400
        
        if action == "entry":
            if is_duplicate(data):
                log_debug(f"🔄 중복 신호 무시 ({symbol}_{side.upper()})", "쿨다운 내 중복 신호")
                return jsonify({"status": "duplicate_ignored"}), 200
            
            # 🔥 추가: 평단가 매칭 처리
            expected_avg = data.get("expected_new_avg")
            if expected_avg:
                data["use_avg_matching"] = True
            
            task_q.put_nowait(data)
            log_debug(f"📥 작업 큐 추가 ({symbol}_{side.upper()})", f"현재 큐 크기: {task_q.qsize()}")
            return jsonify({"status": "queued"}), 200
            
        elif action == "exit":
            reason = data.get("reason", "").upper()
            price = data.get("price", 0)
            
            log_debug(f"📝 TV 청산 알림 수신 ({symbol}_{side.upper()})", 
                      f"사유: {reason}, 가격: {price}")
            return jsonify({"status": "exit_logged_only"}), 200
            
        return jsonify({"error": "Invalid action"}), 400
    except Exception as e:
        log_debug("❌ 웹훅 처리 중 예외 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ========
# 14. 🔥 간단한 WebSocket TP 모니터링 (복잡한 시스템 제거)
# ========
async def price_monitor():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols_to_subscribe = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                subscribe_msg = {
                    "time": int(time.time()), 
                    "channel": "futures.tickers", 
                    "event": "subscribe", 
                    "payload": symbols_to_subscribe
                }
                await ws.send(json.dumps(subscribe_msg))
                log_debug("🔌 웹소켓 구독", f"심볼: {len(symbols_to_subscribe)}개")
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=15)
                    result = json.loads(msg).get("result")
                    if isinstance(result, list):
                        for item in result:
                            simple_tp_monitor(item)  # 🔥 간단한 TP 모니터링
                    elif isinstance(result, dict):
                        simple_tp_monitor(result)
                        
        except asyncio.TimeoutError:
            log_debug("🔌 웹소켓 타임아웃", "15초 내 메시지 수신 없음, 재연결")
        except Exception as e:
            log_debug("🔌 웹소켓 오류", f"재연결 시도... {type(e).__name__}: {str(e)}")
        
        await asyncio.sleep(3)

def simple_tp_monitor(ticker):
    """🔥 간단하고 확실한 TP 모니터링 (복잡한 시스템 제거)"""
    try:
        symbol = normalize_symbol(ticker.get("contract"))
        price = Decimal(str(ticker.get("last", "0")))
        
        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0:
            return
            
        with position_lock:
            pos_side_state = position_state.get(symbol, {})
            
            # 롱 포지션 TP 체크
            long_size = pos_side_state.get("long", {}).get("size", Decimal(0))
            if long_size > 0:
                long_pos = pos_side_state["long"]
                entry_price = long_pos.get("price")
                entry_time = long_pos.get("entry_time", time.time())
                
                if entry_price and entry_price > 0:
                    # 간단한 TP 계산 (서버 기준 0.50% - 버퍼 없음)
                    cfg = SYMBOL_CONFIG[symbol]
                    base_tp_pct = Decimal("0.005") * Decimal(str(cfg["tp_mult"]))
                    
                    # 시간 감쇠 적용
                    time_elapsed = time.time() - entry_time
                    periods_15s = max(0, int(time_elapsed / 15))
                    tp_decay = Decimal("0.002") / 100 * Decimal(str(cfg["tp_mult"]))
                    tp_min = Decimal("0.12") / 100 * Decimal(str(cfg["tp_mult"]))
                    
                    current_tp_pct = max(tp_min, base_tp_pct - periods_15s * tp_decay)
                    tp_price = entry_price * (1 + current_tp_pct)
                    
                    if price >= tp_price:
                        log_debug(f"🎯 롱 TP 실행 ({symbol})", 
                                 f"현재가: {price:.8f}, TP가: {tp_price:.8f}, 포지션: {long_size}")
                        close_position(symbol, "long", "TP")
            
            # 숏 포지션 TP 체크
            short_size = pos_side_state.get("short", {}).get("size", Decimal(0))
            if short_size > 0:
                short_pos = pos_side_state["short"]
                entry_price = short_pos.get("price")
                entry_time = short_pos.get("entry_time", time.time())
                
                if entry_price and entry_price > 0:
                    # 간단한 TP 계산 (서버 기준 0.50% - 버퍼 없음)
                    cfg = SYMBOL_CONFIG[symbol]
                    base_tp_pct = Decimal("0.005") * Decimal(str(cfg["tp_mult"]))
                    
                    # 시간 감쇠 적용
                    time_elapsed = time.time() - entry_time
                    periods_15s = max(0, int(time_elapsed / 15))
                    tp_decay = Decimal("0.002") / 100 * Decimal(str(cfg["tp_mult"]))
                    tp_min = Decimal("0.12") / 100 * Decimal(str(cfg["tp_mult"]))
                    
                    current_tp_pct = max(tp_min, base_tp_pct - periods_15s * tp_decay)
                    tp_price = entry_price * (1 - current_tp_pct)
                    
                    if price <= tp_price:
                        log_debug(f"🎯 숏 TP 실행 ({symbol})", 
                                 f"현재가: {price:.8f}, TP가: {tp_price:.8f}, 포지션: {short_size}")
                        close_position(symbol, "short", "TP")
                
    except Exception as e:
        log_debug(f"❌ 간단 TP 모니터링 오류 ({ticker.get('contract', 'Unknown')})", str(e))
            
            # 숏 포지션 TP 체크
            if pos_side_state.get("short", {}).get("size", Decimal(0)) > 0:
                short_pos = pos_side_state["short"]
                entry_price = short_pos.get("price")
                entry_time = short_pos.get("entry_time", time.time())
                
                if entry_price and entry_price > 0:
                    # 간단한 TP 계산 (서버 기준 0.50% - 버퍼 없음)
                    cfg = SYMBOL_CONFIG[symbol]
                    base_tp_pct = Decimal("0.005") * Decimal(str(cfg["tp_mult"]))
                    
                    # 시간 감쇠 적용
                    time_elapsed = time.time() - entry_time
                    periods_15s = max(0, int(time_elapsed / 15))
                    tp_decay = Decimal("0.002") / 100 * Decimal(str(cfg["tp_mult"]))
                    tp_min = Decimal("0.12") / 100 * Decimal(str(cfg["tp_mult"]))
                    
                    current_tp_pct = max(tp_min, base_tp_pct - periods_15s * tp_decay)
                    tp_price = entry_price * (1 - current_tp_pct)
                    
                    if price <= tp_price:
                        log_debug(f"🎯 숏 TP 실행 ({symbol})", 
                                 f"현재가: {price:.8f}, TP가: {tp_price:.8f}")
                        close_position(symbol, "short", "TP")
                
    except Exception as e:
        log_debug(f"❌ 간단 TP 모니터링 오류 ({ticker.get('contract', 'Unknown')})", str(e))

# ========
# 15. 진입 처리 로직
# ========
def worker(idx):
    while True:
        try:
            data = task_q.get(timeout=1)
            try:
                handle_entry(data)
            except Exception as e:
                log_debug(f"❌ 워커-{idx} 처리 오류", f"작업 처리 중 예외: {str(e)}", exc_info=True)
            finally:
                task_q.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            log_debug(f"❌ 워커-{idx} 심각 오류", f"워커 스레드 오류: {str(e)}", exc_info=True)

def handle_entry(data):
    symbol = normalize_symbol(data.get("symbol"))
    side = data.get("side", "").lower()
    base_type = data.get("type", "normal")
    signal_type = f"{base_type}_{side}"
    
    entry_score = data.get("entry_score", 50)
    signal_price_raw = data.get('price')
    tv_tp_pct = Decimal(str(data.get("tp_pct", "0.5"))) / 100
    sl_pct = Decimal(str(data.get("sl_pct", "4.0"))) / 100
    
    # 🔥 추가: 평단가 매칭 관련 데이터
    expected_new_avg = data.get("expected_new_avg")
    use_avg_matching = data.get("use_avg_matching", False)
    
    if not all([symbol, side, signal_price_raw]):
        log_debug("❌ 진입 처리 불가", f"필수 정보 누락")
        return
    
    cfg = SYMBOL_CONFIG.get(symbol)
    if not cfg:
        return log_debug(f"⚠️ 진입 취소 ({symbol})", "미지원 심볼")
        
    current_price = get_price(symbol)
    price_multiplier = cfg.get("price_multiplier", Decimal("1.0"))
    signal_price = Decimal(str(signal_price_raw)) / price_multiplier
    
    if current_price <= 0 or signal_price <= 0:
        return log_debug(f"❌ 진입 취소 ({symbol})", f"유효하지 않은 가격")
    
    price_diff = abs(current_price - signal_price)
    allowed_slippage = max(signal_price * PRICE_DEVIATION_LIMIT_PCT, Decimal(str(MAX_SLIPPAGE_TICKS)) * cfg['tick_size'])
    if price_diff > allowed_slippage:
        return log_debug(f"⚠️ 진입 취소: 슬리피지 ({symbol}_{side.upper()})", f"가격 차이 초과")
        
    update_all_position_states()
    pos_side_state = position_state.get(symbol, {}).get(side, {})
    
    # 🔥 수정: 총 진입 제한을 13으로 변경
    entry_limits = {"premium": 5, "normal": 5, "rescue": 3}
    total_entry_limit = 13  # 🔥 10에서 13으로 수정
    
    entry_type_key = next((k for k in entry_limits if k in signal_type), None)

    if pos_side_state.get("entry_count", 0) >= total_entry_limit:
        log_debug(f"⚠️ 추가 진입 제한 ({symbol}_{side.upper()})", f"총 진입 횟수 최대치 도달: {total_entry_limit}")
        return

    if entry_type_key and pos_side_state.get(f"{entry_type_key}_entry_count", 0) >= entry_limits[entry_type_key]:
        log_debug(f"⚠️ 추가 진입 제한 ({symbol}_{side.upper()})", f"'{entry_type_key}' 유형 최대치 도달: {entry_limits[entry_type_key]}")
        return

    if pos_side_state.get("size", Decimal(0)) > 0 and "rescue" not in signal_type:
        avg_price = pos_side_state.get("price")
        if avg_price and ((side == "long" and current_price <= avg_price) or (side == "short" and current_price >= avg_price)):
            return log_debug(f"⚠️ 추가 진입 보류 ({symbol}_{side.upper()})", f"평단가 불리")

    current_signal_count = pos_side_state.get("premium_entry_count", 0) if "premium" in signal_type else pos_side_state.get("normal_entry_count", 0)
    
    # 🔥 평단가 매칭 수량 계산 시도
    if use_avg_matching and expected_new_avg:
        matched_qty = calculate_qty_to_match_avg_price(symbol, expected_new_avg)
        if matched_qty and matched_qty > 0:
            # 평단가 매칭으로 수량 결정
            qty = matched_qty
            log_debug(f"📊 평단가 매칭 수량 적용 ({symbol}_{side.upper()})", 
                      f"목표평단: {expected_new_avg}, 매칭수량: {qty}")
        else:
            # 평단가 매칭 실패시 기본 계산
            qty = calculate_position_size(symbol, signal_type, entry_score, current_signal_count)
    else:
        # 기본 수량 계산
        qty = calculate_position_size(symbol, signal_type, entry_score, current_signal_count)
    
    final_position_ratio = Decimal("0")
    
    if "rescue" in signal_type:
        last_ratio = pos_side_state.get('last_entry_ratio', Decimal("5.0"))
        if last_ratio > 0:
            equity, contract_val = get_total_collateral(), get_price(symbol) * cfg["contract_size"]
            if contract_val > 0:
                rescue_ratio = last_ratio * Decimal("1.5")
                qty = max((equity * rescue_ratio / 100 / contract_val).quantize(Decimal('1'), rounding=ROUND_DOWN), cfg["min_qty"])
                final_position_ratio = rescue_ratio
    
    if qty > 0:
        entry_action = "추가진입" if pos_side_state.get("size", 0) > 0 else "첫진입"
        if place_order(symbol, side, qty, signal_type, final_position_ratio):
            update_all_position_states()
            latest_pos_side_state = position_state.get(symbol, {}).get(side, {})
            log_debug(f"✅ {entry_action} 성공 ({symbol}_{side.upper()})", 
                      f"유형: {signal_type}, 수량: {float(qty)} 계약 (총 진입: {latest_pos_side_state.get('entry_count',0)}/13)")
            store_tp_sl(symbol, side, tv_tp_pct, sl_pct, abs(current_price - signal_price) / signal_price, latest_pos_side_state.get("entry_count", 0))
        else:
            log_debug(f"❌ {entry_action} 실패 ({symbol}_{side.upper()})", "주문 실행 중 오류")

# ========
# 16. 포지션 모니터링
# ========
def position_monitor():
    while True:
        time.sleep(30)
        try:
            update_all_position_states()
            total_value = Decimal("0")
            active_positions_log = []
            
            with position_lock:
                for symbol, sides in position_state.items():
                    for side, pos_data in sides.items():
                        if pos_data and pos_data.get("size", Decimal("0")) > 0:
                            total_value += pos_data.get("value", Decimal("0"))
                            pyramid_info = f"총:{pos_data['entry_count']}/13,일:{pos_data['normal_entry_count']}/5,프:{pos_data['premium_entry_count']}/5,레:{pos_data['rescue_entry_count']}/3"
                            active_positions_log.append(f"{symbol}_{side.upper()}: {pos_data['size']:.4f} @ {pos_data['price']:.8f} ({pyramid_info}, 가치: {pos_data['value']:.2f} USDT)")
            
            if active_positions_log:
                equity = get_total_collateral()
                exposure_pct = (total_value / equity * 100) if equity > 0 else 0
                log_debug("🚀 포지션 현황", f"활성: {len(active_positions_log)}개, 총가치: {total_value:.2f} USDT, 노출도: {exposure_pct:.1f}%")
                for pos_info in active_positions_log:
                    log_debug("  └", pos_info)
            else:
                log_debug("📊 포지션 현황", "활성 포지션 없음")
                
        except Exception as e:
            log_debug("❌ 포지션 모니터링 오류", str(e), exc_info=True)

# ========
# 17. 메인 실행
# ========
if __name__ == "__main__":
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.25 (TP 시스템 간소화)")
    log_debug("🎯 TP 시스템", "간단한 WebSocket 백업 TP만 사용 (복잡한 API TP 제거)")
    log_debug("🔧 주요 개선", "SOL 심볼 인식, 총 진입 제한 13회, 평단가 매칭 지원")
    
    initialize_states()
    
    log_debug("📊 초기 상태 로드", "현재 포지션 정보 불러오는 중...")
    update_all_position_states()
    
    initial_active_positions = []
    with position_lock:
        for symbol, sides in position_state.items():
            for side, pos_data in sides.items():
                if pos_data and pos_data.get("size", Decimal("0")) > 0:
                    initial_active_positions.append(
                        f"{symbol}_{side.upper()}: {pos_data['size']:.4f} @ {pos_data.get('price', 0):.8f}"
                    )
    
    log_debug("📊 초기 활성 포지션", f"{len(initial_active_positions)}개 감지" if initial_active_positions else "감지 안됨")
    for pos_info in initial_active_positions:
        log_debug("  └", pos_info)
        
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산", f"{equity:.2f} USDT" if equity > 0 else "조회 실패")
    
    # 백그라운드 스레드 시작
    threading.Thread(target=position_monitor, daemon=True, name="PositionMonitor").start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True, name="SimpleTPMonitor").start()
    
    # 워커 스레드 시작
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True, name=f"Worker-{i}").start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"Flask 서버 0.0.0.0:{port}")
    log_debug("✅ 준비 완료", "간소화된 TP 시스템 + 평단가 매칭 지원")
    
    try:
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
    except Exception as e:
        log_debug("❌ 서버 실행 실패", str(e), exc_info=True)
        sys.exit(1)
