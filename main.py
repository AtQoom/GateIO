#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.33-server - 파인스크립트 v6.33 연동 최종본
(기존 v6.26 기반 수정)
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
# 🔥 수정: 웹훅 중복 전송 방지를 위한 최소 안전장치 (5초)
COOLDOWN_SECONDS = 5
PRICE_DEVIATION_LIMIT_PCT = Decimal("0.0003")
MAX_SLIPPAGE_TICKS = 5
KST = pytz.timezone('Asia/Seoul')

PREMIUM_TP_MULTIPLIERS = {
    "first_entry": Decimal("1.5"),
    "after_normal": Decimal("1.3"),
    "after_premium": Decimal("1.2")
}

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

DEFAULT_SYMBOL_CONFIG = {
    "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), 
    "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.001")
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

def get_symbol_config(symbol):
    if symbol in SYMBOL_CONFIG: return SYMBOL_CONFIG[symbol]
    log_debug(f"⚠️ 누락된 심볼 설정 ({symbol})", "기본값으로 진행")
    SYMBOL_CONFIG[symbol] = DEFAULT_SYMBOL_CONFIG.copy()
    return SYMBOL_CONFIG[symbol]

# ========
# 4. 상태 관리
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
manual_close_protection = {}
manual_protection_lock = threading.RLock()

def get_default_pos_side_state():
    return {"price": None, "size": Decimal("0"), "value": Decimal("0"), "entry_count": 0, "normal_entry_count": 0, "premium_entry_count": 0, "rescue_entry_count": 0, "entry_time": None, 'last_entry_ratio': Decimal("0")}

def initialize_states():
    with position_lock, tpsl_lock:
        for sym in SYMBOL_CONFIG:
            position_state[sym] = {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()}
            tpsl_storage[sym] = {"long": {}, "short": {}}

# ========
# 5. 핵심 유틸리티 함수
# ========
def set_manual_close_protection(symbol, side, duration=30):
    with manual_protection_lock:
        key = f"{symbol}_{side}"
        manual_close_protection[key] = {"protected_until": time.time() + duration, "reason": "manual_close_detected"}
        log_debug(f"🛡️ 수동 청산 보호 활성화 ({key})", f"{duration}초간 자동 TP 차단")

def is_manual_close_protected(symbol, side):
    with manual_protection_lock:
        key = f"{symbol}_{side}"
        if key in manual_close_protection and time.time() < manual_close_protection[key]["protected_until"]: return True
        elif key in manual_close_protection:
            del manual_close_protection[key]
            log_debug(f"🛡️ 수동 청산 보호 해제 ({key})", "보호 시간 만료")
    return False

def _get_api_response(api_call, *args, **kwargs):
    for attempt in range(3):
        try: return api_call(*args, **kwargs)
        except Exception as e:
            error_msg = str(e)
            if isinstance(e, gate_api_exceptions.ApiException): error_msg = f"API Error {e.status}: {e.body or e.reason}"
            if attempt < 2: log_debug("⚠️ API 호출 재시도", f"시도 {attempt+1}/3: {error_msg}")
            else: log_debug("❌ API 호출 최종 실패", error_msg, exc_info=True)
            time.sleep(1)
    return None

def normalize_symbol(raw_symbol):
    if not raw_symbol: return None
    symbol = str(raw_symbol).upper().strip()
    if symbol in SYMBOL_MAPPING: return SYMBOL_MAPPING[symbol]
    if "SOL" in symbol: return "SOL_USDT"
    clean_symbol = symbol.replace('.P', '').replace('PERP', '').replace('USDT', '')
    for key, value in SYMBOL_MAPPING.items():
        if clean_symbol in key: return value
    log_debug("⚠️ 심볼 정규화 실패", f"'{raw_symbol}' → 매핑되지 않음")
    return symbol

