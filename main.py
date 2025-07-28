#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.12 - 모든 기능 유지, 코드 정리 및 최적화, API 재시도 로직 추가
이전처럼 통합 계정 조회 오류(E501 등) 시 즉시 선물 계정 자산으로 폴백하도록 수정
ImportError: cannot import name 'gate_api_exceptions' 해결

주요 기능:
1. 5단계 피라미딩 (20%→40%→120%→480%→960%)
2. 손절직전 진입 (SL_Rescue) - 150% 가중치, 최대 3회, 0.05% 임계값
3. 최소 수량 및 최소 명목 금액 보장
4. 심볼별 가중치 적용 및 시간 감쇠 TP/SL
5. 야간 시간 진입 수량 조절 (0.5배)
6. TradingView 웹훅 기반 자동 주문
7. 실시간 WebSocket을 통한 TP/SL 모니터링 및 자동 청산
8. API 호출 시 일시적 오류에 대한 재시도 로직
9. 통합 계정 조회 실패 시 즉시 선물 계정 자산으로 폴백 (이전 동작 방식 재현)
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
# 🔧 수정: gate_api_exceptions 임포트 오류 해결
from gate_api import exceptions as gate_api_exceptions # gate_api_exceptions를 gate_api.exceptions에서 임포트
import queue
import pytz
import urllib.parse 

# ========================================
# 1. 로깅 설정
# ========================================

# INFO 레벨 로그만 출력하며, werkzeug (Flask 내부) 로그는 ERROR만 출력
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
        logger.exception("") # 예외 정보와 함께 스택 트레이스 출력

# ========================================
# 2. Flask 앱 및 API 설정
# ========================================

app = Flask(__name__)

# Gate.io API 인증 정보 (환경 변수에서 로드)
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt" # 선물 계정의 정산 통화

# Gate.io API 클라이언트 인스턴스 생성
config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)      # 선물 거래 API
unified_api = UnifiedApi(client) # 통합 계정 API (자산 조회용)

# ========================================
# 3. 상수 및 설정
# ========================================

COOLDOWN_SECONDS = 10 # 서버측 신호 쿨다운 (파인스크립트와 연동)
KST = pytz.timezone('Asia/Seoul') # 한국 시간대 설정

# 심볼 매핑: TradingView/Gate.io에서 사용하는 다양한 심볼 명칭을 내부 표준 심볼명으로 통일
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

# 심볼별 계약 사양 및 TP/SL 가중치 설정 (Gate.io 실제 규격에 맞춰 조정 필요)
SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"), "min_notional": Decimal("5"), "tp_mult": 0.5, "sl_mult": 0.5},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"), "min_notional": Decimal("5"), "tp_mult": 0.6, "sl_mult": 0.6},
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

position_state = {}     # 각 심볼의 현재 포지션 정보 (진입가, 사이즈, 방향, 진입 횟수, SL-Rescue 횟수 등)
position_lock = threading.RLock() # 포지션 상태 변경 시 스레드 동시성 제어 락
account_cache = {"time": 0, "data": None} # 자산 정보를 캐싱하여 API 호출 빈도 줄임
recent_signals = {}     # 중복 신호 방지 및 쿨다운 관리
signal_lock = threading.RLock() # 신호 관련 데이터 변경 시 스레드 동시성 제어 락
tpsl_storage = {}       # PineScript에서 전달받은 단계별 TP/SL 정보 저장
tpsl_lock = threading.RLock()   # TP/SL 정보 변경 시 스레드 동시성 제어 락
pyramid_tracking = {}   # 일반 피라미딩 스킵 로직을 위한 추적 정보
task_q = queue.Queue(maxsize=100) # 웹훅 요청을 비동기 워커 스레드로 전달하기 위한 큐
WORKER_COUNT = min(6, max(2, os.cpu_count() * 2)) # 병렬 처리를 위한 워커 스레드 수 (CPU 코어 수 기반)

# ========================================
# 5. 핵심 유틸리티 함수 (API 재시도 로직 추가)
# ========================================

def _get_api_response(api_call, *args, **kwargs):
    """API 호출을 캡슐화하고 예외 처리 및 재시도 로직을 포함합니다."""
    max_retries = 3 # 최대 재시도 횟수
    for attempt in range(max_retries):
        try:
            return api_call(*args, **kwargs)
        except Exception as e:
            # gate_api_exceptions.ApiException인 경우 status와 reason을 확인
            if isinstance(e, gate_api_exceptions.ApiException):
                error_msg = f"API Error {e.status}: {e.reason}"
                # 특정 에러 (예: E501 USER_NOT_FOUND)는 재시도 없이 바로 예외 발생
                if e.status == 501 and "USER_NOT_FOUND" in e.reason.upper():
                    log_debug("❌ 치명적 API 오류 (재시도 안함)", error_msg)
                    raise # 통합 계정 조회 시 발생하는 E501은 재시도하지 않고 바로 상위로 전파
            else:
                error_msg = str(e)

            if attempt < max_retries - 1:
                log_debug("⚠️ API 호출 재시도", f"시도 {attempt + 1}/{max_retries}: {error_msg}. 잠시 후 재시도합니다.")
                time.sleep(2 ** attempt) # 지수 백오프 (1, 2, 4초 대기)
            else:
                log_debug("❌ API 호출 최종 실패", error_msg, exc_info=True)
    return None # 모든 재시도 실패 시 None 반환

def normalize_symbol(raw_symbol):
    """원본 심볼을 내부 표준 심볼명으로 정규화"""
    symbol = str(raw_symbol).upper().strip()
    return SYMBOL_MAPPING.get(symbol) or SYMBOL_MAPPING.get(symbol.replace('.P', '').replace('PERP', ''))

