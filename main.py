#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.12 - 독립적 피라미딩 시스템 (일반 5차 + 프리미엄 5차)
Pine Script v6.12+독립피라미딩과 완전 호환 - 3분 RSI 기반 수량 조절

주요 기능:
1. 독립적 피라미딩: 일반 신호 최대 5차 + 프리미엄 신호 최대 5차 = 총 10차
2. 3분 RSI 기반 수량 조절: 일반 70%, 프리미엄 140%
3. 손절직전 진입 (SL_Rescue) - 150% 가중치, 최대 3회
4. 점수 기반 진입 수량 조절 (0~100점 → 25%~100% 가중치)
5. 심볼별 가중치 적용 및 시간 감쇠 TP/SL
6. TradingView 웹훅 기반 자동 주문
7. 실시간 WebSocket을 통한 TP/SL 모니터링 및 자동 청산
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
from gate_api import exceptions as gate_api_exceptions
import queue
import pytz
import urllib.parse 

# ========================================
# 1. 로깅 설정
# ========================================

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

def log_debug(tag, msg, exc_info=False):
    """디버그 및 정보 로깅 유틸리티 함수"""
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception("")

# ========================================
# 2. Flask 앱 및 API 설정
# ========================================

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# ========================================
# 3. 상수 및 설정
# ========================================

COOLDOWN_SECONDS = 14
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
    "ONDO_USDT": "ONDO_USDT", "ONDOUSDT.P": "ONDO_USDT", "ONDOUSDTPERP": "ONDO_USDT", "ONDO_USDT": "ONDO_USDT", "ONDO": "ONDO_USDT",
}

SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"), "min_notional": Decimal("5"), "tp_mult": 0.55, "sl_mult": 0.55},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"), "min_notional": Decimal("5"), "tp_mult": 0.65, "sl_mult": 0.65},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 0.8, "sl_mult": 0.8},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0},
    "PEPE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000000"), "min_notional": Decimal("5"), "tp_mult": 1.2, "sl_mult": 1.2},
    "XRP_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0},
    "DOGE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.2, "sl_mult": 1.2},
    "ONDO_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0},
}

# ========================================
# 4. 전역 변수 및 동기화 객체
# ========================================

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
recent_signals = {}
signal_lock = threading.RLock()
tpsl_storage = {}
tpsl_lock = threading.RLock()
pyramid_tracking = {}
task_q = queue.Queue(maxsize=100)
WORKER_COUNT = min(6, max(2, os.cpu_count() * 2))

# ========================================
# 5. 핵심 유틸리티 함수
# ========================================

def _get_api_response(api_call, *args, **kwargs):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return api_call(*args, **kwargs)
        except Exception as e:
            if isinstance(e, gate_api_exceptions.ApiException):
                error_msg = f"API Error {e.status}: {e.reason}"
                if e.status == 501 and "USER_NOT_FOUND" in e.reason.upper():
                    log_debug("❌ 치명적 API 오류 (재시도 없음)", error_msg)
                    raise
            else:
                error_msg = str(e)
            if attempt < max_retries - 1:
                log_debug("⚠️ API 호출 재시도", f"시도 {attempt+1}/{max_retries}: {error_msg}, 잠시 후 재시도")
                time.sleep(2 ** attempt)
            else:
                log_debug("❌ API 호출 최종 실패", error_msg, exc_info=True)
    return None

def normalize_symbol(raw_symbol):
    symbol = str(raw_symbol).upper().strip()
    return SYMBOL_MAPPING.get(symbol) or SYMBOL_MAPPING.get(symbol.replace('.P', '').replace('PERP', ''))

