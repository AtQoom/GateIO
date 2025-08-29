#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.33-server - 파인스크립트 v6.33 연동 최종본 (+동적 심볼 정리, status 확장)
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

# ======== 1. 로깅 설정 ========
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception("")

# ======== 2. Flask 앱 및 API 설정 ========
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

# ======== 3. 상수 및 설정 ========
COOLDOWN_SECONDS = 5
PRICE_DEVIATION_LIMIT_PCT = Decimal("0.0003")
MAX_SLIPPAGE_TICKS = 5
KST = pytz.timezone('Asia/Seoul')

PREMIUM_TP_MULTIPLIERS = {
    "first_entry":   Decimal("1.5"),
    "after_normal":  Decimal("1.3"),
    "after_premium": Decimal("1.2")
}

SYMBOL_MAPPING = {
    "BTCUSDT":"BTC_USDT","BTCUSDT.P":"BTC_USDT","BTCUSDTPERP":"BTC_USDT","BTC_USDT":"BTC_USDT","BTC":"BTC_USDT",
    "ETHUSDT":"ETH_USDT","ETHUSDT.P":"ETH_USDT","ETHUSDTPERP":"ETH_USDT","ETH_USDT":"ETH_USDT","ETH":"ETH_USDT",
    "SOLUSDT":"SOL_USDT","SOLUSDT.P":"SOL_USDT","SOLUSDTPERP":"SOL_USDT","SOL_USDT":"SOL_USDT","SOL":"SOL_USDT",
    "ADAUSDT":"ADA_USDT","ADAUSDT.P":"ADA_USDT","ADAUSDTPERP":"ADA_USDT","ADA_USDT":"ADA_USDT","ADA":"ADA_USDT",
    "SUIUSDT":"SUI_USDT","SUIUSDT.P":"SUI_USDT","SUIUSDTPERP":"SUI_USDT","SUI_USDT":"SUI_USDT","SUI":"SUI_USDT",
    "LINKUSDT":"LINK_USDT","LINKUSDT.P":"LINK_USDT","LINKUSDTPERP":"LINK_USDT","LINK_USDT":"LINK_USDT","LINK":"LINK_USDT",
    "PEPEUSDT":"PEPE_USDT","PEPEUSDT.P":"PEPE_USDT","PEPEUSDTPERP":"PEPE_USDT","PEPE_USDT":"PEPE_USDT","PEPE":"PEPE_USDT",
    "XRPUSDT":"XRP_USDT","XRPUSDT.P":"XRP_USDT","XRPUSDTPERP":"XRP_USDT","XRP_USDT":"XRP_USDT","XRP":"XRP_USDT",
    "DOGEUSDT":"DOGE_USDT","DOGEUSDT.P":"DOGE_USDT","DOGEUSDTPERP":"DOGE_USDT","DOGE_USDT":"DOGE_USDT","DOGE":"DOGE_USDT",
    "ONDOUSDT":"ONDO_USDT","ONDOUSDT.P":"ONDO_USDT","ONDOUSDTPERP":"ONDO_USDT","ONDO_USDT":"ONDO_USDT","ONDO":"ONDO_USDT",
}

DEFAULT_SYMBOL_CONFIG = {
    "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
    "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.001")
}