def get_total_collateral(force=False):
    now = time.time()
    if not force and account_cache.get("data") and account_cache.get("time", 0) > now - 30: return account_cache["data"]
    acc = _get_api_response(api.list_futures_accounts, SETTLE)
    equity = Decimal(str(getattr(acc, 'total', '0'))) if acc else Decimal("0")
    account_cache.update({"time": now, "data": equity})
    return equity

def get_price(symbol):
    ticker = _get_api_response(api.list_futures_tickers, SETTLE, contract=symbol)
    return Decimal(str(ticker[0].last)) if ticker and len(ticker) > 0 else Decimal("0")

# ========
# 6. 파인스크립트 연동 및 수량계산 함수
# ========
def get_signal_type_multiplier(signal_type):
    if "premium" in signal_type: return Decimal("2.0")
    # 🔥 수정: 레스큐 증폭률 3배 적용
    if "rescue" in signal_type: return Decimal("3.0")
    return Decimal("1.0")

def get_entry_weight_from_score(score):
    try:
        score = Decimal(str(score))
        if score <= 10: return Decimal("0.25")
        if score <= 30: return Decimal("0.35")
        if score <= 50: return Decimal("0.50")
        if score <= 70: return Decimal("0.65")
        if score <= 90: return Decimal("0.80")
        return Decimal("1.00")
    except: return Decimal("0.25")

def get_ratio_by_index(idx):
    # 🔥 수정: 파인스크립트와 동일한 진입 비율로 변경
    ratios = [Decimal("3.0"), Decimal("7.0"), Decimal("20.0"), Decimal("50.0"), Decimal("120.0")]
    return ratios[min(idx, len(ratios) - 1)]

def store_tp_sl(symbol, side, tp, sl, entry_number):
    with tpsl_lock:
        tpsl_storage.setdefault(symbol, {"long": {}, "short": {}})[side][entry_number] = {"tp": tp, "sl": sl, "entry_time": time.time()}

def is_duplicate(data):
    with signal_lock:
        now = time.time()
        symbol_id = f"{data.get('symbol')}_{data.get('side')}_{data.get('type')}"
        if now - recent_signals.get(symbol_id, 0) < COOLDOWN_SECONDS:
            log_debug(f"🔄 중복 신호 무시 ({symbol_id})", f"{COOLDOWN_SECONDS}초 내 중복 신호")
            return True
        recent_signals[symbol_id] = now
        recent_signals.update({k: v for k, v in recent_signals.items() if now - v < 300})
        return False

def calculate_position_size(symbol, signal_type, entry_score, current_signal_count):
    cfg, equity, price = get_symbol_config(symbol), get_total_collateral(), get_price(symbol)
    if equity <= 0 or price <= 0:
        return Decimal("0"), Decimal("0")
    
    base_ratio = get_ratio_by_index(current_signal_count)
    signal_multiplier = get_signal_type_multiplier(signal_type)
    score_weight = get_entry_weight_from_score(entry_score)
    final_ratio = base_ratio * signal_multiplier * score_weight
    
    # 레스큐는 이전 비율 기준
    if "rescue" in signal_type:
        with position_lock:
            # 🔥 추가: 'side' 변수가 없어서 오류가 발생할 수 있으므로 추가합니다.
            side = "long" if "long" in signal_type else "short"
            last_ratio = position_state.get(symbol, {}).get(side, {}).get('last_entry_ratio', Decimal("5.0"))
            final_ratio = last_ratio * signal_multiplier
            
    contract_value = price * cfg["contract_size"]
    if contract_value <= 0:
        return Decimal("0"), final_ratio
    
    # 🔥 수정: 변수명을 'final_position_ratio'에서 'final_ratio'로 변경
    position_value = equity * final_ratio / 100
    base_qty = (position_value / contract_value).quantize(Decimal('1'), rounding=ROUND_DOWN)
    
    qty_with_min = max(base_qty, cfg["min_qty"])
    
    if qty_with_min * contract_value < cfg["min_notional"]:
        final_qty = (cfg["min_notional"] / contract_value).quantize(Decimal('1'), rounding=ROUND_DOWN) + Decimal("1")
    else:
        final_qty = qty_with_min
        
    # 🔥 수정: 반환값에 qty가 누락되어 있었습니다.
    return final_qty, final_ratio