def get_total_collateral(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 30 and account_cache["data"]:
        return account_cache["data"]
    equity = Decimal("0")
    acc = _get_api_response(api.list_futures_accounts, SETTLE)
    if acc:
        equity = Decimal(str(getattr(acc, 'available', '0')))
        log_debug("✅ 선물 계정 자산 조회", f"성공: {equity:.2f} USDT")
    else:
        log_debug("❌ 선물 계정 자산 조회 실패", "자산 정보를 가져올 수 없습니다.")
    account_cache.update({"time": now, "data": equity})
    return equity

def get_price(symbol):
    ticker = _get_api_response(api.list_futures_tickers, SETTLE, contract=symbol)
    if ticker and isinstance(ticker, list) and len(ticker) > 0:
        return Decimal(str(ticker[0].last))
    log_debug(f"⚠️ 가격 조회 실패 ({symbol})", "티커 데이터 없음 또는 API 오류")
    return Decimal("0")

# ========================================
# 6. 🔥 수정된 신호 타입 기반 수량 가중치 계산
# ========================================

def get_signal_type_multiplier(signal_type, entry_type):
    """
    Pine Script signal_type과 entry_type을 기반으로 수량 배수 계산
    
    프리미엄 신호 (3분 RSI 만족): 140% (70% × 2.0)
    일반 신호 (3분 RSI 불만족): 70% (70% × 1.0)
    """
    try:
        # Pine Script v6.12+독립피라미딩의 signal_type 확인
        if signal_type == "premium_3m_rsi":
            # 프리미엄 신호: 70%의 2배 = 140%
            return Decimal("1.4")
        elif signal_type in ["normal_signal", "main", "hybrid_enhanced", "backup_enhanced"]:
            # 일반 신호: 70%
            return Decimal("0.7")
        else:
            # 알 수 없는 신호 타입: 기본 70%
            log_debug("⚠️ 알 수 없는 신호 타입", f"signal_type: {signal_type}, entry_type: {entry_type}, 기본값 70% 적용")
            return Decimal("0.7")
    except Exception:
        log_debug("⚠️ 신호 타입 변환 오류", f"signal_type: {signal_type}, entry_type: {entry_type}, 기본값 70% 사용")
        return Decimal("0.7")

def get_entry_weight_from_score(score):
    """점수 기반 가중치 계산 (Pine Script와 동일)"""
    try:
        score = Decimal(str(score))
        if score <= 10:
            return Decimal("0.25")
        elif score <= 30:
            return Decimal("0.35")
        elif score <= 50:
            return Decimal("0.50")
        elif score <= 70:
            return Decimal("0.65")
        elif score <= 90:
            return Decimal("0.80")
        else:
            return Decimal("1.00")
    except (ValueError, TypeError, Exception):
        log_debug("⚠️ 점수 변환 오류", f"entry_score: {score}, 기본값 0.25 사용")
        return Decimal("0.25")

# ========================================
# 7. TP/SL 저장 및 관리
# ========================================

def store_tp_sl(symbol, tp, sl, entry_number):
    with tpsl_lock:
        tpsl_storage.setdefault(symbol, {})[entry_number] = {
            "tp": tp, "sl": sl, "entry_time": time.time()
        }

def get_tp_sl(symbol, entry_number=None):
    """Pine Script와 동기화된 TP/SL 값 반환"""
    with tpsl_lock:
        if symbol in tpsl_storage:
            if entry_number and entry_number in tpsl_storage[symbol]:
                val = tpsl_storage[symbol][entry_number]
                return val["tp"], val["sl"], val["entry_time"]
            elif tpsl_storage[symbol]:
                latest_entry = max(tpsl_storage[symbol].keys())
                val = tpsl_storage[symbol][latest_entry]
                return val["tp"], val["sl"], val["entry_time"]
    
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    base_tp = Decimal("0.005")  # 0.5%
    base_sl = Decimal("0.04")   # 4.0%
    
    return base_tp * Decimal(str(cfg["tp_mult"])), base_sl * Decimal(str(cfg["sl_mult"])), time.time()

# ========================================
# 8. 중복 신호 체크
# ========================================

def get_time_based_multiplier():
    now_hour = KST.localize(datetime.now()).hour
    return Decimal("1.0")

def is_duplicate(data):
    with signal_lock:
        now = time.time()
        symbol_id = f"{data.get('symbol', '')}_{data.get('side', '')}"
        signal_unique_id = data.get("id", "")

        if signal_unique_id and recent_signals.get(signal_unique_id) and (now - recent_signals[signal_unique_id]["last_processed_time"] < COOLDOWN_SECONDS):
            log_debug(f"🔄 중복 신호 무시 ({data.get('symbol', '')})", f"고유 ID '{signal_unique_id}' 쿨다운({COOLDOWN_SECONDS}초) 중.")
            return True

        if recent_signals.get(symbol_id) and (now - recent_signals[symbol_id]["last_processed_time"] < COOLDOWN_SECONDS):
            log_debug(f"🔄 중복 신호 무시 ({data.get('symbol', '')})", f"'{symbol_id}' 쿨다운({COOLDOWN_SECONDS}초) 중.")
            return True

        recent_signals[symbol_id] = {"last_processed_time": now}
        if signal_unique_id:
            recent_signals[signal_unique_id] = {"last_processed_time": now}

        recent_signals.pop(f"{data.get('symbol', '')}_{'short' if data.get('side') == 'long' else 'long'}", None)
        recent_signals.update({k: v for k, v in recent_signals.items() if now - v["last_processed_time"] < 300})
        return False

# ========================================
# 9. 🔥 수정된 수량 계산 (독립적 피라미딩 + 3분 RSI)
# ========================================

def calculate_position_size(symbol, signal_type, entry_multiplier=Decimal("1.0"), entry_score=50, current_signal_count=0):
    """
    독립적 피라미딩 시스템 수량 계산
    
    - 일반 신호: 최대 5차까지 독립 카운트
    - 프리미엄 신호: 최대 5차까지 독립 카운트
    - 신호 타입별 수량 조절: 일반 70%, 프리미엄 140%
    """
    cfg = SYMBOL_CONFIG[symbol]
    equity, price = get_total_collateral(), get_price(symbol)
    if equity <= 0 or price <= 0:
        log_debug(f"⚠️ 수량 계산 불가 ({symbol})", f"자산: {equity}, 가격: {price}")
        return Decimal("0")
    
    # 🔥 수정: 독립적 피라미딩 카운트 확인
    total_entry_count = position_state.get(symbol, {}).get("total_entry_count", 0)
    normal_entry_count = position_state.get(symbol, {}).get("normal_entry_count", 0)
    premium_entry_count = position_state.get(symbol, {}).get("premium_entry_count", 0)
    
    # 총 10차 제한 체크
    if total_entry_count >= 10:
        log_debug(f"⚠️ 최대 총 진입 도달 ({symbol})", f"총 진입 횟수: {total_entry_count}/10")
        return Decimal("0")
    
    # 신호 타입별 개별 제한 체크
    if signal_type == "premium_3m_rsi" and premium_entry_count >= 5:
        log_debug(f"⚠️ 프리미엄 최대 진입 도달 ({symbol})", f"프리미엄 진입: {premium_entry_count}/5")
        return Decimal("0")
    elif signal_type in ["normal_signal", "main"] and normal_entry_count >= 5:
        log_debug(f"⚠️ 일반 최대 진입 도달 ({symbol})", f"일반 진입: {normal_entry_count}/5")
        return Decimal("0")
    
    # 진입비율 배열 (Pine Script와 동일)
    entry_ratios = [Decimal("0.50"), Decimal("0.80"), Decimal("2.00"), Decimal("4.20"), Decimal("12.00")]
    current_ratio = entry_ratios[min(current_signal_count, len(entry_ratios) - 1)]
    
    # SL-Rescue 가중치 적용
    if signal_type == "sl_rescue":
        current_ratio = current_ratio * Decimal("1.5")
        log_debug(f"🚨 SL-Rescue 가중치 적용 ({symbol})", f"150% 증량")
    
    # 🔥 신호 타입별 수량 조절 적용
    signal_multiplier = get_signal_type_multiplier(signal_type, "")
    log_debug(f"🎯 신호 타입 수량 조절 ({symbol})", 
              f"signal_type: {signal_type} → 배수: {float(signal_multiplier*100)}%")
    
    # 점수 기반 가중치 적용
    score_weight = get_entry_weight_from_score(entry_score)
    log_debug(f"📊 점수 기반 가중치 적용 ({symbol})", 
              f"진입 점수: {entry_score}점 → 가중치: {float(score_weight*100)}%")
    
    # 최종 포지션 비율 = 기본 비율 × 신호 타입 배수 × 시간 가중치 × 점수 가중치
    final_position_ratio = current_ratio * signal_multiplier * entry_multiplier * score_weight
    
    position_value = equity * (final_position_ratio / Decimal("100"))
    contract_value = price * cfg["contract_size"]
    calculated_qty = (position_value / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
    final_qty = max(calculated_qty, cfg["min_qty"])
    
    # 최소 명목금액 보장
    current_notional = final_qty * price * cfg["contract_size"]
    if current_notional < cfg["min_notional"]:
        min_qty_for_notional = (cfg["min_notional"] / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
        final_qty = max(final_qty, min_qty_for_notional)
        log_debug(f"💡 최소 주문금액 조정 ({symbol})", f"조정된 수량: {final_qty}")
    
    log_debug(f"📊 독립피라미딩 수량 계산 ({symbol})", 
              f"총진입: #{total_entry_count+1}/10, {signal_type}진입: #{current_signal_count+1}/5, "
              f"기본비율: {float(current_ratio)}%, 신호배수: {float(signal_multiplier*100)}%, "
              f"점수: {entry_score}점({float(score_weight*100)}%), 최종비율: {float(final_position_ratio)}%, "
              f"수량: {final_qty}")
    
    return final_qty

# ========================================
# 10. 🔥 수정된 포지션 상태 관리 (독립 카운트 포함)
# ========================================

def update_position_state(symbol):
    with position_lock:
        pos_info = _get_api_response(api.get_position, SETTLE, symbol)
        size = Decimal("0")
        if pos_info and pos_info.size:
            try:
                size = Decimal(str(pos_info.size))
            except Exception:
                size = Decimal("0")
        
        if size != 0:
            existing = position_state.get(symbol, {})
            position_state[symbol] = {
                "price": Decimal(str(pos_info.entry_price)),
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "value": abs(size) * Decimal(str(pos_info.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"],
                # 기존 카운트 유지
                "entry_count": existing.get("entry_count", 0),  # 호환성 유지
                "total_entry_count": existing.get("total_entry_count", 0),  # 🔥 추가: 총 진입 횟수
                "normal_entry_count": existing.get("normal_entry_count", 0),  # 🔥 추가: 일반 신호 진입 횟수
                "premium_entry_count": existing.get("premium_entry_count", 0),  # 🔥 추가: 프리미엄 신호 진입 횟수
                "entry_time": existing.get("entry_time", time.time()),
                "sl_entry_count": existing.get("sl_entry_count", 0),
                'time_multiplier': existing.get('time_multiplier', Decimal("1.0"))
            }
            return False
        else:
            # 🔥 수정: 모든 카운트 리셋
            position_state[symbol] = {
                "price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"),
                "entry_count": 0, "total_entry_count": 0,  # 🔥 추가
                "normal_entry_count": 0, "premium_entry_count": 0,  # 🔥 추가
                "entry_time": None, "sl_entry_count": 0, 
                'time_multiplier': Decimal("1.0")
            }
            pyramid_tracking.pop(symbol, None)
            tpsl_storage.pop(symbol, None)
            return True

# ========================================
# 11. SL-Rescue 조건 확인 (기존과 동일)
# ========================================

def is_sl_rescue_condition(symbol):
    with position_lock:
        pos = position_state.get(symbol)
        if not pos or pos["size"] == 0 or pos["total_entry_count"] >= 10 or pos["sl_entry_count"] >= 3:  # 🔥 수정: 총 10차 제한
            return False
        current_price, avg_price, side = get_price(symbol), pos["price"], pos["side"]
        if current_price <= 0: return False
        original_tp, original_sl, entry_start_time = get_tp_sl(symbol, pos["total_entry_count"])  # 🔥 수정
        symbol_weight_sl = Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
        sl_price = avg_price * (1 - original_sl * symbol_weight_sl) if side == "buy" else avg_price * (1 + original_sl * symbol_weight_sl)
        
        if (side == "buy" and current_price <= sl_price) or (side == "sell" and current_price >= sl_price):
            return True
        return False

# ========================================
# 12. 🔥 수정된 주문 실행 (독립 카운트 관리)
# ========================================

def place_order(symbol, side, qty, signal_type, time_multiplier):
    with position_lock:
        cfg = SYMBOL_CONFIG[symbol]
        qty_dec = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN)
        if qty_dec < cfg["min_qty"]:
            qty_dec = cfg["min_qty"]
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order_value_estimate = qty_dec * get_price(symbol) * cfg["contract_size"]
        if order_value_estimate > get_total_collateral() * Decimal("10"):
            log_debug(f"⚠️ 과도한 주문 방지 ({symbol})", f"명목 가치: {order_value_estimate:.2f} USDT. 주문 취소.")
            return False
        
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=False)
        if not _get_api_response(api.create_futures_order, SETTLE, order): 
            return False
        
        # 🔥 카운트 업데이트 (독립적)
        pos = position_state.setdefault(symbol, {})
        
        # 총 진입 횟수 증가
        total_count = pos.get("total_entry_count", 0) + 1
        pos["total_entry_count"] = total_count
        pos["entry_count"] = total_count  # 호환성 유지
        
        # 신호 타입별 카운트 증가
        if signal_type == "premium_3m_rsi":
            pos["premium_entry_count"] = pos.get("premium_entry_count", 0) + 1
        elif signal_type in ["normal_signal", "main"]:
            pos["normal_entry_count"] = pos.get("normal_entry_count", 0) + 1
        
        pos["entry_time"] = time.time()
        if total_count == 1:
            pos['time_multiplier'] = time_multiplier
        
        log_debug(f"✅ 독립피라미딩 주문 성공 ({symbol})", 
                  f"{side.upper()} {float(qty_dec)} 계약 ({signal_type}, "
                  f"총: #{total_count}/10, 일반: {pos.get('normal_entry_count', 0)}/5, "
                  f"프리미엄: {pos.get('premium_entry_count', 0)}/5)")
        
        time.sleep(2)
        update_position_state(symbol)
        return True

def close_position(symbol, reason="manual"):
    with position_lock:
        if not _get_api_response(api.create_futures_order, SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)):
            return False
        
        log_debug(f"✅ 청산 완료 ({symbol})", f"이유: {reason}")
        
        # 🔥 수정: 모든 카운트 리셋
        position_state[symbol] = {
            "price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"),
            "entry_count": 0, "total_entry_count": 0,
            "normal_entry_count": 0, "premium_entry_count": 0,
            "entry_time": None, "sl_entry_count": 0, 
            'time_multiplier': Decimal("1.0")
        }
        
        pyramid_tracking.pop(symbol, None)
        tpsl_storage.pop(symbol, None)
        
        with signal_lock:
            keys_to_remove = [k for k in recent_signals.keys() if k.startswith(symbol + "_") or k.startswith(symbol)]
            for k in keys_to_remove:
                recent_signals.pop(k)
        
        time.sleep(1)
        update_position_state(symbol)
        return True