SYMBOL_CONFIG = {
    "BTC_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("0.0001"),"min_notional":Decimal("5"),"tp_mult":0.55,"sl_mult":0.55,"tick_size":Decimal("0.1")},
    "ETH_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("0.01"),"min_notional":Decimal("5"),"tp_mult":0.65,"sl_mult":0.65,"tick_size":Decimal("0.01")},
    "SOL_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("1"),"min_notional":Decimal("5"),"tp_mult":0.8,"sl_mult":0.8,"tick_size":Decimal("0.001")},
    "ADA_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("10"),"min_notional":Decimal("5"),"tp_mult":1.0,"sl_mult":1.0,"tick_size":Decimal("0.0001")},
    "SUI_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("1"),"min_notional":Decimal("5"),"tp_mult":1.0,"sl_mult":1.0,"tick_size":Decimal("0.001")},
    "LINK_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("1"),"min_notional":Decimal("5"),"tp_mult":1.0,"sl_mult":1.0,"tick_size":Decimal("0.001")},
    "PEPE_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("10000000"),"min_notional":Decimal("5"),"tp_mult":1.2,"sl_mult":1.2,"tick_size":Decimal("0.00000001"),"price_multiplier":Decimal("100000000.0")},
    "XRP_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("10"),"min_notional":Decimal("5"),"tp_mult":1.0,"sl_mult":1.0,"tick_size":Decimal("0.0001")},
    "DOGE_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("10"),"min_notional":Decimal("5"),"tp_mult":1.2,"sl_mult":1.2,"tick_size":Decimal("0.00001")},
    "ONDO_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("1"),"min_notional":Decimal("5"),"tp_mult":1.0,"sl_mult":1.0,"tick_size":Decimal("0.0001")}
}

def get_symbol_config(symbol):
    if symbol in SYMBOL_CONFIG: 
        return SYMBOL_CONFIG[symbol]
    log_debug(f"⚠️ 누락된 심볼 설정 ({symbol})", "기본값으로 진행")
    SYMBOL_CONFIG[symbol] = DEFAULT_SYMBOL_CONFIG.copy()
    return SYMBOL_CONFIG[symbol]

# ======== 4. 상태 관리 ========
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

# 동적 심볼 관리 + 메트릭
dynamic_symbols = set()
ws_last_payload = []        # 마지막 구독 payload
ws_last_subscribed_at = 0   # 최근 구독시각(epoch)

def get_default_pos_side_state():
    return {
        "price": None, "size": Decimal("0"), "value": Decimal("0"),
        "entry_count": 0, "normal_entry_count": 0, "premium_entry_count": 0,
        "rescue_entry_count": 0, "entry_time": None, "last_entry_ratio": Decimal("0"),
        "current_tp_pct": Decimal("0")
    }

def initialize_states():
    """API에서 받는 모든 심볼에 대해 동적으로 상태를 초기화"""
    with position_lock, tpsl_lock:
        for sym in SYMBOL_CONFIG:
            position_state[sym] = {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()}
            tpsl_storage[sym] = {"long": {}, "short": {}}
        try:
            api_positions = _get_api_response(api.list_positions, settle=SETTLE)
            if api_positions:
                for pos in api_positions:
                    symbol = normalize_symbol(pos.contract)
                    if symbol and symbol not in position_state:
                        log_debug("🆕 새 심볼 초기화", f"{symbol} - API에서 발견")
                        position_state[symbol] = {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()}
                        tpsl_storage[symbol] = {"long": {}, "short": {}}
                        dynamic_symbols.add(symbol)
        except Exception as e:
            log_debug("⚠️ 동적 초기화 실패", str(e))

# ======== 5. 유틸 ========
def set_manual_close_protection(symbol, side, duration=30):
    with manual_protection_lock:
        key = f"{symbol}_{side}"
        manual_close_protection[key] = {"protected_until": time.time() + duration, "reason": "manual_close_detected"}
        log_debug("🛡️ 수동 청산 보호 활성화", f"{key} {duration}초")

def is_manual_close_protected(symbol, side):
    with manual_protection_lock:
        key = f"{symbol}_{side}"
        if key in manual_close_protection and time.time() < manual_close_protection[key]["protected_until"]:
            return True
        elif key in manual_close_protection:
            del manual_close_protection[key]
            log_debug("🛡️ 수동 청산 보호 해제", key)
    return False

def _get_api_response(api_call, *args, **kwargs):
    for attempt in range(3):
        try:
            return api_call(*args, **kwargs)
        except Exception as e:
            error_msg = str(e)
            if isinstance(e, gate_api_exceptions.ApiException):
                error_msg = f"API Error {e.status}: {e.body or e.reason}"
            if attempt < 2:
                log_debug("⚠️ API 호출 재시도", f"{attempt+1}/3: {error_msg}")
            else:
                log_debug("❌ API 호출 최종 실패", error_msg, exc_info=True)
            time.sleep(1)
    return None