# ========
# 7. 주문 및 상태 관리
# ========
def place_order(symbol, side, qty):
    with position_lock:
        try:
            # 주문 전 상태 확인을 위해 먼저 호출
            update_all_position_states()
            original_size = position_state.get(symbol, {}).get(side, {}).get("size", Decimal("0"))
            
            order = FuturesOrder(contract=symbol, size=int(qty) if side == "long" else -int(qty), price="0", tif="ioc")
            result = _get_api_response(api.create_futures_order, SETTLE, order)
            
            if not result: 
                log_debug(f"❌ 주문 API 호출 실패 ({symbol}_{side.upper()})", "결과 없음")
                return False

            log_debug(f"✅ 주문 전송 ({symbol}_{side.upper()})", f"ID: {getattr(result, 'id', 'N/A')}")
            
            # ▼▼▼ [핵심 수정] API 지연에 대응하기 위해 대기 시간을 5초 -> 15초로 늘림 ▼▼▼
            for attempt in range(15):
                time.sleep(1)
                update_all_position_states()
                current_size = position_state.get(symbol, {}).get(side, {}).get("size", Decimal("0"))
                if current_size > original_size:
                    log_debug(f"🔄 포지션 업데이트 확인 ({symbol}_{side.upper()})", f"{attempt+1}초 소요")
                    return True
            # ▲▲▲ [수정 완료] ▲▲▲
            
            log_debug(f"❌ 포지션 업데이트 실패 ({symbol}_{side.upper()})", "15초 후에도 변경 없음. API 지연 가능성 높음.")
            return False
        except Exception as e:
            log_debug(f"❌ 주문 오류 ({symbol}_{side.upper()})", str(e), exc_info=True)
            return False

def update_all_position_states():
    """🔥 최종 안정성 강화: API 조회 실패 시, 기존 포지션 상태를 유지하여 '사라지는 포지션' 버그를 완벽히 해결"""
    with position_lock:
        api_positions = None
        api_source = "None"
        
        # 1. 통합 계정(Unified Account) 먼저 확인
        unified_positions = _get_api_response(unified_api.list_unified_positions, SETTLE)
        
        # ▼▼▼ [핵심] API 호출이 성공했을 때만(결과가 None이 아닐 때) api_positions에 값을 할당 ▼▼▼
        if unified_positions is not None:
            api_positions = unified_positions
            api_source = "UnifiedApi"
        else:
            # 통합 계정 API 호출 실패 시, 클래식 선물 계정으로 재시도
            futures_positions = _get_api_response(api.list_positions, SETTLE)
            if futures_positions is not None:
                api_positions = futures_positions
                api_source = "FuturesApi"
        
        # ▼▼▼ [매우 중요] 두 API 모두에서 정보를 가져오는데 실패했다면, 함수를 즉시 종료하여 기존 상태를 보존! ▼▼▼
        if api_positions is None:
            log_debug("❌ 포지션 동기화 실패", "API 엔드포인트 응답 없음. 이전 포지션 상태를 유지합니다.")
            return

        log_debug("📊 포지션 동기화 시작", f"데이터 소스: {api_source}, 찾은 포지션 수: {len(api_positions)}")
        
        active_positions_set = set()
        
        for pos in api_positions: # 성공적으로 가져온 API 결과로만 처리
            symbol = normalize_symbol(pos.contract)
            if not symbol: continue
            if symbol not in position_state: initialize_states()
            
            cfg = get_symbol_config(symbol)
            pos_size = Decimal(str(pos.size))
            
            if hasattr(pos, 'mode') and pos.mode in ['dual_long', 'dual_short']:
                side = 'long' if pos.mode == 'dual_long' else 'short'
                state = position_state[symbol][side]
                state.update({
                    "price": Decimal(str(pos.entry_price)), "size": pos_size,
                    "value": pos_size * Decimal(str(pos.mark_price)) * cfg["contract_size"]
                })
                active_positions_set.add((symbol, side))
            else:
                if pos_size > 0:
                    side = 'long'
                    state = position_state[symbol]['long']
                    state.update({
                        "price": Decimal(str(pos.entry_price)), "size": pos_size,
                        "value": pos_size * Decimal(str(pos.mark_price)) * cfg["contract_size"]
                    })
                    active_positions_set.add((symbol, side))
                    if position_state[symbol]['short']['size'] > 0: position_state[symbol]['short'] = get_default_pos_side_state()
                elif pos_size < 0:
                    side = 'short'
                    state = position_state[symbol]['short']
                    state.update({
                        "price": Decimal(str(pos.entry_price)), "size": abs(pos_size),
                        "value": abs(pos_size) * Decimal(str(pos.mark_price)) * cfg["contract_size"]
                    })
                    active_positions_set.add((symbol, side))
                    if position_state[symbol]['long']['size'] > 0: position_state[symbol]['long'] = get_default_pos_side_state()
        
        # API에서 확인된 포지션 외에는 모두 청산된 것으로 간주하고 정리
        for symbol, sides in position_state.items():
            for side in ["long", "short"]:
                if (symbol, side) not in active_positions_set and sides[side]["size"] > 0:
                    log_debug(f"🔄 수동/자동 청산 감지 ({symbol}_{side.upper()})", f"서버: {sides[side]['size']}, API: 없음")
                    set_manual_close_protection(symbol, side)
                    position_state[symbol][side] = get_default_pos_side_state()
                    if symbol in tpsl_storage: tpsl_storage[symbol][side].clear()