# ========================================
# 13. 🔥 수정된 Flask 웹훅 라우트 (독립 피라미딩 정보 처리)
# ========================================

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    with signal_lock: recent_signals.clear()
    with tpsl_lock: tpsl_storage.clear()
    pyramid_tracking.clear()
    log_debug("🔄 캐시 초기화", "모든 신호 및 TP/SL 캐시가 초기화되었습니다.")
    return jsonify({"status": "cache_cleared"})

@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info(f"[WEBHOOK_RECEIVED] 데이터 수신: {request.data}")
    try:
        raw_data = request.get_data(as_text=True)
        if not raw_data:
            return jsonify({"error": "Empty data"}), 400

        # JSON 파싱
        data = None
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            if request.form:
                data = request.form.to_dict()
            elif "&" not in raw_data and "=" not in raw_data:
                try:
                    data = json.loads(urllib.parse.unquote(raw_data))
                except Exception:
                    pass

        if not data:
            return jsonify({"error": "Failed to parse data"}), 400

        # 🔥 수정된 데이터 파싱 (독립 피라미딩 정보 추가)
        symbol_raw = data.get("symbol", "")
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        signal_type = data.get("signal", "normal_signal")  # Pine Script signal_type
        entry_type = data.get("type", "")  # entry_type
        entry_score = data.get("entry_score", 50)
        
        # 🔥 추가: 독립 피라미딩 카운트 정보
        total_entries = data.get("total_entries", 0)
        normal_entries = data.get("normal_entries", 0)
        premium_entries = data.get("premium_entries", 0)
        rsi_3m = data.get("rsi_3m", 0)  # 3분 RSI 값
        rsi_15s = data.get("rsi_15s", 0)  # 15초 RSI 값
        qty_multiplier = data.get("qty_multiplier", "70%")  # 수량 배수 정보
        
        log_debug(
            "📊 독립피라미딩 웹훅 데이터 (v6.12)",
            f"심볼: {symbol_raw}, 방향: {side}, 액션: {action}, "
            f"signal_type: {signal_type}, entry_type: {entry_type}, "
            f"총진입: {total_entries}, 일반: {normal_entries}/5, 프리미엄: {premium_entries}/5, "
            f"3분RSI: {rsi_3m}, 15초RSI: {rsi_15s}, 수량배수: {qty_multiplier}, "
            f"점수: {entry_score}점"
        )

        symbol = normalize_symbol(symbol_raw)
        if not symbol or symbol not in SYMBOL_CONFIG:
            log_debug("❌ 유효하지 않은 심볼", f"원본: {symbol_raw}")
            return jsonify({"error": f"Invalid symbol: {symbol_raw}"}), 400

        # 중복 신호 체크
        if is_duplicate(data):
            return jsonify({"status": "duplicate_ignored"}), 200

        # ===== Entry 처리 =====
        if action == "entry" and side in ["long", "short"]:
            try:
                # 🔥 Pine Script v6.12+독립피라미딩 데이터 추가
                data["signal_type"] = signal_type
                data["entry_type"] = entry_type
                data["total_entries"] = total_entries
                data["normal_entries"] = normal_entries
                data["premium_entries"] = premium_entries
                data["rsi_3m"] = rsi_3m
                data["rsi_15s"] = rsi_15s
                task_q.put_nowait(data)
            except queue.Full:
                return jsonify({"status": "queue_full"}), 429
            return jsonify({
                "status": "queued",
                "symbol": symbol,
                "side": side,
                "signal_type": signal_type,
                "total_entries": total_entries,
                "normal_entries": normal_entries,
                "premium_entries": premium_entries,
                "qty_multiplier": qty_multiplier,
                "queue_size": task_q.qsize()
            }), 200

        # ===== Exit 처리 (기존과 동일) =====
        elif action == "exit":
            reason = data.get("reason", "").upper()

            if reason in ("TP", "SL"):
                log_debug(f"TP/SL 청산 알림 무시 ({symbol})", f"이유: {reason}")
                return jsonify({"status": "ignored", "reason": "tp_sl_handled_by_server"}), 200

            update_position_state(symbol)
            pos = position_state.get(symbol, {})
            pos_side = pos.get("side")

            if pos_side:
                log_debug(f"✅ 청산 신호 수신 ({symbol})", f"이유: {reason}. 포지션 청산 시도.")
                close_position(symbol, reason)
            else:
                log_debug(f"💡 청산 불필요 ({symbol})", "활성 포지션 없음")

            return jsonify({"status": "success", "action": "exit"})

        log_debug("❌ 알 수 없는 웹훅 액션", f"수신된 액션: {action}")
        return jsonify({"error": "Invalid action"}), 400

    except Exception as e:
        log_debug("❌ 웹훅 처리 중 예외 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ========================================
# 14. 🔥 수정된 상태 API (독립 피라미딩 정보 추가)
# ========================================

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
            pos = position_state.get(sym, {})
            if pos.get("side"):
                total_entry_count = pos.get("total_entry_count", 0)
                normal_entry_count = pos.get("normal_entry_count", 0)
                premium_entry_count = pos.get("premium_entry_count", 0)
                
                tp_sl_info = []
                for i in range(1, total_entry_count + 1):
                    tp, sl, entry_start_time = get_tp_sl(sym, i)
                    tp_sl_info.append({
                        "entry": i,
                        "tp_pct": float(tp) * 100,
                        "sl_pct": float(sl) * 100,
                        "entry_time_kst": datetime.fromtimestamp(entry_start_time, KST).strftime('%Y-%m-%d %H:%M:%S'),
                        "elapsed_seconds": int(time.time() - entry_start_time)
                    })
                
                positions[sym] = {
                    "side": pos["side"],
                    "size": float(pos["size"]),
                    "price": float(pos["price"]),
                    "value": float(pos["value"]),
                    # 🔥 독립 피라미딩 정보 추가
                    "total_entry_count": total_entry_count,
                    "normal_entry_count": normal_entry_count,
                    "premium_entry_count": premium_entry_count,
                    "entry_count": total_entry_count,  # 호환성 유지
                    "sl_rescue_count": pos.get("sl_entry_count", 0),
                    "symbol_multiplier": SYMBOL_CONFIG[sym]["tp_mult"],
                    "time_multiplier": float(pos.get('time_multiplier', Decimal("1.0"))),
                    "tp_sl_info": tp_sl_info,
                    "pyramid_tracking": pyramid_tracking.get(sym, {"signal_count": 0, "last_entered": False})
                }
        
        return jsonify({
            "status": "running",
            "version": "v6.12_independent_pyramiding_3m_rsi",
            "current_time_kst": datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            "balance_usdt": float(equity),
            "active_positions": positions,
            "cooldown_seconds": COOLDOWN_SECONDS,
            # 🔥 독립 피라미딩 시스템 정보
            "independent_pyramiding": {
                "max_total_entries": 10,
                "max_normal_entries": 5,
                "max_premium_entries": 5,
                "normal_signal_multiplier": "70%",
                "premium_signal_multiplier": "140% (70% × 2배)"
            },
            "max_sl_rescue_per_position": 3,
            "pyramiding_entry_ratios": [0.5, 0.8, 2.0, 4.2, 12.0],
            # 🔥 v6.12 전용 정보 추가
            "signal_type_multipliers": {
                "premium_3m_rsi": "140% (70% × 2배)",
                "normal_signal": "70%",
                "main": "70%",
                "hybrid_enhanced": "70%",
                "backup_enhanced": "70%"
            },
            "score_based_weights": {
                "0-10": "25%", "11-30": "35%", "31-50": "50%", 
                "51-70": "65%", "71-90": "80%", "91-100": "100%"
            },
            "symbol_weights": {sym: {"tp_mult": cfg["tp_mult"], "sl_mult": cfg["sl_mult"]} for sym, cfg in SYMBOL_CONFIG.items()},
            "queue_info": {"size": task_q.qsize(), "max_size": task_q.maxsize}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 중 오류 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ========================================
# 15. WebSocket 모니터링 (기존과 동일)
# ========================================

async def price_monitor():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols_to_subscribe = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"time": int(time.time()), "channel": "futures.tickers", "event": "subscribe", "payload": symbols_to_subscribe}))
                log_debug("📡 웹소켓", f"구독 완료: {symbols_to_subscribe}")
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    data = json.loads(msg)
                    if data.get("event") in ["error", "subscribe"]: continue
                    result = data.get("result")
                    if isinstance(result, list):
                        for item in result:
                            check_tp_sl(item)
                    elif isinstance(result, dict):
                        check_tp_sl(result)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
            log_debug("🔌 웹소켓 연결 문제", f"재연결 시도... ({type(e).__name__})")
        except Exception as e:
            log_debug("❌ 웹소켓 오류", str(e), exc_info=True)
        await asyncio.sleep(5)