def normalize_symbol(raw_symbol):
    if not raw_symbol: return None
    symbol = str(raw_symbol).upper().strip()
    if symbol in SYMBOL_MAPPING: return SYMBOL_MAPPING[symbol]
    clean = symbol.replace('.P', '').replace('PERP', '').replace('USDT', '')
    for key, value in SYMBOL_MAPPING.items():
        if clean in key: return value
    log_debug("⚠️ 심볼 정규화 실패", f"'{raw_symbol}' → 매핑되지 않음")
    return symbol

def get_total_collateral(force=False):
    now = time.time()
    if not force and account_cache.get("data") and account_cache.get("time", 0) > now - 30:
        return account_cache["data"]
    acc = _get_api_response(api.list_futures_accounts, SETTLE)
    equity = Decimal(str(getattr(acc, 'total', '0'))) if acc else Decimal("0")
    account_cache.update({"time": now, "data": equity})
    return equity

def get_price(symbol):
    ticker = _get_api_response(api.list_futures_tickers, SETTLE, contract=symbol)
    return Decimal(str(ticker.last)) if ticker and len(ticker) > 0 else Decimal("0")

# ======== 6. 파인스크립트 연동 및 수량계산 ========
def get_signal_type_multiplier(signal_type):
    if "premium" in signal_type: return Decimal("2.0")
    if "rescue" in signal_type:  return Decimal("3.0")
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
    except:
        return Decimal("0.25")

def get_ratio_by_index(idx):
    ratios = [Decimal("3.0"), Decimal("7.0"), Decimal("20.0"), Decimal("50.0"), Decimal("120.0")]
    return ratios[min(idx, len(ratios) - 1)]

def store_tp_sl(symbol, side, tp, sl, entry_number):
    with tpsl_lock:
        tpsl_storage.setdefault(symbol, {"long": {}, "short": {}})[side][entry_number] = {
            "tp": tp, "sl": sl, "entry_time": time.time()
        }

def is_duplicate(data):
    with signal_lock:
        now = time.time()
        symbol_id = f"{data.get('symbol')}_{data.get('side')}_{data.get('type')}"
        if now - recent_signals.get(symbol_id, 0) < COOLDOWN_SECONDS:
            log_debug("🔄 중복 신호 무시", f"{symbol_id}")
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
    if "rescue" in signal_type:
        with position_lock:
            side = "long" if "long" in signal_type else "short"
            last_ratio = position_state.get(symbol, {}).get(side, {}).get('last_entry_ratio', Decimal("5.0"))
            final_ratio = last_ratio * signal_multiplier
    contract_value = price * cfg["contract_size"]
    if contract_value <= 0:
        return Decimal("0"), final_ratio
    position_value = equity * final_ratio / 100
    base_qty = (position_value / contract_value).quantize(Decimal('1'), rounding=ROUND_DOWN)
    qty_with_min = max(base_qty, cfg["min_qty"])
    if qty_with_min * contract_value < cfg["min_notional"]:
        final_qty = (cfg["min_notional"] / contract_value).quantize(Decimal('1'), rounding=ROUND_DOWN) + Decimal("1")
    else:
        final_qty = qty_with_min
    return final_qty, final_ratio

# ======== 7. 주문 및 상태 관리 ========
def place_order(symbol, side, qty):
    with position_lock:
        try:
            update_all_position_states()
            original_size = position_state.get(symbol, {}).get(side, {}).get("size", Decimal("0"))
            order = FuturesOrder(contract=symbol, size=int(qty) if side == "long" else -int(qty), price="0", tif="ioc")
            result = _get_api_response(api.create_futures_order, SETTLE, order)
            if not result:
                log_debug("❌ 주문 API 호출 실패", f"{symbol}_{side}")
                return False
            log_debug("✅ 주문 전송", f"{symbol}_{side} id={getattr(result, 'id', 'N/A')}")
            for attempt in range(15):
                time.sleep(1)
                update_all_position_states()
                current_size = position_state.get(symbol, {}).get(side, {}).get("size", Decimal("0"))
                if current_size > original_size:
                    log_debug("🔄 포지션 업데이트 확인", f"{symbol}_{side} {attempt+1}s")
                    return True
            log_debug("❌ 포지션 업데이트 실패", f"{symbol}_{side} 15s 타임아웃")
            return False
        except Exception as e:
            log_debug("❌ 주문 오류", f"{symbol}_{side} {e}", exc_info=True)
            return False