# ========
# 8. 핵심 진입 처리 로직
# ========
def handle_entry(data):
    symbol, side, base_type = normalize_symbol(data.get("symbol")), data.get("side", "").lower(), data.get("type", "normal")
    signal_type, entry_score = f"{base_type}_{side}", data.get("entry_score", 50)
    tv_tp_pct, tv_sl_pct = Decimal(str(data.get("tp_pct", "0")))/100, Decimal(str(data.get("sl_pct", "0")))/100
    
    if not all([symbol, side, data.get('price'), tv_tp_pct > 0, tv_sl_pct > 0]):
        return log_debug("❌ 진입 불가", f"필수 정보 누락: {data}")
    
    cfg, current_price = get_symbol_config(symbol), get_price(symbol)
    signal_price = Decimal(str(data['price'])) / cfg.get("price_multiplier", Decimal("1.0"))
    if current_price <= 0: return
    
    update_all_position_states()
    state = position_state[symbol][side]
    
    if state["entry_count"] == 0 and abs(current_price - signal_price) > max(signal_price * PRICE_DEVIATION_LIMIT_PCT, Decimal(str(MAX_SLIPPAGE_TICKS)) * cfg['tick_size']):
        return log_debug(f"⚠️ 첫 진입 취소: 슬리피지", f"가격차 큼")
    
    entry_limits = {"premium": 5, "normal": 5, "rescue": 3}
    if state["entry_count"] >= sum(entry_limits.values()) or state[f"{base_type}_entry_count"] >= entry_limits.get(base_type, 99):
        return log_debug(f"⚠️ 추가 진입 제한", "최대 횟수 도달")
    
    current_signal_count = state[f"{base_type}_entry_count"] if "rescue" not in signal_type else 0
    qty, final_ratio = calculate_position_size(symbol, signal_type, entry_score, current_signal_count)
        
    if qty > 0 and place_order(symbol, side, qty):
        # 진입 성공 후 상태 업데이트
        state["entry_count"] += 1
        state[f"{base_type}_entry_count"] += 1
        state["entry_time"] = time.time()
        if "rescue" not in signal_type: state['last_entry_ratio'] = final_ratio
        
        # ▼▼▼ [제안 드린 추가 로직] 프리미엄 TP 배수 저장 ▼▼▼
        current_multiplier = state.get("premium_tp_multiplier", Decimal("1.0"))
        
        if "premium" in signal_type:
            # 첫 프리미엄 진입 (기존 포지션 없음)
            if state["premium_entry_count"] == 1 and state["normal_entry_count"] == 0:
                new_multiplier = PREMIUM_TP_MULTIPLIERS["first_entry"]
            # 일반 진입 후 첫 프리미엄 진입
            elif state["premium_entry_count"] == 1 and state["normal_entry_count"] > 0:
                new_multiplier = PREMIUM_TP_MULTIPLIERS["after_normal"]
            # 프리미엄 진입 후 추가 프리미엄 진입
            else:
                new_multiplier = PREMIUM_TP_MULTIPLIERS["after_premium"]
            
            # 기존 배수와 새 배수 중 더 유리한(작은) 값을 선택하여 점진적으로 TP 목표를 낮춤
            state["premium_tp_multiplier"] = min(current_multiplier, new_multiplier) if current_multiplier != Decimal("1.0") else new_multiplier
            log_debug(f"✨ 프리미엄 TP 배수 적용", f"{symbol} {side.upper()} → {state['premium_tp_multiplier']:.2f}x")

        # 일반/레스큐 신호로 첫 진입 시, 배수 1.0으로 초기화
        elif state["entry_count"] == 1:
            state["premium_tp_multiplier"] = Decimal("1.0")
        # ▲▲▲ [로직 추가 완료] ▲▲▲

        store_tp_sl(symbol, side, tv_tp_pct, tv_sl_pct, state["entry_count"])
        log_debug(f"💾 TP/SL 저장", f"TP: {tv_tp_pct*100:.3f}%, SL: {tv_sl_pct*100:.3f}%")
        log_debug(f"✅ 진입 성공", f"유형: {signal_type}, 수량: {float(qty)}")