def check_tp_sl(ticker):
    """Pine Script avg_price와 동기화된 TP/SL 체크"""
    try:
        symbol, price = ticker.get("contract"), Decimal(str(ticker.get("last", "0")))
        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0: 
            return
            
        with position_lock:
            pos = position_state.get(symbol, {})
            side, total_entry_count = pos.get("side"), pos.get("total_entry_count", 0)  # 🔥 수정
            
            if not side or total_entry_count == 0:
                return
            
            pine_avg_price = pos.get("pine_avg_price")
            entry_price = pine_avg_price if pine_avg_price else pos.get("price")
            
            if not entry_price:
                return
                
            symbol_weight_tp = Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
            symbol_weight_sl = Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
            
            original_tp, original_sl, entry_start_time = get_tp_sl(symbol, total_entry_count)  # 🔥 수정
            
            # 시간 감쇠 계산
            time_elapsed = time.time() - entry_start_time
            periods_15s = int(time_elapsed / 15)
            
            tp_decay_amt_ps = Decimal("0.002") / 100
            tp_min_pct_ps = Decimal("0.12") / 100
            tp_reduction = Decimal(str(periods_15s)) * (tp_decay_amt_ps * symbol_weight_tp)
            adjusted_tp = max(tp_min_pct_ps * symbol_weight_tp, original_tp - tp_reduction)
            
            sl_decay_amt_ps = Decimal("0.004") / 100
            sl_min_pct_ps = Decimal("0.09") / 100
            sl_reduction = Decimal(str(periods_15s)) * (sl_decay_amt_ps * symbol_weight_sl)
            adjusted_sl = max(sl_min_pct_ps * symbol_weight_sl, original_sl - sl_reduction)
            
            # 평단가 기준 TP/SL 계산
            tp_price = entry_price * (1 + adjusted_tp) if side == "buy" else entry_price * (1 - adjusted_tp)
            sl_price = entry_price * (1 - adjusted_sl) if side == "buy" else entry_price * (1 + adjusted_sl)
            
            tp_triggered = (price >= tp_price if side == "buy" else price <= tp_price)
            sl_triggered = (price <= sl_price if side == "buy" else price >= sl_price)
            
            if tp_triggered:
                log_debug(f"🎯 TP 트리거 ({symbol})", 
                         f"평단가: {entry_price:.8f}, 현재가: {price:.8f}, TP가: {tp_price:.8f}")
                close_position(symbol, "TP")
            elif sl_triggered:
                log_debug(f"🛑 SL 트리거 ({symbol})", 
                         f"평단가: {entry_price:.8f}, 현재가: {price:.8f}, SL가: {sl_price:.8f}")
                close_position(symbol, "SL")
                
    except Exception as e:
        log_debug(f"❌ TP/SL 체크 오류 ({ticker.get('contract', 'Unknown')})", str(e), exc_info=True)