def update_all_position_states():
    """주기 동기화 + 동적 심볼 추가/정리"""
    with position_lock:
        api_positions = _get_api_response(api.list_positions, settle=SETTLE)
        if api_positions is None:
            log_debug("❌ 포지션 동기화 실패", "API 응답 없음")
            return
        log_debug("📊 포지션 동기화 시작", f"FuturesApi count={len(api_positions)}")
        positions_to_clear = set()
        for symbol, sides in position_state.items():
            for side in ["long", "short"]:
                if sides[side]["size"] > 0:
                    positions_to_clear.add((symbol, side))

        # API 반영
        for pos in api_positions:
            pos_size = Decimal(str(pos.size))
            if pos_size == 0:
                continue
            symbol = normalize_symbol(pos.contract)
            if not symbol:
                continue
            if symbol not in position_state:
                log_debug("🆕 즉시 초기화", f"{symbol} - update 중 발견")
                position_state[symbol] = {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()}
                tpsl_storage[symbol] = {"long": {}, "short": {}}
                dynamic_symbols.add(symbol)

            cfg = get_symbol_config(symbol)
            side = None
            if hasattr(pos, 'mode') and pos.mode == 'dual_long': side = 'long'
            elif hasattr(pos, 'mode') and pos.mode == 'dual_short': side = 'short'
            elif pos_size > 0: side = 'long'
            elif pos_size < 0: side = 'short'
            if not side:
                continue

            positions_to_clear.discard((symbol, side))
            state = position_state[symbol][side]
            old_size = state.get("size", Decimal("0"))
            state.update({
                "price": Decimal(str(pos.entry_price)),
                "size": abs(pos_size),
                "value": abs(pos_size) * Decimal(str(pos.mark_price)) * cfg["contract_size"]
            })
            if old_size != abs(pos_size):
                log_debug("🔄 포지션 상태 업데이트", f"{symbol}_{side.upper()}: {old_size} → {abs(pos_size)}")

        # 청산/미보유 정리 + 동적 심볼 자동 제거
        cleared_symbols = set()
        for symbol, side in positions_to_clear:
            log_debug("🔄 수동/자동 청산 감지", f"{symbol}_{side.upper()} - API 응답에 없음")
            set_manual_close_protection(symbol, side)
            position_state[symbol][side] = get_default_pos_side_state()
            if symbol in tpsl_storage:
                tpsl_storage[symbol][side].clear()
            # 두 사이드 모두 size 0이면 동적 심볼 세트에서 제거 후보
            other_side = "short" if side == "long" else "long"
            if position_state[symbol][other_side]["size"] == 0 and position_state[symbol][side]["size"] == 0:
                cleared_symbols.add(symbol)

        # 동적 심볼 자동 정리
        if cleared_symbols:
            before = len(dynamic_symbols)
            dynamic_symbols.difference_update(cleared_symbols)
            after = len(dynamic_symbols)
            if before != after:
                log_debug("🧹 동적 심볼 정리", f"제거:{before-after}개 → 현재 {after}개")