def get_total_collateral(force=False):
    """총 자산 조회 (캐싱 적용)"""
    now = time.time()
    if not force and account_cache["time"] > now - 30 and account_cache["data"]:
        return account_cache["data"] # 캐싱된 데이터 반환

    equity = Decimal("0")
    
    # 🔧 수정: 통합 계정 조회 시 E501 (USER_NOT_FOUND) 또는 일반적인 API 에러 발생 시 즉시 선물 계정으로 폴백
    unified_account_checked = False
    try:
        unified = _get_api_response(unified_api.list_unified_accounts) # E501 발생 시 여기서 예외 발생 후 바로 except로 이동
        unified_account_checked = True # 통합 계정 조회 시도됨
        if unified:
            for attr in ['unified_account_total_equity', 'equity']:
                if hasattr(unified, attr):
                    equity = Decimal(str(getattr(unified, attr)))
                    if equity > 0: # 유효한 값이면 사용
                        log_debug("✅ 통합 계정 자산 조회", f"성공: {equity:.2f} USDT")
                        account_cache.update({"time": now, "data": equity})
                        return equity
        # 통합 계정 조회 결과가 없거나 0인 경우 (Unified Account가 활성화되지 않은 경우 등)
        log_debug("⚠️ 통합 계정 자산 조회 실패", "통합 계정 정보가 없거나 자산이 0입니다. 선물 계정 자산으로 폴백합니다.")
    except gate_api_exceptions.ApiException as e:
        # _get_api_response에서 발생한 ApiException이 여기에 잡힘. 특히 E501 USER_NOT_FOUND는 여기서 처리.
        log_debug("❌ 통합 계정 조회 건너뛰기", f"통합 계정 조회 중 오류 발생 ({e.status}: {e.reason}). 선물 계정 자산으로 폴백합니다.")
    except Exception as e:
        log_debug("❌ 통합 계정 조회 중 일반 오류", str(e), exc_info=True)
    
    # 통합 계정 조회 실패 시 (또는 오류 발생 시) 선물 계정으로 폴백
    # unified_account_checked 플래그는 필요 없으므로 제거
    acc = _get_api_response(api.list_futures_accounts, SETTLE)
    if acc:
        equity = Decimal(str(getattr(acc, 'available', '0')))
        log_debug("✅ 선물 계정 자산 조회", f"성공: {equity:.2f} USDT")
    else:
        log_debug("❌ 선물 계정 자산 조회도 실패", "자산 정보를 가져올 수 없습니다.")
    
    account_cache.update({"time": now, "data": equity}) # 캐시 업데이트
    return equity

def get_price(symbol):
    """현재 시장 가격 조회"""
    ticker = _get_api_response(api.list_futures_tickers, SETTLE, contract=symbol)
    if ticker and len(ticker) > 0:
        return Decimal(str(ticker[0].last))
    log_debug(f"⚠️ 가격 조회 실패 ({symbol})", "티커 데이터 없음 또는 API 오류")
    return Decimal("0")

# ========================================
# 6. TP/SL 저장 및 관리 (PineScript v6.12 호환)
# ========================================

def store_tp_sl(symbol, tp, sl, entry_number):
    """PineScript에서 전달받은 TP/SL 값을 진입 단계별로 저장"""
    with tpsl_lock:
        tpsl_storage.setdefault(symbol, {})[entry_number] = {
            "tp": tp, "sl": sl, "entry_time": time.time()
        }

def get_tp_sl(symbol, entry_number=None):
    """저장된 TP/SL 조회 또는 기본값 반환"""
    with tpsl_lock:
        if symbol in tpsl_storage:
            if entry_number and entry_number in tpsl_storage[symbol]:
                return tpsl_storage[symbol][entry_number]["tp"], tpsl_storage[symbol][entry_number]["sl"], tpsl_storage[symbol][entry_number]["entry_time"]
            elif tpsl_storage[symbol]: # entry_number 없으면 최신 값 반환
                latest_entry = max(tpsl_storage[symbol].keys())
                return tpsl_storage[symbol][latest_entry]["tp"], tpsl_storage[symbol][latest_entry]["sl"], tpsl_storage[symbol][latest_entry]["entry_time"]
    
    # 저장된 값이 없거나 유효하지 않을 경우 PineScript 기본 TP/SL 값 반환
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    return Decimal("0.006") * Decimal(str(cfg["tp_mult"])), Decimal("0.04") * Decimal(str(cfg["sl_mult"])), time.time()

# ========================================
# 7. 중복 신호 체크 및 시간대 조절
# ========================================

def get_time_based_multiplier():
    """한국 시간 기준, 시간대별 진입 배수를 반환 (야간 시간 0.5배)"""
    return Decimal("0.5") if KST.localize(datetime.now()).hour >= 22 or KST.localize(datetime.now()).hour < 9 else Decimal("1.0")
    