# ========================================
# 16. 🔥 수정된 워커 스레드 및 진입 처리 (독립 피라미딩)
# ========================================

def worker(idx):
    log_debug(f"⚙️ 워커-{idx} 시작", f"워커 스레드 {idx} 시작됨")
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
            time.sleep(1)

def handle_entry(data):
    symbol_raw = data.get("symbol", "")
    side = data.get("side", "").lower()
    signal_type = data.get("signal", "normal_signal")
    entry_type = data.get("type", "")
    entry_score = data.get("entry_score", 50)
    
    # 🔥 추가: 독립 피라미딩 정보
    total_entries = data.get("total_entries", 0)
    normal_entries = data.get("normal_entries", 0)
    premium_entries = data.get("premium_entries", 0)
    rsi_3m = data.get("rsi_3m", 0)
    rsi_15s = data.get("rsi_15s", 0)
    
    log_debug("📊 독립피라미딩 진입 처리 (v6.12)", 
              f"심볼: {symbol_raw}, 방향: {side}, signal_type: {signal_type}, "
              f"총진입: {total_entries}, 일반: {normal_entries}, 프리미엄: {premium_entries}, "
              f"3분RSI: {rsi_3m}, 15초RSI: {rsi_15s}, 점수: {entry_score}점")

    symbol = normalize_symbol(symbol_raw)
    if not symbol or symbol not in SYMBOL_CONFIG:
        return

    # 🔥 신호 타입별 로깅
    if signal_type == "premium_3m_rsi":
        log_debug(f"🌟 프리미엄 신호 수신 ({symbol})", f"3분 RSI 조건 만족 → 140% 수량 적용")
    else:
        log_debug(f"📊 일반 신호 수신 ({symbol})", f"3분 RSI 조건 미충족 → 70% 수량 적용")

    update_position_state(symbol)
    total_entry_count = position_state.get(symbol, {}).get("total_entry_count", 0)  # 🔥 수정
    normal_entry_count = position_state.get(symbol, {}).get("normal_entry_count", 0)
    premium_entry_count = position_state.get(symbol, {}).get("premium_entry_count", 0)
    current_pos_side = position_state.get(symbol, {}).get("side")
    desired_side = "buy" if side == "long" else "sell"

    # Pine Script avg_price 정보 저장
    pine_avg_price = data.get("avg_price")
    if pine_avg_price and pine_avg_price > 0:
        position_state.setdefault(symbol, {})["pine_avg_price"] = Decimal(str(pine_avg_price))

    # 반대 포지션 처리
    if current_pos_side and current_pos_side != desired_side:
        if not close_position(symbol, "reverse_entry"):
            return
        time.sleep(1)
        update_position_state(symbol)
        total_entry_count = 0
        normal_entry_count = 0
        premium_entry_count = 0

    # 🔥 독립적 진입 제한 체크
    if total_entry_count >= 10:
        log_debug(f"⚠️ 총 최대 진입 도달 ({symbol})", f"총 {total_entry_count}/10")
        return

    if signal_type == "premium_3m_rsi" and premium_entry_count >= 5:
        log_debug(f"⚠️ 프리미엄 최대 진입 도달 ({symbol})", f"프리미엄 {premium_entry_count}/5")
        return
    elif signal_type in ["normal_signal", "main"] and normal_entry_count >= 5:
        log_debug(f"⚠️ 일반 최대 진입 도달 ({symbol})", f"일반 {normal_entry_count}/5")
        return

    # SL-Rescue 처리 (기존과 동일)
    is_sl_rescue_signal = (signal_type == "sl_rescue")
    if is_sl_rescue_signal:
        sl_entry_count = position_state.get(symbol, {}).get("sl_entry_count", 0)
        if sl_entry_count >= 3:
            return
        if not is_sl_rescue_condition(symbol):
            return
        position_state[symbol]["sl_entry_count"] = sl_entry_count + 1
    else:
        if total_entry_count > 0:  # 🔥 수정
            current_price = get_price(symbol)
            avg_price = position_state[symbol].get("pine_avg_price") or position_state[symbol]["price"]
            if (current_pos_side == "buy" and current_price >= avg_price) or (current_pos_side == "sell" and current_price <= avg_price):
                log_debug(f"⏭️ 가격조건 미충족 ({symbol})", f"현재가: {current_price:.8f}, 평단가: {avg_price:.8f}")
                return

    # Pine Script TP/SL 값 저장
    pine_tp = data.get("tp_pct", 0.5) / 100
    pine_sl = data.get("sl_pct", 4.0) / 100
    
    if pine_tp > 0 and pine_sl > 0:
        store_tp_sl(symbol, Decimal(str(pine_tp)), Decimal(str(pine_sl)), total_entry_count + 1)

    # 🔥 신호 타입에 따른 현재 카운트 결정
    if signal_type == "premium_3m_rsi":
        current_signal_count = premium_entry_count
    else:
        current_signal_count = normal_entry_count

    # 🔥 수정된 수량 계산 (독립 피라미딩)
    qty = calculate_position_size(symbol, signal_type, get_time_based_multiplier(), entry_score, current_signal_count)
    if qty <= 0:
        return
        
    if place_order(symbol, desired_side, qty, signal_type, get_time_based_multiplier()):
        multiplier_info = "140%" if signal_type == "premium_3m_rsi" else "70%"
        new_total = total_entry_count + 1
        log_debug(f"✅ 독립피라미딩 진입 성공 ({symbol})", 
                  f"{desired_side.upper()} {float(qty)} 계약 (총 #{new_total}/10, "
                  f"신호: {signal_type}, 수량배수: {multiplier_info}, 점수: {entry_score}점)")
    else:
        log_debug(f"❌ 진입 실패 ({symbol})", f"{desired_side.upper()}")