# ========
# 9. 웹훅 라우트 및 백그라운드 작업
# ========
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.get_data(as_text=True))
        log_debug("📬 웹훅 수신", f"데이터: {data}")
        if data.get("action") == "entry" and not is_duplicate(data):
            task_q.put_nowait(data)
        return "OK", 200
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e), exc_info=True)
        return "Error", 500

@app.route("/ping", methods=["GET"])
def ping(): return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral(force=True)
        update_all_position_states()
        active_positions = {}
        with position_lock:
            for symbol, sides in position_state.items():
                for side, pos_data in sides.items():
                    if pos_data["size"] != 0:
                        active_positions[f"{symbol}_{side.upper()}"] = {
                            "size": float(pos_data["size"]), "price": float(pos_data["price"]), "value": float(abs(pos_data["value"])),
                            "entry_count": pos_data["entry_count"], "normal_count": pos_data["normal_entry_count"],
                            "premium_count": pos_data["premium_entry_count"], "rescue_count": pos_data["rescue_entry_count"],
                            "last_ratio": float(pos_data['last_entry_ratio']),
                            # ▼▼▼ [개선] 모니터링을 위한 정보 추가 ▼▼▼
                            "premium_tp_mult": float(pos_data.get("premium_tp_multiplier", 1.0)),
                            "current_tp_pct": f"{float(pos_data.get('current_tp_pct', 0.0)) * 100:.4f}%"
                            # ▲▲▲ [수정 완료] ▲▲▲
                        }
        return jsonify({"status": "running", "version": "v6.33-server", "balance_usdt": float(equity), "active_positions": active_positions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
async def price_monitor():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"time": int(time.time()), "channel": "futures.tickers", "event": "subscribe", "payload": symbols}))
                log_debug("🔌 웹소켓 구독", f"{len(symbols)}개 심볼")
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    result = json.loads(msg).get("result")
                    if isinstance(result, list): [simple_tp_monitor(i) for i in result]
                    elif isinstance(result, dict): simple_tp_monitor(result)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
            log_debug("🔌 웹소켓 재연결", f"사유: {type(e).__name__}")
        except Exception as e: log_debug("🔌 웹소켓 오류", str(e), exc_info=True)
        await asyncio.sleep(5)