def is_duplicate(data):
    """웹훅 신호의 중복을 체크하여 동일한 신호가 COOLDOWN_SECONDS 이내에 여러 번 처리되는 것을 방지"""
    with signal_lock:
        now = time.time()
        symbol_id = f"{data.get('symbol', '')}_{data.get('side', '')}"
        signal_id_from_pine = data.get("id", "")

        # 1. PineScript ID를 통한 빠른 중복 체크 (5초 이내)
        if signal_id_from_pine and recent_signals.get(signal_id_from_pine) and (now - recent_signals[signal_id_from_pine]["time"] < 5):
            log_debug(f"🔄 중복 신호 무시 ({data.get('symbol', '')})", f"PineScript ID '{signal_id_from_pine}' 5초 이내 중복.")
            return True

        # 2. 심볼+방향 기반 쿨다운 체크
        if recent_signals.get(symbol_id) and (now - recent_signals[symbol_id]["time"] < COOLDOWN_SECONDS):
            log_debug(f"🔄 중복 신호 무시 ({data.get('symbol', '')})", f"'{symbol_id}' 쿨다운({COOLDOWN_SECONDS}초) 중.")
            return True

        # 신규 신호로 기록
        recent_signals[symbol_id] = {"time": now, "id": signal_id_from_pine}
        if signal_id_from_pine:
            recent_signals[signal_id_from_pine] = {"time": now}
        
        # 반대 방향 포지션의 쿨다운 정보는 제거 (새로운 진입 시 반대 방향 쿨다운 리셋)
        recent_signals.pop(f"{data.get('symbol', '')}_{'short' if data.get('side') == 'long' else 'long'}", None)
        
        # 오래된 신호 기록 정리 (5분 이상된 기록 삭제)
        recent_signals.update({k: v for k, v in recent_signals.items() if now - v["time"] < 300})
        return False

# ========================================
# 8. 수량 계산 (최소 수량 보장 및 명목 금액 체크)
# ========================================