def position_monitor():
    """포지션 모니터링 (독립 피라미딩 정보 포함)"""
    while True:
        time.sleep(30)
        try:
            total_value = Decimal("0")
            active_positions_log = []
            
            for symbol in SYMBOL_CONFIG:
                update_position_state(symbol)
                pos = position_state.get(symbol, {})
                
                if pos.get("side"):
                    total_value += pos["value"]
                    # 🔥 독립 피라미딩 정보 추가
                    pyramid_info = (f"총: {pos.get('total_entry_count', 0)}/10, "
                                   f"일반: {pos.get('normal_entry_count', 0)}/5, "
                                   f"프리미엄: {pos.get('premium_entry_count', 0)}/5, "
                                   f"SL-Rescue: {pos.get('sl_entry_count', 0)}/3")
                    active_positions_log.append(
                        f"{symbol}: {pos['side']} {pos['size']:.4f} @ "
                        f"{pos.get('pine_avg_price', pos['price']):.8f} "
                        f"({pyramid_info}, 명목가치: {pos['value']:.2f} USDT)"
                    )
            
            if active_positions_log:
                equity = get_total_collateral()
                exposure_pct = (total_value / equity * 100) if equity > 0 else 0
                log_debug("📊 독립피라미딩 포지션 현황 (v6.12)", 
                         f"활성 포지션: {len(active_positions_log)}개, "
                         f"총 명목가치: {total_value:.2f} USDT, "
                         f"총자산: {equity:.2f} USDT, 노출도: {exposure_pct:.1f}%")
                for pos_info in active_positions_log:
                    log_debug("  └", pos_info)
            else:
                log_debug("📊 포지션 현황 보고", "현재 활성 포지션이 없습니다.")
                
        except Exception as e:
            log_debug("❌ 포지션 모니터링 오류", str(e), exc_info=True)