# ======== 8. 진입 처리 ========
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
        return log_debug("⚠️ 첫 진입 취소: 슬리피지", "가격차 큼")
    entry_limits = {"premium": 5, "normal": 5, "rescue": 3}
    if state["entry_count"] >= sum(entry_limits.values()) or state[f"{base_type}_entry_count"] >= entry_limits.get(base_type, 99):
        return log_debug("⚠️ 추가 진입 제한", "최대 횟수 도달")
    current_signal_count = state[f"{base_type}_entry_count"] if "rescue" not in signal_type else 0
    qty, final_ratio = calculate_position_size(symbol, signal_type, entry_score, current_signal_count)
    if qty > 0 and place_order(symbol, side, qty):
        state["entry_count"] += 1
        state[f"{base_type}_entry_count"] += 1
        state["entry_time"] = time.time()
        if "rescue" not in signal_type: state['last_entry_ratio'] = final_ratio
        current_multiplier = state.get("premium_tp_multiplier", Decimal("1.0"))
        if "premium" in signal_type:
            if state["premium_entry_count"] == 1 and state["normal_entry_count"] == 0:
                new_multiplier = PREMIUM_TP_MULTIPLIERS["first_entry"]
            elif state["premium_entry_count"] == 1 and state["normal_entry_count"] > 0:
                new_multiplier = PREMIUM_TP_MULTIPLIERS["after_normal"]
            else:
                new_multiplier = PREMIUM_TP_MULTIPLIERS["after_premium"]
            state["premium_tp_multiplier"] = min(current_multiplier, new_multiplier) if current_multiplier != Decimal("1.0") else new_multiplier
            log_debug("✨ 프리미엄 TP 배수 적용", f"{symbol} {side.upper()} → {state['premium_tp_multiplier']:.2f}x")
        elif state["entry_count"] == 1:
            state["premium_tp_multiplier"] = Decimal("1.0")
        store_tp_sl(symbol, side, tv_tp_pct, tv_sl_pct, state["entry_count"])
        log_debug("💾 TP/SL 저장", f"TP:{tv_tp_pct*100:.3f}% SL:{tv_sl_pct*100:.3f}%")
        log_debug("✅ 진입 성공", f"{signal_type} qty={float(qty)}")

# ======== 9. 라우트 & 백그라운드 ========
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.get_data(as_text=True))
        log_debug("📬 웹훅 수신", f"{data}")
        if data.get("action") == "entry" and not is_duplicate(data):
            task_q.put_nowait(data)
        return "OK", 200
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e), exc_info=True)
        return "Error", 500