def calculate_position_size(symbol, signal_type, entry_multiplier=Decimal("1.0")):
    """포지션 크기를 계산 (피라미딩, SL-Rescue 가중치, 최소 수량/금액 보장)"""
    cfg = SYMBOL_CONFIG[symbol]
    equity, price = get_total_collateral(), get_price(symbol)
    if equity <= 0 or price <= 0:
        log_debug(f"⚠️ 수량 계산 불가 ({symbol})", f"자산: {equity}, 가격: {price}")
        return Decimal("0")
    
    entry_count = position_state.get(symbol, {}).get("entry_count", 0)
    if entry_count >= 5: # 최대 진입 횟수 제한
        log_debug(f"⚠️ 최대 진입 도달 ({symbol})", f"현재 진입 횟수: {entry_count}/5")
        return Decimal("0")
    
    entry_ratios = [Decimal("20"), Decimal("40"), Decimal("120"), Decimal("480"), Decimal("960")]
    current_ratio = entry_ratios[entry_count]
    
    is_sl_rescue = (signal_type == "sl_rescue") # 파인스크립트에서 넘어온 signal_type 사용
    if is_sl_rescue:
        current_ratio *= Decimal("1.5") # SL-Rescue 시 50% 추가 가중치
        log_debug(f"🚨 손절직전 가중치 적용 ({symbol})", f"기본 비율({entry_ratios[entry_count]}%) → 150% 증량({float(current_ratio)}%)")
    
    position_value = equity * (current_ratio / Decimal("100")) * entry_multiplier
    contract_value = price * cfg["contract_size"]
    calculated_qty = (position_value / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
    
    final_qty = max(calculated_qty, cfg["min_qty"]) # 최소 수량 보장
    
    # 최소 명목 금액 (min_notional) 체크 및 조정
    current_notional = final_qty * price * cfg["contract_size"]
    if current_notional < cfg["min_notional"]:
        log_debug(f"💡 최소 주문금액 ({cfg['min_notional']} USDT) 미달 감지 ({symbol})", f"현재 명목가치: {current_notional:.2f} USDT")
        min_qty_for_notional = (cfg["min_notional"] / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
        final_qty = max(final_qty, min_qty_for_notional)
        log_debug(f"💡 최소 주문금액 조정 완료 ({symbol})", f"조정된 최종 수량: {final_qty:.4f} (명목가치: {final_qty * price * cfg['contract_size']:.2f} USDT)")
    
    log_debug(f"📊 수량 계산 상세 ({symbol})", f"진입 #{entry_count+1}/5, 비율: {float(current_ratio)}%, 최종수량: {final_qty:.4f}")
    return final_qty

# ========================================
# 9. 포지션 상태 관리
# ========================================

def update_position_state(symbol):
    """Gate.io 포지션 상태 조회 및 내부 전역 변수 업데이트"""
    with position_lock:
        pos_info = _get_api_response(api.get_position, SETTLE, symbol)
        size = Decimal("0")
        if pos_info and pos_info.size:
            try:
                size = Decimal(str(pos_info.size))
            except Exception:
                log_debug(f"❌ 포지션 크기 변환 오류 ({symbol})", f"Invalid size received: {pos_info.size}. Treating as 0.")
                size = Decimal("0")
        
        if size != 0: # 포지션이 열려있는 경우
            existing = position_state.get(symbol, {})
            position_state[symbol] = {
                "price": Decimal(str(pos_info.entry_price)),
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "value": abs(size) * Decimal(str(pos_info.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"],
                "entry_count": existing.get("entry_count", 0),
                "entry_time": existing.get("entry_time", time.time()),
                "sl_entry_count": existing.get("sl_entry_count", 0),
                'time_multiplier': existing.get('time_multiplier', Decimal("1.0")) # 기존 배수 유지
            }
            return False # 포지션 열림
        else: # 포지션이 닫혀있는 경우
            position_state[symbol] = {"price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"),
                                      "entry_count": 0, "entry_time": None, "sl_entry_count": 0, 'time_multiplier': Decimal("1.0")}
            pyramid_tracking.pop(symbol, None) # 관련 추적 정보 초기화
            tpsl_storage.pop(symbol, None) # TP/SL 저장소 초기화
            return True # 포지션 닫힘

def is_sl_rescue_condition(symbol):
    """손절 직전 추가 진입 (SL-Rescue) 조건 평가 (파인스크립트 0.05% 임계값 반영)"""
    with position_lock:
        pos = position_state.get(symbol)
        if not pos or pos["size"] == 0 or pos["entry_count"] >= 5 or pos["sl_entry_count"] >= 3:
            return False # 기본 조건 미충족 (포지션 없음, 최대 진입/SL-Rescue 횟수 초과)
        
        current_price, avg_price, side = get_price(symbol), pos["price"], pos["side"]
        if current_price <= 0: return False
        
        # 시간 감쇠 적용된 SL 가격 계산 (PineScript 로직과 동일)
        original_tp, original_sl, entry_start_time = get_tp_sl(symbol, pos["entry_count"])
        time_elapsed = time.time() - entry_start_time
        periods_15s = int(time_elapsed / 15)
        
        sl_decay_amt_ps, sl_min_pct_ps = Decimal("0.004") / 100, Decimal("0.09") / 100
        symbol_weight_sl = Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
        sl_reduction = Decimal(str(periods_15s)) * (sl_decay_amt_ps * symbol_weight_sl)
        current_sl_pct_adjusted = max(sl_min_pct_ps, original_sl - sl_reduction)
        
        sl_price = avg_price * (1 - current_sl_pct_adjusted) if side == "buy" else avg_price * (1 + current_sl_pct_adjusted)
        
        sl_proximity_threshold = Decimal("0.0005") # 0.05% 임계값
        
        # SL 가격 근접 및 손실 상태 확인
        is_near_sl = abs(current_price - sl_price) / sl_price <= sl_proximity_threshold
        is_underwater = (side == "buy" and current_price < avg_price) or (side == "sell" and current_price > avg_price)
        
        if is_near_sl and is_underwater:
            log_debug(f"🚨 SL-Rescue 조건 충족 ({symbol})", f"현재가: {current_price:.8f}, 손절가: {sl_price:.8f}, 차이: {abs(current_price - sl_price) / sl_price * 100:.4f}% (<{sl_proximity_threshold*100:.2f}%), 현재 손절률: {current_sl_pct_adjusted*100:.2f}%")
        return is_near_sl and is_underwater

# ========================================
# 10. 주문 실행 및 청산
# ========================================

def place_order(symbol, side, qty, entry_number, time_multiplier):
    """실제로 Gate.io에 주문 제출 및 내부 상태 업데이트"""
    with position_lock:
        cfg = SYMBOL_CONFIG[symbol]
        qty_dec = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN) # 수량 정규화
        
        if qty_dec < cfg["min_qty"]: # 최소 수량 미달 시 조정
            log_debug(f"💡 최소 수량 적용 ({symbol})", f"계산: {qty} → 적용: {qty_dec}")
            qty_dec = cfg["min_qty"]
        
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        
        # 과도한 주문 방지 (총 자산의 10배를 넘는 주문)
        order_value_estimate = qty_dec * get_price(symbol) * cfg["contract_size"]            
        if order_value_estimate > get_total_collateral() * Decimal("10"):
            log_debug(f"⚠️ 과도한 주문 방지 ({symbol})", f"예상 주문 명목 가치: {order_value_estimate:.2f} USDT. 주문이 너무 큽니다.")
            return False

        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=False)
        if not _get_api_response(api.create_futures_order, SETTLE, order): return False
        
        # 내부 포지션 상태 업데이트
        position_state.setdefault(symbol, {})["entry_count"] = entry_number # 다음 진입 넘버링
        position_state[symbol]["entry_time"] = time.time()
        if entry_number == 1: # 최초 진입 시에만 시간대별 배수 저장
            position_state[symbol]['time_multiplier'] = time_multiplier
        
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약 (진입 #{entry_number}/5)")
        time.sleep(2) # API 호출 후 잠시 대기
        update_position_state(symbol) # 실제 포지션 정보로 동기화
        return True

def close_position(symbol, reason="manual"):
    """포지션 시장가 청산 및 모든 관련 내부 상태 초기화"""
    with position_lock:
        if not _get_api_response(api.create_futures_order, SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)): return False
        
        log_debug(f"✅ 청산 완료 ({symbol})", f"이유: {reason}")
        
        # 내부 상태 변수 초기화
        position_state[symbol] = {"price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"),
                                  "entry_count": 0, "entry_time": None, "sl_entry_count": 0, 'time_multiplier': Decimal("1.0")}
        pyramid_tracking.pop(symbol, None)
        tpsl_storage.pop(symbol, None)
        with signal_lock: # 해당 심볼의 최근 신호 기록도 초기화
            for k in [k for k in recent_signals.keys() if k.startswith(symbol + "_")]: recent_signals.pop(k)
        
        time.sleep(1)
        update_position_state(symbol)
        return True

# ========================================
# 11. Flask 라우트 (웹훅 및 상태 API)
# ========================================

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    """서버 상태 확인용 핑"""
    return "pong", 200

@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    """서버 내부 캐시 (신호 기록, TP/SL 저장 등) 초기화"""
    with signal_lock: recent_signals.clear()
    with tpsl_lock: tpsl_storage.clear()
    pyramid_tracking.clear()
    log_debug("🔄 캐시 초기화", "모든 신호 및 TP/SL 캐시가 초기화되었습니다.")
    return jsonify({"status": "cache_cleared"})