def simple_tp_monitor(ticker):
    """🔥 진짜 최종 수정: 'reduce_only=True' 옵션을 추가하여 헤지 모드에서 TP가 반대 포지션을 여는 치명적인 버그를 해결"""
    try:
        symbol = normalize_symbol(ticker.get("contract"))
        price = Decimal(str(ticker.get("last", "0")))
        
        if not symbol or price <= 0: return
        cfg = get_symbol_config(symbol)
        if not cfg: return
            
        with position_lock:
            pos_side_state = position_state.get(symbol, {})
            
            # --- 롱 포지션 TP 체크 ---
            long_actual_size = pos_side_state.get("long", {}).get("size", Decimal(0))
            if long_actual_size > 0 and not is_manual_close_protected(symbol, "long"):
                long_pos = pos_side_state["long"]
                entry_price = long_pos.get("price")
                entry_count = long_pos.get("entry_count", 0)

                if entry_price and entry_price > 0 and entry_count > 0:
                    premium_multiplier = long_pos.get("premium_tp_multiplier", Decimal("1.0"))
                    symbol_weight_tp = Decimal(str(cfg["tp_mult"]))
                    tp_map = [Decimal("0.0045"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
                    base_tp_pct = tp_map[min(entry_count-1, len(tp_map)-1)] * symbol_weight_tp * premium_multiplier
                    time_elapsed = time.time() - long_pos.get("entry_time", time.time())
                    periods_15s = max(0, int(time_elapsed / 15))
                    tp_decay_amt_ps = Decimal("0.002") / 100
                    tp_min_pct_ps = Decimal("0.16") / 100
                    tp_reduction = Decimal(str(periods_15s)) * (tp_decay_amt_ps * symbol_weight_tp * premium_multiplier)
                    min_tp_with_mult = tp_min_pct_ps * symbol_weight_tp * premium_multiplier
                    current_tp_pct = max(min_tp_with_mult, base_tp_pct - tp_reduction)
                    long_pos["current_tp_pct"] = current_tp_pct
                    tp_price = entry_price * (1 + current_tp_pct)
                    
                    if price >= tp_price:
                        set_manual_close_protection(symbol, 'long', duration=20)
                        log_debug(f"🎯 롱 TP 실행 ({symbol})", f"진입가: {entry_price:.8f}, 현재가: {price:.8f}, TP가: {tp_price:.8f}")
                        
                        try:
                            # ▼▼▼ [핵심 수정] reduce_only=True 플래그를 추가하여 '청산 전용' 주문으로 전송 ▼▼▼
                            order = FuturesOrder(contract=symbol, size=-int(long_actual_size), price="0", tif="ioc", reduce_only=True)
                            result = _get_api_response(api.create_futures_order, SETTLE, order)
                            if result:
                                log_debug(f"✅ 롱 TP 청산 주문 성공 ({symbol})", f"ID: {getattr(result, 'id', 'Unknown')}")
                                position_state[symbol]['long'] = get_default_pos_side_state()
                                if symbol in tpsl_storage: tpsl_storage[symbol]['long'].clear()
                                log_debug(f"🔄 TP 실행 후 상태 초기화 완료 ({symbol}_long)")
                            else:
                                log_debug(f"❌ 롱 TP 청산 주문 실패 ({symbol})", "API 호출 실패. 20초 후 재시도 가능.")
                        except Exception as e:
                            log_debug(f"❌ 롱 TP 청산 오류 ({symbol})", str(e), exc_info=True)

            # --- 숏 포지션 TP 체크 ---
            short_actual_size = pos_side_state.get("short", {}).get("size", Decimal(0))
            if short_actual_size > 0 and not is_manual_close_protected(symbol, "short"):
                short_pos = pos_side_state["short"]
                entry_price = short_pos.get("price")
                entry_count = short_pos.get("entry_count", 0)

                if entry_price and entry_price > 0 and entry_count > 0:
                    premium_multiplier = short_pos.get("premium_tp_multiplier", Decimal("1.0"))
                    symbol_weight_tp = Decimal(str(cfg["tp_mult"]))
                    tp_map = [Decimal("0.005"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
                    base_tp_pct = tp_map[min(entry_count-1, len(tp_map)-1)] * symbol_weight_tp * premium_multiplier
                    time_elapsed = time.time() - short_pos.get("entry_time", time.time())
                    periods_15s = max(0, int(time_elapsed / 15))
                    tp_decay_amt_ps = Decimal("0.002") / 100
                    tp_min_pct_ps = Decimal("0.16") / 100
                    tp_reduction = Decimal(str(periods_15s)) * (tp_decay_amt_ps * symbol_weight_tp * premium_multiplier)
                    min_tp_with_mult = tp_min_pct_ps * symbol_weight_tp * premium_multiplier
                    current_tp_pct = max(min_tp_with_mult, base_tp_pct - tp_reduction)
                    short_pos["current_tp_pct"] = current_tp_pct
                    tp_price = entry_price * (1 - current_tp_pct)
                    
                    if price <= tp_price:
                        set_manual_close_protection(symbol, 'short', duration=20)
                        log_debug(f"🎯 숏 TP 실행 ({symbol})", f"진입가: {entry_price:.8f}, 현재가: {price:.8f}, TP가: {tp_price:.8f}")
                        
                        try:
                            # ▼▼▼ [핵심 수정] reduce_only=True 플래그를 추가하여 '청산 전용' 주문으로 전송 ▼▼▼
                            order = FuturesOrder(contract=symbol, size=int(short_actual_size), price="0", tif="ioc", reduce_only=True)
                            result = _get_api_response(api.create_futures_order, SETTLE, order)
                            if result:
                                log_debug(f"✅ 숏 TP 청산 주문 성공 ({symbol})", f"주문 ID: {getattr(result, 'id', 'Unknown')}")
                                position_state[symbol]['short'] = get_default_pos_side_state()
                                if symbol in tpsl_storage: tpsl_storage[symbol]['short'].clear()
                                log_debug(f"🔄 TP 실행 후 상태 초기화 완료 ({symbol}_short)")
                            else:
                                log_debug(f"❌ 숏 TP 청산 주문 실패 ({symbol})", "API 호출 실패. 20초 후 재시도 가능.")
                        except Exception as e:
                            log_debug(f"❌ 숏 TP 청산 오류 ({symbol})", str(e), exc_info=True)
                
    except Exception as e:
        log_debug(f"❌ TP 모니터링 오류 ({ticker.get('contract', 'Unknown')})", str(e))

def worker(idx):
    while True:
        try: handle_entry(task_q.get(timeout=60))
        except queue.Empty: continue
        except Exception as e: log_debug(f"❌ 워커-{idx} 오류", str(e), exc_info=True)

def position_monitor():
    while True:
        time.sleep(30)
        try:
            update_all_position_states()
            log_debug("📊 포지션 현황", "주기적 상태 업데이트 완료")
        except Exception as e: log_debug("❌ 모니터링 오류", str(e), exc_info=True)

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.33-server")
    log_debug("🛡️ 안전장치", f"웹훅 중복 방지 쿨다운: {COOLDOWN_SECONDS}초")
    initialize_states()
    log_debug("💰 초기 자산", f"{get_total_collateral(force=True):.2f} USDT")
    update_all_position_states()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    for i in range(WORKER_COUNT): threading.Thread(target=worker, args=(i,), daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"0.0.0.0:{port} 대기 중...")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