@app.route("/ping", methods=["GET"])
def ping(): 
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral(force=True)
        update_all_position_states()

        # 상태 로그
        with position_lock:
            total_positions = 0
            for symbol, sides in position_state.items():
                for side, pos_data in sides.items():
                    if pos_data["size"] > 0:
                        total_positions += 1
                        log_debug("📍 상태 확인", f"{symbol}_{side.upper()}: size={pos_data['size']}, price={pos_data['price']}")
        log_debug("📊 status 호출", f"전체 활성 포지션: {total_positions}개")

        # 응답 데이터
        active_positions = {}
        with position_lock:
            for symbol, sides in position_state.items():
                for side, pos_data in sides.items():
                    if pos_data["size"] != 0:
                        active_positions[f"{symbol}_{side.upper()}"] = {
                            "size": float(pos_data["size"]),
                            "price": float(pos_data["price"]),
                            "value": float(abs(pos_data["value"])),
                            "entry_count": pos_data["entry_count"],
                            "normal_count": pos_data["normal_entry_count"],
                            "premium_count": pos_data["premium_entry_count"],
                            "rescue_count": pos_data["rescue_entry_count"],
                            "last_ratio": float(pos_data['last_entry_ratio']),
                            "premium_tp_mult": float(pos_data.get("premium_tp_multiplier", 1.0)),
                            "current_tp_pct": f"{float(pos_data.get('current_tp_pct', 0.0)) * 100:.4f}%"
                        }

        # 동적/구독 관련 디버그 메타
        meta = {
            "tracked_symbols_total": len(position_state),
            "active_position_count": len(active_positions),
            "dynamic_symbols": sorted(list(dynamic_symbols)),
            "ws_last_payload_count": len(ws_last_payload),
            "ws_last_payload_sample": ws_last_payload[:10],
            "ws_last_subscribed_at": ws_last_subscribed_at
        }

        return jsonify({
            "status": "running",
            "version": "v6.33-server",
            "balance_usdt": float(equity),
            "active_positions": active_positions,
            "debug_info": meta
        })
    except Exception as e:
        log_debug("❌ status 오류", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

async def price_monitor():
    global ws_last_payload, ws_last_subscribed_at
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    last_resubscribe = time.time()

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                base_symbols = set(SYMBOL_CONFIG.keys())
                all_symbols = list(base_symbols | dynamic_symbols)

                # 구독 및 메트릭 기록
                payload = all_symbols
                ws_last_payload = payload[:]  # 복사 저장
                ws_last_subscribed_at = int(time.time())

                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": payload
                }))
                log_debug("🔌 웹소켓 구독", f"{len(payload)}개 심볼 (기본:{len(base_symbols)}, 동적:{len(dynamic_symbols)})")

                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    result = json.loads(msg).get("result")
                    if isinstance(result, list):
                        [simple_tp_monitor(i) for i in result]
                    elif isinstance(result, dict):
                        simple_tp_monitor(result)

                    # 60초마다 심볼 세트 변경 여부 확인 → 변경 시 재구독
                    if time.time() - last_resubscribe > 60:
                        current_symbols = list(set(SYMBOL_CONFIG.keys()) | dynamic_symbols)
                        if current_symbols != all_symbols:
                            await ws.send(json.dumps({
                                "time": int(time.time()),
                                "channel": "futures.tickers",
                                "event": "subscribe",
                                "payload": current_symbols
                            }))
                            ws_last_payload = current_symbols[:]
                            ws_last_subscribed_at = int(time.time())
                            log_debug("🔌 동적 재구독", f"변경:{len(current_symbols)-len(all_symbols)}개 (총 {len(current_symbols)})")
                            all_symbols = current_symbols
                        last_resubscribe = time.time()

        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
            log_debug("🔌 웹소켓 재연결", f"사유: {type(e).__name__}")
        except Exception as e:
            log_debug("🔌 웹소켓 오류", str(e), exc_info=True)
        await asyncio.sleep(5)