@app.before_request
def log_request():
    """모든 수신 요청을 로깅 (ping 제외)"""
    if request.path != "/ping":
        log_debug("🌐 요청 수신", f"{request.method} {request.path}")
        if request.method == "POST" and request.path == "/webhook":
            raw_data = request.get_data(as_text=True)
            log_debug("📩 웹훅 원본 데이터", f"길이: {len(raw_data)}, 내용: {raw_data[:200]}...")

@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    """TradingView 웹훅 신호 처리"""
    try:
        raw_data = request.get_data(as_text=True)
        if not raw_data: return jsonify({"error": "Empty data"}), 400
        
        data = None
        try: data = json.loads(raw_data)
        except json.JSONDecodeError:
            if request.form: data = request.form.to_dict()
            elif "&" not in raw_data and "=" not in raw_data: # 단순 문자열 시도
                try: data = json.loads(urllib.parse.unquote(raw_data)) # urllib.parse 임포트 필요
                except Exception: pass
        
        if not data:
            if "{{" in raw_data and "}}" in raw_data: # 플레이스홀더 감지
                return jsonify({"error": "TradingView placeholder detected", "solution": "Use {{strategy.order.alert_message}} in TradingView alert message field"}), 400
            return jsonify({"error": "Failed to parse data"}), 400
        
        symbol_raw, side, action = data.get("symbol", ""), data.get("side", "").lower(), data.get("action", "").lower()
        log_debug("📊 파싱된 웹훅 데이터", f"심볼: {symbol_raw}, 방향: {side}, 액션: {action}, signal_type: {data.get('signal', 'N/A')}, entry_type: {data.get('type', 'N/A')}")
        
        symbol = normalize_symbol(symbol_raw)
        if not symbol or symbol not in SYMBOL_CONFIG: 
            log_debug("❌ 유효하지 않은 심볼", f"원본: {symbol_raw}. SYMBOL_CONFIG에 없음 또는 정규화 실패.")
            return jsonify({"error": f"Invalid symbol: {symbol_raw}"}), 400
        
        if is_duplicate(data): return jsonify({"status": "duplicate_ignored"}), 200 # 중복 신호 무시

        if action == "entry" and side in ["long", "short"]:
            try: task_q.put_nowait(data) # 큐에 작업 추가
            except queue.Full: return jsonify({"status": "queue_full"}), 429
            return jsonify({"status": "queued", "symbol": symbol, "side": side, "queue_size": task_q.qsize()}), 200
        
        elif action == "exit":
            if data.get("reason") in ["TP", "SL"]: return jsonify({"status": "ignored", "reason": "tp_sl_handled_by_server"}), 200 # 서버 자체 처리
            update_position_state(symbol)
            if position_state.get(symbol, {}).get("side"): # 포지션이 열려있으면 청산
                log_debug(f"✅ TradingView 청산 신호 수신 ({symbol})", f"이유: {data.get('reason', '알 수 없음')}. 포지션 청산 시도.")
                close_position(symbol, data.get("reason", "signal"))
            else:
                log_debug(f"💡 청산 실행 불필요 ({symbol})", "활성 포지션이 없거나 이미 청산되었습니다.")
            return jsonify({"status": "success", "action": "exit"})
        
        log_debug("❌ 알 수 없는 웹훅 액션", f"수신된 액션: {action}. 처리되지 않았습니다.")
        return jsonify({"error": "Invalid action"}), 400     
        
    except Exception as e:
        log_debug("❌ 웹훅 처리 중 예외 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    """서버의 현재 상태를 JSON 형태로 반환"""
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG: # 모든 심볼에 대해 포지션 상태 조회
            update_position_state(sym)
            pos = position_state.get(sym, {})
            if pos.get("side"): # 활성 포지션 정보 구성
                entry_count = pos.get("entry_count", 0)
                tp_sl_info = []
                for i in range(1, entry_count + 1):
                    tp, sl, entry_start_time = get_tp_sl(sym, i)
                    tp_sl_info.append({
                        "entry": i, "tp_pct": float(tp) * 100, "sl_pct": float(sl) * 100,
                        "entry_time_kst": datetime.fromtimestamp(entry_start_time, KST).strftime('%Y-%m-%d %H:%M:%S'),
                        "elapsed_seconds": int(time.time() - entry_start_time)
                    })
                positions[sym] = {
                    "side": pos["side"], "size": float(pos["size"]), "price": float(pos["price"]),
                    "value": float(pos["value"]), "entry_count": entry_count, "sl_rescue_count": pos.get("sl_entry_count", 0),
                    "symbol_multiplier": SYMBOL_CONFIG[sym]["tp_mult"], "time_multiplier": float(pos.get('time_multiplier', Decimal("1.0"))),
                    "tp_sl_info": tp_sl_info, "pyramid_tracking": pyramid_tracking.get(sym, {"signal_count": 0, "last_entered": False})
                }
        
        return jsonify({
            "status": "running", "version": "v6.12_e501_direct_fallback", "current_time_kst": datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            "balance_usdt": float(equity), "active_positions": positions, "cooldown_seconds": COOLDOWN_SECONDS,
            "max_entries_per_symbol": 5, "max_sl_rescue_per_position": 3,
            "sl_rescue_proximity_threshold": float(Decimal("0.0005")) * 100,
            "pyramiding_entry_ratios": [float(r) for r in [Decimal("20"), Decimal("40"), Decimal("120"), Decimal("480"), Decimal("960")]],
            "symbol_weights": {sym: {"tp_mult": cfg["tp_mult"], "sl_mult": cfg["sl_mult"]} for sym, cfg in SYMBOL_CONFIG.items()},
            "queue_info": {"size": task_q.qsize(), "max_size": task_q.maxsize}
        })
        
    except Exception as e:
        log_debug("❌ 상태 조회 중 오류 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ========================================
# 12. WebSocket 모니터링 (TP/SL 체크)
# ========================================

async def price_monitor():
    """실시간 가격 모니터링 및 TP/SL 도달 여부 체크"""
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
                    if data.get("event") in ["error", "subscribe"]: continue # 에러/구독 확인 메시지 스킵
                    
                    result = data.get("result")
                    if isinstance(result, list): # 여러 티커 데이터 처리
                        for item in result: check_tp_sl(item)
                    elif isinstance(result, dict): # 단일 티커 데이터 처리
                        check_tp_sl(result)
                        
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
            log_debug("🔌 웹소켓 연결 문제", f"재연결 시도... ({type(e).__name__})")
        except Exception as e:
            log_debug("❌ 웹소켓 오류", str(e), exc_info=True)
        await asyncio.sleep(5)

def check_tp_sl(ticker):
    """실시간 티커 데이터 기반 TP/SL 도달 여부 체크 및 청산"""
    try:
        symbol, price = ticker.get("contract"), Decimal(str(ticker.get("last", "0")))
        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0: return

        with position_lock:
            pos = position_state.get(symbol, {})
            entry_price, side, entry_count = pos.get("price"), pos.get("side"), pos.get("entry_count", 0)
            if not entry_price or not side or entry_count == 0: return # 활성 포지션 없음
            
            symbol_weight_tp, symbol_weight_sl = Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"])), Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
            original_tp, original_sl, entry_start_time = get_tp_sl(symbol, entry_count)
            time_elapsed, periods_15s = time.time() - entry_start_time, int(time_elapsed / 15)
            
            # TP/SL 시간 감쇠 계산 (PineScript v6.12 로직 반영)
            tp_decay_amt_ps, tp_min_pct_ps = Decimal("0.002") / 100, Decimal("0.12") / 100
            tp_reduction = Decimal(str(periods_15s)) * (tp_decay_amt_ps * symbol_weight_tp)
            adjusted_tp = max(tp_min_pct_ps, original_tp - tp_reduction)
            
            sl_decay_amt_ps, sl_min_pct_ps = Decimal("0.004") / 100, Decimal("0.09") / 100
            sl_reduction = Decimal(str(periods_15s)) * (sl_decay_amt_ps * symbol_weight_sl)
            adjusted_sl = max(sl_min_pct_ps, original_sl - sl_reduction)

            tp_price = entry_price * (1 + adjusted_tp) if side == "buy" else entry_price * (1 - adjusted_tp)
            sl_price = entry_price * (1 - adjusted_sl) if side == "buy" else entry_price * (1 + adjusted_sl)
            
            tp_triggered = (price >= tp_price if side == "buy" else price <= tp_price)
            sl_triggered = (price <= sl_price if side == "buy" else price >= sl_price)

            if tp_triggered: log_debug(f"🎯 TP 트리거 ({symbol})", f"현재가: {price:.8f}, TP가: {tp_price:.8f} ({adjusted_tp*100:.3f}%)."); close_position(symbol, "TP")
            elif sl_triggered: log_debug(f"🛑 SL 트리거 ({symbol})", f"현재가: {price:.8f}, SL가: {sl_price:.8f} ({adjusted_sl*100:.3f}%)."); close_position(symbol, "SL")

    except Exception as e:
        log_debug(f"❌ TP/SL 체크 중 오류 발생 ({ticker.get('contract', 'Unknown')})", str(e), exc_info=True)

# ========================================
# 13. 백그라운드 모니터링 (주기적인 포지션 현황 로깅)
# ========================================

def position_monitor():
    """주기적으로 현재 포지션 상태를 모니터링하고 로깅"""
    while True:
        time.sleep(300) # 5분 대기
        try:
            total_value, active_positions_log = Decimal("0"), []
            for symbol in SYMBOL_CONFIG: # 모든 심볼에 대해 포지션 상태 업데이트 및 정보 수집
                update_position_state(symbol)
                pos = position_state.get(symbol, {})
                if pos.get("side"):
                    total_value += pos["value"]
                    pyramid_info = f", 일반 추가 신호: {pyramid_tracking.get(symbol, {}).get('signal_count', 0)}회"
                    active_positions_log.append(f"{symbol}: {pos['side']} {pos['size']:.4f} 계약 @ {pos['price']:.8f} (총 진입: #{pos.get('entry_count', 0)}/5, SL-Rescue: #{pos.get('sl_entry_count', 0)}/3, 명목 가치: {pos['value']:.2f} USDT{pyramid_info})")
            
            if active_positions_log: # 활성 포지션이 있으면 로깅
                equity = get_total_collateral()
                exposure_pct = (total_value / equity * 100) if equity > 0 else 0
                log_debug("📊 포지션 현황 보고", f"활성 포지션: {len(active_positions_log)}개, 총 명목 가치: {total_value:.2f} USDT, 총 자산: {equity:.2f} USDT, 총 노출도: {exposure_pct:.1f}%")
                for pos_info in active_positions_log: log_debug("  └", pos_info)
            else: log_debug("📊 포지션 현황 보고", "현재 활성 포지션이 없습니다.")
        except Exception as e:
            log_debug("❌ 포지션 모니터링 중 오류 발생", str(e), exc_info=True)

# ========================================
# 14. 워커 스레드 (비동기 웹훅 처리)
# ========================================

def worker(idx):
    """웹훅 요청을 큐에서 가져와 비동기적으로 처리하는 워커 스레드"""
    log_debug(f"⚙️ 워커-{idx} 시작", f"워커 스레드 {idx}가 시작되었습니다.")
    while True:
        try:
            data = task_q.get(timeout=1) # 큐에서 1초 대기하며 작업 가져오기
            try: handle_entry(data) # 실제 진입 처리 로직 호출
            except Exception as e: log_debug(f"❌ 워커-{idx} 처리 오류", f"작업 처리 중 예외 발생: {str(e)}", exc_info=True)
            finally: task_q.task_done() # 작업 완료 알림
        except queue.Empty: continue # 큐가 비어있으면 계속 대기
        except Exception as e: log_debug(f"❌ 워커-{idx} 심각한 오류", f"워커 스레드 자체 오류: {str(e)}", exc_info=True); time.sleep(1)

def handle_entry(data):
    """TradingView 웹훅 진입 신호 분석 및 주문 실행"""
    symbol_raw, side, signal_type, entry_type = data.get("symbol", ""), data.get("side", "").lower(), data.get("signal", "none"), data.get("type", "")
    log_debug("📊 진입 처리 시작", f"심볼: {symbol_raw}, 방향: {side}, signal_type: {signal_type}, entry_type: {entry_type}")
    
    symbol = normalize_symbol(symbol_raw)
    if not symbol or symbol not in SYMBOL_CONFIG: log_debug(f"❌ 잘못된 심볼 ({symbol_raw})", "처리 중단."); return
        
    update_position_state(symbol)
    entry_count, current_pos_side = position_state.get(symbol, {}).get("entry_count", 0), position_state.get(symbol, {}).get("side")
    desired_side = "buy" if side == "long" else "sell"

    entry_multiplier = position_state.get(symbol, {}).get('time_multiplier', Decimal("1.0")) if entry_count > 0 else get_time_based_multiplier()
    
    if current_pos_side and current_pos_side != desired_side: # 반대 포지션 청산
        log_debug(f"🔄 반대 포지션 감지 ({symbol})", f"현재: {current_pos_side.upper()} → 목표: {desired_side.upper()}. 기존 포지션 청산 시도.")
        if not close_position(symbol, "reverse_entry"): log_debug(f"❌ 반대 포지션 청산 실패 ({symbol})", "신규 진입 중단."); return
        time.sleep(1); update_position_state(symbol); entry_count = 0 # 청산 후 상태 리셋

    if entry_count >= 5: log_debug(f"⚠️ 최대 진입 도달 ({symbol})", f"현재: {entry_count}/5. 추가 진입하지 않음."); return
    
    is_sl_rescue_signal = (signal_type == "sl_rescue") # SL-Rescue는 파인스크립트 signal 필드 기준으로 판단
    
    if is_sl_rescue_signal: # 손절 직전 진입 (SL-Rescue) 로직
        sl_entry_count = position_state.get(symbol, {}).get("sl_entry_count", 0)
        if sl_entry_count >= 3: log_debug(f"⚠️ 손절직전 최대 진입 도달 ({symbol})", f"현재 SL-Rescue 횟수: {sl_entry_count}/3. 추가 진입하지 않음."); return
        if not is_sl_rescue_condition(symbol): log_debug(f"⏭️ 손절직전 조건 불충족 ({symbol})", "서버 자체 검증 실패. 진입 건너뜀."); return
        
        position_state[symbol]["sl_entry_count"] = sl_entry_count + 1 # SL-Rescue 카운트 증가
        log_debug(f"🚨 손절직전 진입 진행 ({symbol})", f"SL-Rescue #{sl_entry_count + 1}/3회 시도.")
    else: # 일반 추가 진입 로직 (스킵 로직 적용)
        if entry_count > 0: # 1차 진입 이후만 해당
            pyramid_tracking.setdefault(symbol, {"signal_count": 0, "last_entered": False})["signal_count"] += 1
            tracking = pyramid_tracking[symbol]
            
            current_price, avg_price = get_price(symbol), position_state[symbol]["price"]
            price_ok = (current_pos_side == "buy" and current_price < avg_price) or (current_pos_side == "sell" and current_price > avg_price)
            
            should_skip_pyramid, skip_reason = False, ""
            if tracking["signal_count"] == 1: should_skip_pyramid, skip_reason = True, "첫 번째 추가 진입 신호는 건너뜁니다."
            elif tracking["signal_count"] == 2: should_skip_pyramid = not price_ok; skip_reason = "가격 조건 미충족." if should_skip_pyramid else ""
            else: should_skip_pyramid = tracking["last_entered"] or not price_ok; skip_reason = ("직전 진입" if tracking["last_entered"] else "가격 조건 미충족.") if should_skip_pyramid else ""

            if should_skip_pyramid: tracking["last_entered"] = False; log_debug(f"⏭️ 일반 추가 진입 건너뛰기 ({symbol})", f"신호 #{tracking['signal_count']}, 이유: {skip_reason}"); return
            else: tracking["last_entered"] = True

    actual_entry_number = entry_count + 1
    
    # TP/SL 저장 (SL-Rescue는 기존 포지션의 TP/SL을 따라감)
    if not is_sl_rescue_signal:
        tp_map = [Decimal("0.005"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002"), Decimal("0.0015")]
        sl_map = [Decimal("0.04"), Decimal("0.038"), Decimal("0.035"), Decimal("0.033"), Decimal("0.03")]
        
        if actual_entry_number <= len(tp_map): # 배열 범위 확인
            tp = tp_map[actual_entry_number - 1] * Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
            sl = sl_map[actual_entry_number - 1] * Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
            store_tp_sl(symbol, tp, sl, actual_entry_number)
            log_debug(f"💾 TP/SL 저장 ({symbol})", f"진입 #{actual_entry_number}/5, TP: {tp*100:.3f}%, SL: {sl*100:.3f}%")
        else: log_debug(f"⚠️ TP/SL 저장 오류 ({symbol})", f"진입 단계 {actual_entry_number}에 대한 TP/SL 맵이 없습니다.")
    
    qty = calculate_position_size(symbol, signal_type, entry_multiplier)
    if qty <= 0: log_debug(f"❌ 수량 계산 실패 ({symbol})", "계산 수량 0 이하. 주문하지 않음."); return
    
    success = place_order(symbol, desired_side, qty, actual_entry_number, entry_multiplier)
    if success: log_debug(f"✅ 진입 성공 ({symbol})", f"{desired_side.upper()} {float(qty)} 계약 (진입 #{actual_entry_number}/5, 타입: {'손절직전(+50%)' if is_sl_rescue_signal else '일반'}).")
    else: log_debug(f"❌ 진입 실패 ({symbol})", f"{desired_side.upper()} 주문 실행 실패.")
        
# ========================================
# 15. 메인 실행
# ========================================

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.12 (재시도 로직 적용 최종 버전) - 실행 중...")
    log_debug("📊 현재 설정", f"감시 심볼: {len(SYMBOL_CONFIG)}개, 서버 쿨다운: {COOLDOWN_SECONDS}초, 최대 피라미딩 진입: 5회")
    
    # 설정 로깅
    for label, data in [("🎯 심볼별 TP/SL 가중치", {sym.replace('_USDT', ''): f"TP: {cfg['tp_mult']*100:.0f}%, SL: {cfg['sl_mult']*100:.0f}%" for sym, cfg in SYMBOL_CONFIG.items()}),
                       ("📈 전략 기본 설정", "기본 익절률: 0.6%, 기본 손절률: 4.0%"),
                       ("🔄 TP/SL 시간 감쇠", "15초마다 TP -0.002%*가중치, SL -0.004%*가중치 (최소 TP 0.12%, SL 0.09%)"),
                       ("📊 피라미딩 진입 비율", "1차: 20%, 2차: 40%, 3차: 120%, 4차: 480%, 5차: 960% (자산 대비)"),
                       ("📉 단계별 TP (가중치 적용 전)", "1차: 0.5%, 2차: 0.35%, 3차: 0.3%, 4차: 0.2%, 5차: 0.15%"),
                       ("📉 단계별 SL (가중치 적용 전)", "1차: 4.0%, 2차: 3.8%, 3차: 3.5%, 4차: 3.3%, 5차: 3.0%"),
                       ("🚨 손절직전 진입 (SL-Rescue)", f"범위: {Decimal('0.0005')*100:.2f}%, 수량 가중치: 150%, 최대 진입: 3회 (다른 조건 무시)"),
                       ("💡 최소 수량/명목 금액 보장", "계산 수량이 최소 규격 미달 시 자동으로 조정하여 주문.")]:
        log_debug(label, data)
    
    # 초기 자산 및 포지션 확인
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산 확인", f"{equity:.2f} USDT" if equity > 0 else "자산 조회 실패 또는 잔고 부족. API 키 확인 필요.")
    
    initial_active_positions = []
    # SYMBOL_CONFIG의 각 심볼에 대해 초기 포지션 상태를 로드
    for symbol in SYMBOL_CONFIG: 
        # update_position_state는 이미 재시도 로직이 적용된 _get_api_response를 사용하므로 여기서 추가적인 try-except는 필요 없음
        update_position_state(symbol) 
        pos = position_state.get(symbol, {})
        if pos.get("side"):
            initial_active_positions.append(f"{symbol}: {pos['side']} {pos['size']:.4f} @ {pos['price']:.8f} (총 진입: #{pos.get('entry_count', 0)}/5, SL-Rescue: #{pos.get('sl_entry_count', 0)}/3)")
    log_debug("📊 초기 활성 포지션", f"서버 시작 시 {len(initial_active_positions)}개 감지." if initial_active_positions else "감지되지 않음.")
    for pos_info in initial_active_positions: log_debug("  └", pos_info)
    
    # 백그라운드 작업 시작
    log_debug("🔄 백그라운드 작업", "모니터링 및 워커 스레드 시작 중...")
    for target_func, name in [(position_monitor, "PositionMonitor"), (lambda: asyncio.run(price_monitor()), "PriceMonitor")]:
        threading.Thread(target=target_func, daemon=True, name=name).start()
    
    # 워커 스레드 시작
    log_debug("⚙️ 워커 스레드", f"{WORKER_COUNT}개 시작 중...")
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True, name=f"Worker-{i}").start()
        log_debug(f"⚙️ 워커-{i} 시작", f"워커 스레드 {i} 실행 중.")
        
    # Flask 웹 서버 실행
    port = int(os.environ.get("PORT", 8080)) # 환경 변수에서 포트 가져오기 (기본 8080)
    log_debug("🌐 웹 서버 시작", f"Flask 웹 서버가 0.0.0.0:{port}에서 실행됩니다.")
    log_debug("✅ 준비 완료", "웹훅 신호를 기다리는 중입니다. (TradingView 알림 설정 확인)")
    log_debug("🔍 테스트 및 상태 확인 방법", f"POST http://localhost:{port}/webhook 으로 웹훅 테스트, GET http://localhost:{port}/status 로 서버 상태 확인.")
    app.run(host="0.0.0.0", port=port, debug=False)