# ========================================
# 17. 메인 실행
# ========================================

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.12 (독립적 피라미딩 + 3분 RSI 시스템)")
    log_debug("📊 현재 설정", f"감시 심볼: {len(SYMBOL_CONFIG)}개, 쿨다운: {COOLDOWN_SECONDS}초")
    log_debug("🔥 독립 피라미딩", "일반 신호: 최대 5차 (70% 수량) + 프리미엄 신호: 최대 5차 (140% 수량) = 총 10차")
    log_debug("🎯 수량 조절", "프리미엄: 140% (3분 RSI 만족), 일반: 70% (3분 RSI 불만족)")
    
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산 확인", f"{equity:.2f} USDT" if equity > 0 else "자산 조회 실패")
    
    # 초기 포지션 확인 (독립 카운트 포함)
    initial_active_positions = []
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
        pos = position_state.get(symbol, {})
        if pos.get("side"):
            initial_active_positions.append(
                f"{symbol}: {pos['side']} {pos['size']:.4f} @ {pos['price']:.8f} "
                f"(총: {pos.get('total_entry_count', 0)}/10, "
                f"일반: {pos.get('normal_entry_count', 0)}/5, "
                f"프리미엄: {pos.get('premium_entry_count', 0)}/5)"
            )
    
    log_debug("📊 초기 활성 포지션", 
              f"{len(initial_active_positions)}개 감지" if initial_active_positions else "감지 안됨")
    for pos_info in initial_active_positions:
        log_debug("  └", pos_info)
    
    # 백그라운드 스레드 시작
    for target_func, name in [(position_monitor, "IndependentPyramidingMonitor"), 
                              (lambda: asyncio.run(price_monitor()), "PriceMonitor")]:
        threading.Thread(target=target_func, daemon=True, name=name).start()
    
    # 워커 스레드 시작
    log_debug("⚙️ 워커 스레드", f"{WORKER_COUNT}개 시작 중")
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True, name=f"Worker-{i}").start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"Flask 서버 0.0.0.0:{port}에서 실행 중")
    log_debug("✅ 준비 완료", "Pine Script v6.12 독립적 피라미딩 + 3분 RSI 시스템 대기중")
    
    app.run(host="0.0.0.0", port=port, debug=False)