def simple_tp_monitor(ticker):
    """reduce_only=True로 청산 전용 실행"""
    try:
        symbol = normalize_symbol(ticker.get("contract"))
        price = Decimal(str(ticker.get("last", "0")))
        if not symbol or price <= 0: return
        cfg = get_symbol_config(symbol)
        if not cfg: return

        with position_lock:
            pos = position_state.get(symbol, {})

            # Long
            long_size = pos.get("long", {}).get("size", Decimal(0))
            if long_size > 0 and not is_manual_close_protected(symbol, "long"):
                long_state = pos["long"]
                entry_price = long_state.get("price")
                entry_count = long_state.get("entry_count", 0)
                if entry_price and entry_price > 0 and entry_count > 0:
                    premium_mult = long_state.get("premium_tp_multiplier", Decimal("1.0"))
                    sym_tp_mult = Decimal(str(cfg["tp_mult"]))
                    tp_map = [Decimal("0.0045"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
                    base_tp = tp_map[min(entry_count-1, len(tp_map)-1)] * sym_tp_mult * premium_mult
                    elapsed = time.time() - long_state.get("entry_time", time.time())
                    periods = max(0, int(elapsed / 15))
                    decay = Decimal("0.002") / 100
                    min_tp = Decimal("0.16") / 100
                    reduction = Decimal(str(periods)) * (decay * sym_tp_mult * premium_mult)
                    current_tp = max(min_tp * sym_tp_mult * premium_mult, base_tp - reduction)
                    long_state["current_tp_pct"] = current_tp
                    tp_price = entry_price * (1 + current_tp)
                    if price >= tp_price:
                        set_manual_close_protection(symbol, 'long', duration=20)
                        log_debug("🎯 롱 TP 실행", f"{symbol} entry:{entry_price:.8f} now:{price:.8f} tp:{tp_price:.8f}")
                        try:
                            order = FuturesOrder(contract=symbol, size=-int(long_size), price="0", tif="ioc", reduce_only=True)
                            result = _get_api_response(api.create_futures_order, SETTLE, order)
                            if result:
                                log_debug("✅ 롱 TP 청산 주문 성공", f"{symbol}")
                                position_state[symbol]['long'] = get_default_pos_side_state()
                                if symbol in tpsl_storage: tpsl_storage[symbol]['long'].clear()
                                log_debug("🔄 TP 실행 후 상태 초기화 완료", f"{symbol}_long")
                            else:
                                log_debug("❌ 롱 TP 청산 주문 실패", f"{symbol}")
                        except Exception as e:
                            log_debug("❌ 롱 TP 청산 오류", str(e), exc_info=True)

            # Short
            short_size = pos.get("short", {}).get("size", Decimal(0))
            if short_size > 0 and not is_manual_close_protected(symbol, "short"):
                short_state = pos["short"]
                entry_price = short_state.get("price")
                entry_count = short_state.get("entry_count", 0)
                if entry_price and entry_price > 0 and entry_count > 0:
                    premium_mult = short_state.get("premium_tp_multiplier", Decimal("1.0"))
                    sym_tp_mult = Decimal(str(cfg["tp_mult"]))
                    tp_map = [Decimal("0.005"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
                    base_tp = tp_map[min(entry_count-1, len(tp_map)-1)] * sym_tp_mult * premium_mult
                    elapsed = time.time() - short_state.get("entry_time", time.time())
                    periods = max(0, int(elapsed / 15))
                    decay = Decimal("0.002") / 100
                    min_tp = Decimal("0.16") / 100
                    reduction = Decimal(str(periods)) * (decay * sym_tp_mult * premium_mult)
                    current_tp = max(min_tp * sym_tp_mult * premium_mult, base_tp - reduction)
                    short_state["current_tp_pct"] = current_tp
                    tp_price = entry_price * (1 - current_tp)
                    if price <= tp_price:
                        set_manual_close_protection(symbol, 'short', duration=20)
                        log_debug("🎯 숏 TP 실행", f"{symbol} entry:{entry_price:.8f} now:{price:.8f} tp:{tp_price:.8f}")
                        try:
                            order = FuturesOrder(contract=symbol, size=int(short_size), price="0", tif="ioc", reduce_only=True)
                            result = _get_api_response(api.create_futures_order, SETTLE, order)
                            if result:
                                log_debug("✅ 숏 TP 청산 주문 성공", f"{symbol}")
                                position_state[symbol]['short'] = get_default_pos_side_state()
                                if symbol in tpsl_storage: tpsl_storage[symbol]['short'].clear()
                                log_debug("🔄 TP 실행 후 상태 초기화 완료", f"{symbol}_short")
                            else:
                                log_debug("❌ 숏 TP 청산 주문 실패", f"{symbol}")
                        except Exception as e:
                            log_debug("❌ 숏 TP 청산 오류", str(e), exc_info=True)

    except Exception as e:
        log_debug("❌ TP 모니터링 오류", str(e))

def worker(idx):
    while True:
        try:
            handle_entry(task_q.get(timeout=60))
        except queue.Empty:
            continue
        except Exception as e:
            log_debug(f"❌ 워커-{idx} 오류", str(e), exc_info=True)

def position_monitor():
    while True:
        time.sleep(30)
        try:
            update_all_position_states()
            log_debug("📊 포지션 현황", "주기적 상태 업데이트 완료")
        except Exception as e:
            log_debug("❌ 모니터링 오류", str(e), exc_info=True)

# ======== Main ========
if __name__ == "__main__":
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.33-server")
    log_debug("🛡️ 안전장치", f"웹훅 중복 방지 쿨다운: {COOLDOWN_SECONDS}초")
    initialize_states()
    log_debug("💰 초기 자산", f"{get_total_collateral(force=True):.2f} USDT")
    update_all_position_states()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"0.0.0.0:{port} 대기 중...")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
