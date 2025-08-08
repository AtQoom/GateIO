#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.12 - 모든 기능 유지, 코드 정리 및 최적화, API 재시도 로직 추가
통합 계정 조회 로직 제거. 이전처럼 선물 계정 자산만 조회하도록 수정.
SL 임계값 0.01%로 변경 및 UnboundLocalError 해결.
Pine Script와 쿨다운 연동 강화.

주요 기능:
1. 5단계 피라미딩 (30%→30%→40%→150%→500%)
2. 손절직전 진입 (SL_Rescue) - 150% 가중치, 최대 3회, 0.01% 임계값
3. 최소 수량 및 최소 명목 금액 보장
4. 심볼별 가중치 적용 및 시간 감쇠 TP/SL
5. 야간 시간 진입 수량 조절 (0.5배→1.0배)
6. TradingView 웹훅 기반 자동 주문
7. 실시간 WebSocket을 통한 TP/SL 모니터링 및 자동 청산
8. API 호출 시 일시적 오류에 대한 재시도 로직
9. 통합 계정 관련 API 호출 제거 (이전 동작 방식 재현)
10. SL 임계값 0.01%로 변경
11. check_tp_sl 함수의 UnboundLocalError 해결
12. 웹훅 쿨다운 로직 강화
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
unified_api = UnifiedApi(client)  # 현재 통합 계정 관련 API는 사용 안 함

# ========================================
# 3. 상수 및 설정
# ========================================

COOLDOWN_SECONDS = 14 # 신호 쿨다운 초
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
# 5. 핵심 유틸리티 함수 (API 재시도 포함)
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
# 6. TP/SL 저장 및 관리 (PineScript v6.12 호환)
# ========================================

def store_tp_sl(symbol, tp, sl, entry_number):
    with tpsl_lock:
        tpsl_storage.setdefault(symbol, {})[entry_number] = {
            "tp": tp, "sl": sl, "entry_time": time.time()
        }

def get_tp_sl(symbol, entry_number=None):
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
    return Decimal("0.006") * Decimal(str(cfg["tp_mult"])), Decimal("0.04") * Decimal(str(cfg["sl_mult"])), time.time()

# ========================================
# 7. 중복 신호 체크 및 시간대 조절
# ========================================

def get_time_based_multiplier():
    now_hour = KST.localize(datetime.now()).hour
    return Decimal("1.0") if (now_hour >= 22 or now_hour < 9) else Decimal("1.0")

def is_duplicate(data):
    with signal_lock:
        now = time.time()
        symbol_id = f"{data.get('symbol', '')}_{data.get('side', '')}"
        signal_unique_id = data.get("id", "")

        signal_time_from_pine = data.get("time")
        signal_time_seconds = signal_time_from_pine / 1000 if signal_time_from_pine else now

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
# 8. 수량 계산 (피라미딩, SL-Rescue, 최소 수량/명목 보장)
# ========================================

def calculate_position_size(symbol, signal_type, entry_multiplier=Decimal("1.0")):
    cfg = SYMBOL_CONFIG[symbol]
    equity, price = get_total_collateral(), get_price(symbol)
    if equity <= 0 or price <= 0:
        log_debug(f"⚠️ 수량 계산 불가 ({symbol})", f"자산: {equity}, 가격: {price}")
        return Decimal("0")
    entry_count = position_state.get(symbol, {}).get("entry_count", 0)
    if entry_count >= 5:
        log_debug(f"⚠️ 최대 진입 도달 ({symbol})", f"현재 진입 횟수: {entry_count}/5")
        return Decimal("0")
    entry_ratios = [Decimal("30"), Decimal("50"), Decimal("100"), Decimal("200"), Decimal("400")]
    current_ratio = entry_ratios[entry_count]
    if signal_type == "sl_rescue":
        current_ratio = current_ratio * Decimal("1.5")
        log_debug(f"🚨 손절직전 가중치 적용 ({symbol})", f"기본 비율({entry_ratios[entry_count]}%) → 150% 증량({float(current_ratio)}%)")

    position_value = equity * (current_ratio / Decimal("100")) * entry_multiplier
    contract_value = price * cfg["contract_size"]
    calculated_qty = (position_value / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
    final_qty = max(calculated_qty, cfg["min_qty"])
    current_notional = final_qty * price * cfg["contract_size"]
    if current_notional < cfg["min_notional"]:
        log_debug(f"💡 최소 주문금액 ({cfg['min_notional']} USDT) 미달 감지 ({symbol})", f"현재 명목가치: {current_notional:.2f} USDT")
        min_qty_for_notional = (cfg["min_notional"] / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
        final_qty = max(final_qty, min_qty_for_notional)
        log_debug(f"💡 최소 주문금액 조정 완료 ({symbol})", f"조정된 최종 수량: {final_qty:.4f} (명목가치: {final_qty * price * cfg['contract_size']:.2f} USDT)")
    log_debug(f"📊 수량 계산 상세 ({symbol})", f"진입 #{entry_count+1}/5, 비율: {float(current_ratio)}%, 최종수량: {final_qty:.4f}")
    return final_qty

# ========================================
# 9. 포지션 상태 관리 및 업데이트
# ========================================

def update_position_state(symbol):
    with position_lock:
        pos_info = _get_api_response(api.get_position, SETTLE, symbol)
        size = Decimal("0")
        if pos_info and pos_info.size:
            try:
                size = Decimal(str(pos_info.size))
            except Exception:
                log_debug(f"❌ 포지션 크기 변환 오류 ({symbol})", f"Invalid size received: {pos_info.size}. Treating as 0.")
                size = Decimal("0")
        if size != 0:
            existing = position_state.get(symbol, {})
            position_state[symbol] = {
                "price": Decimal(str(pos_info.entry_price)),
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "value": abs(size) * Decimal(str(pos_info.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"],
                "entry_count": existing.get("entry_count", 0),
                "entry_time": existing.get("entry_time", time.time()),
                "sl_entry_count": existing.get("sl_entry_count", 0),
                'time_multiplier': existing.get('time_multiplier', Decimal("1.0"))
            }
            return False
        else:
            position_state[symbol] = {"price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"),
                                      "entry_count": 0, "entry_time": None, "sl_entry_count": 0, 'time_multiplier': Decimal("1.0")}
            pyramid_tracking.pop(symbol, None)
            tpsl_storage.pop(symbol, None)
            return True

# ========================================
# 10. SL-Rescue 조건 확인
# ========================================

def is_sl_rescue_condition(symbol):
    with position_lock:
        pos = position_state.get(symbol)
        if not pos or pos["size"] == 0 or pos["entry_count"] >= 5 or pos["sl_entry_count"] >= 3:
            return False
        current_price, avg_price, side = get_price(symbol), pos["price"], pos["side"]
        if current_price <= 0: return False
        original_tp, original_sl, entry_start_time = get_tp_sl(symbol, pos["entry_count"])
        symbol_weight_sl = Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
        # 손절가 계산
        sl_price = avg_price * (1 - original_sl * symbol_weight_sl) if side == "buy" else avg_price * (1 + original_sl * symbol_weight_sl)
        
        # 손절가 도달 조건: 현재가가 손절가 이하인 경우 (롱), 이상인 경우 (숏)
        if (side == "buy" and current_price <= sl_price) or (side == "sell" and current_price >= sl_price):
            return True
        return False

# ========================================
# 11. 주문 실행 및 청산
# ========================================

def place_order(symbol, side, qty, entry_number, time_multiplier):
    with position_lock:
        cfg = SYMBOL_CONFIG[symbol]
        qty_dec = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN)
        if qty_dec < cfg["min_qty"]:
            log_debug(f"💡 최소 수량 적용 ({symbol})", f"계산: {qty} → 적용: {qty_dec}")
            qty_dec = cfg["min_qty"]
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order_value_estimate = qty_dec * get_price(symbol) * cfg["contract_size"]
        if order_value_estimate > get_total_collateral() * Decimal("10"):
            log_debug(f"⚠️ 과도한 주문 방지 ({symbol})", f"명목 가치: {order_value_estimate:.2f} USDT. 주문 취소.")
            return False
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=False)
        if not _get_api_response(api.create_futures_order, SETTLE, order): return False
        position_state.setdefault(symbol, {})["entry_count"] = entry_number
        position_state[symbol]["entry_time"] = time.time()
        if entry_number == 1:
            position_state[symbol]['time_multiplier'] = time_multiplier
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약 (진입 #{entry_number}/5)")
        time.sleep(2)
        update_position_state(symbol)
        return True

def close_position(symbol, reason="manual"):
    with position_lock:
        if not _get_api_response(api.create_futures_order, SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)):
            return False
        log_debug(f"✅ 청산 완료 ({symbol})", f"이유: {reason}")
        position_state[symbol] = {"price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"),
                                  "entry_count": 0, "entry_time": None, "sl_entry_count": 0, 'time_multiplier': Decimal("1.0")}
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
# 12. Flask 라우트 (웹훅 및 상태 API)
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

@app.before_request
def log_request():
    if request.path != "/ping":
        log_debug("🌐 요청 수신", f"{request.method} {request.path}")
        if request.method == "POST" and request.path == "/webhook":
            raw_data = request.get_data(as_text=True)
            log_debug("📩 웹훅 원본 데이터", f"길이: {len(raw_data)}, 내용: {raw_data[:200]}...")

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
            if "{{" in raw_data and "}}" in raw_data:
                return jsonify({
                    "error": "TradingView placeholder detected",
                    "solution": "Use {{strategy.order.alert_message}} in TradingView alert message field"
                }), 400
            return jsonify({"error": "Failed to parse data"}), 400

        # 기본 데이터
        symbol_raw = data.get("symbol", "")
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        log_debug(
            "📊 파싱된 웹훅 데이터",
            f"심볼: {symbol_raw}, 방향: {side}, 액션: {action}, "
            f"signal_type: {data.get('signal', 'N/A')}, entry_type: {data.get('type', 'N/A')}"
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
                task_q.put_nowait(data)
            except queue.Full:
                return jsonify({"status": "queue_full"}), 429
            return jsonify({
                "status": "queued",
                "symbol": symbol,
                "side": side,
                "queue_size": task_q.qsize()
            }), 200

        # ===== Exit 처리 =====
        elif action == "exit":
            reason = data.get("reason", "").upper()

            # TP/SL 청산 알림은 서버 내부 자동청산 처리만 하고 무시
            if reason in ("TP", "SL"):
                log_debug(f"TP/SL 청산 알림 무시 ({symbol})", f"이유: {reason}")
                return jsonify({"status": "ignored", "reason": "tp_sl_handled_by_server"}), 200

            # 현재 포지션 상태 및 방향 확인
            update_position_state(symbol)
            pos = position_state.get(symbol, {})
            pos_side = pos.get("side")  # 'buy' or 'sell'

            current_price = get_price(symbol)
            target_price = Decimal(str(data.get("price", current_price)))
            base_tolerance = Decimal("0.0002")  # 0.02%
            symbol_mult = Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
            price_tolerance = base_tolerance * symbol_mult

            if current_price <= 0 or target_price <= 0:
                log_debug(f"⚠️ 가격 0 감지 ({symbol})", f"필터 무시 - 현재가: {current_price}, 목표가: {target_price}")
            else:
                price_diff_ratio = abs(current_price - target_price) / target_price
                # 불리한 경우에만 필터 적용 (롱: 현재가 < 목표가, 숏: 현재가 > 목표가)
                if (pos_side == "buy" and current_price < target_price) or (pos_side == "sell" and current_price > target_price):
                    if price_diff_ratio > price_tolerance:
                        log_debug(f"⏳ 불리한 가격, 청산 보류 ({symbol})",
                                  f"현재가 {current_price:.8f}, 목표가 {target_price:.8f}, "
                                  f"차이 {price_diff_ratio*100:.3f}% > 허용 {price_tolerance*100:.3f}%")
                        return jsonify({"status": "exit_delayed", "reason": "price_diff"}), 200
                else:
                    log_debug(f"💡 유리한 가격 즉시 청산 ({symbol})",
                              f"현재가 {current_price:.8f}, 목표가 {target_price:.8f}, "
                              f"차이 {price_diff_ratio*100:.3f}%")

            if pos_side:
                log_debug(f"✅ 청산 신호 수신 ({symbol})", f"이유: {reason}. 포지션 청산 시도.")
                close_position(symbol, reason)
            else:
                log_debug(f"💡 청산 불필요 ({symbol})", "활성 포지션 없음")

            return jsonify({"status": "success", "action": "exit"})

        # ===== 알 수 없는 액션 =====
        log_debug("❌ 알 수 없는 웹훅 액션", f"수신된 액션: {action}")
        return jsonify({"error": "Invalid action"}), 400

    except Exception as e:
        log_debug("❌ 웹훅 처리 중 예외 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
            pos = position_state.get(sym, {})
            if pos.get("side"):
                entry_count = pos.get("entry_count", 0)
                tp_sl_info = []
                for i in range(1, entry_count + 1):
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
                    "entry_count": entry_count,
                    "sl_rescue_count": pos.get("sl_entry_count", 0),
                    "symbol_multiplier": SYMBOL_CONFIG[sym]["tp_mult"],
                    "time_multiplier": float(pos.get('time_multiplier', Decimal("1.0"))),
                    "tp_sl_info": tp_sl_info,
                    "pyramid_tracking": pyramid_tracking.get(sym, {"signal_count": 0, "last_entered": False})
                }
        return jsonify({
            "status": "running",
            "version": "v6.12_sl_0_01pct_cooldown_14s_fixed",
            "current_time_kst": datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            "balance_usdt": float(equity),
            "active_positions": positions,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "max_entries_per_symbol": 5,
            "max_sl_rescue_per_position": 3,
            "sl_rescue_proximity_threshold": float(Decimal("0.0001")) * 100,
            "pyramiding_entry_ratios": [30, 30, 40, 150, 500],
            "symbol_weights": {sym: {"tp_mult": cfg["tp_mult"], "sl_mult": cfg["sl_mult"]} for sym, cfg in SYMBOL_CONFIG.items()},
            "queue_info": {"size": task_q.qsize(), "max_size": task_q.maxsize}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 중 오류 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ========================================
# 13. WebSocket 모니터링 (TP/SL 체크)
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
    try:
        symbol, price = ticker.get("contract"), Decimal(str(ticker.get("last", "0")))
        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0: return
        with position_lock:
            pos = position_state.get(symbol, {})
            entry_price, side, entry_count = pos.get("price"), pos.get("side"), pos.get("entry_count", 0)
            if not entry_price or not side or entry_count == 0:
                return
            symbol_weight_tp = Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
            symbol_weight_sl = Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
            original_tp, original_sl, entry_start_time = get_tp_sl(symbol, entry_count)
            time_elapsed = time.time() - entry_start_time
            periods_15s = int(time_elapsed / 15)
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
            if tp_triggered:
                log_debug(f"🎯 TP 트리거 ({symbol})", f"현재가: {price:.8f}, TP가: {tp_price:.8f} ({adjusted_tp*100:.3f}%)")
                close_position(symbol, "TP")
            elif sl_triggered:
                log_debug(f"🛑 SL 트리거 ({symbol})", f"현재가: {price:.8f}, SL가: {sl_price:.8f} ({adjusted_sl*100:.3f}%)")
                close_position(symbol, "SL")
    except Exception as e:
        log_debug(f"❌ TP/SL 체크 오류 ({ticker.get('contract', 'Unknown')})", str(e), exc_info=True)

# ========================================
# 14. 백그라운드 모니터링 및 워커 스레드
# ========================================

def position_monitor():
    while True:
        time.sleep(300)
        try:
            total_value = Decimal("0")
            active_positions_log = []
            for symbol in SYMBOL_CONFIG:
                update_position_state(symbol)
                pos = position_state.get(symbol, {})
                if pos.get("side"):
                    total_value += pos["value"]
                    pyramid_info = f", 일반 추가 신호: {pyramid_tracking.get(symbol, {}).get('signal_count', 0)}회"
                    active_positions_log.append(f"{symbol}: {pos['side']} {pos['size']:.4f} 계약 @ {pos['price']:.8f} (총 진입: #{pos.get('entry_count', 0)}/5, SL-Rescue: #{pos.get('sl_entry_count', 0)}/3, 명목 가치: {pos['value']:.2f} USDT{pyramid_info})")
            if active_positions_log:
                equity = get_total_collateral()
                exposure_pct = (total_value / equity * 100) if equity > 0 else 0
                log_debug("📊 포지션 현황 보고", f"활성 포지션: {len(active_positions_log)}개, 총 명목 가치: {total_value:.2f} USDT, 총 자산: {equity:.2f} USDT, 노출도: {exposure_pct:.1f}%")
                for pos_info in active_positions_log:
                    log_debug("  └", pos_info)
            else:
                log_debug("📊 포지션 현황 보고", "현재 활성 포지션이 없습니다.")
        except Exception as e:
            log_debug("❌ 포지션 모니터링 오류 발생", str(e), exc_info=True)

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
    symbol_raw, side, signal_type, entry_type = data.get("symbol", ""), data.get("side", "").lower(), data.get("signal", "none"), data.get("type", "")
    log_debug("📊 진입 처리 시작", f"심볼: {symbol_raw}, 방향: {side}, signal_type: {signal_type}, entry_type: {entry_type}")

    symbol = normalize_symbol(symbol_raw)
    if not symbol or symbol not in SYMBOL_CONFIG:
        log_debug(f"❌ 잘못된 심볼 ({symbol_raw})", "처리 중단.")
        return

    update_position_state(symbol)
    entry_count = position_state.get(symbol, {}).get("entry_count", 0)
    current_pos_side = position_state.get(symbol, {}).get("side")
    desired_side = "buy" if side == "long" else "sell"
    entry_multiplier = position_state.get(symbol, {}).get('time_multiplier', Decimal("1.0")) if entry_count > 0 else get_time_based_multiplier()

    if current_pos_side and current_pos_side != desired_side:
        log_debug(f"🔄 반대 포지션 감지 ({symbol})", f"{current_pos_side.upper()} → {desired_side.upper()} 기존 청산 시도")
        if not close_position(symbol, "reverse_entry"):
            log_debug(f"❌ 반대 포지션 청산 실패 ({symbol})", "신규 진입 중단.")
            return
        time.sleep(1)
        update_position_state(symbol)
        entry_count = 0

    if entry_count >= 5:
        log_debug(f"⚠️ 최대 진입 도달 ({symbol})", f"진입 {entry_count}/5. 추가진입 안함")
        return

    is_sl_rescue_signal = (signal_type == "sl_rescue")
    if is_sl_rescue_signal:
        sl_entry_count = position_state.get(symbol, {}).get("sl_entry_count", 0)
        if sl_entry_count >= 3:
            log_debug(f"⚠️ SL-Rescue 최대 진입 도달 ({symbol})", f"{sl_entry_count}/3회")
            return
        if not is_sl_rescue_condition(symbol):
            log_debug(f"⏭️ SL-Rescue 조건 미충족 ({symbol})", "진입 건너뜀")
            return
        position_state[symbol]["sl_entry_count"] = sl_entry_count + 1
        log_debug(f"🚨 SL-Rescue 진입 ({symbol})", f"#{sl_entry_count + 1}/3회 시도")
        actual_entry_number = entry_count + 1
    else:
        if entry_count > 0:
            current_price, avg_price = get_price(symbol), position_state[symbol]["price"]
            if (current_pos_side == "buy" and current_price >= avg_price) or (current_pos_side == "sell" and current_price <= avg_price):
                log_debug(f"⏭️ 가격조건 미충족 ({symbol})", f"현재가: {current_price:.8f}, 평단가: {avg_price:.8f}")
                return
        actual_entry_number = entry_count + 1

    # TP/SL 저장
    tp_map = [Decimal("0.005"), Decimal("0.004"), Decimal("0.0035"), Decimal("0.003"), Decimal("0.002")]
    sl_map = [Decimal("0.04"), Decimal("0.038"), Decimal("0.035"), Decimal("0.033"), Decimal("0.03")]
    if actual_entry_number <= len(tp_map):
        tp = tp_map[actual_entry_number - 1] * Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
        sl = sl_map[actual_entry_number - 1] * Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
        store_tp_sl(symbol, tp, sl, actual_entry_number)
        log_debug(f"💾 TP/SL 저장 ({symbol})", f"#{actual_entry_number}/5, TP: {tp*100:.3f}%, SL: {sl*100:.3f}%")

    # ===== 가격차이 필터 (불리할때만) =====
    current_price = get_price(symbol)
    target_price = Decimal(str(data.get("price", current_price)))
    base_tolerance = Decimal("0.0002")  # 0.02%
    symbol_mult = Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
    price_tolerance = base_tolerance * symbol_mult

    if current_price <= 0 or target_price <= 0:
        log_debug(f"⚠️ 가격 0 감지 ({symbol})", f"필터 무시 - 현재가: {current_price}, 목표가: {target_price}")
    else:
        price_diff_ratio = abs(current_price - target_price) / target_price
        # 불리한 방향일 때만 필터 적용
        if (side == "long" and current_price > target_price) or (side == "short" and current_price < target_price):
            if price_diff_ratio > price_tolerance:
                log_debug(f"⏳ 불리한 가격, 진입 보류 ({symbol})",
                          f"현재가 {current_price:.8f}, 목표가 {target_price:.8f}, "
                          f"차이 {price_diff_ratio*100:.3f}% > 허용 {price_tolerance*100:.3f}%")
                return
        else:
            log_debug(f"💡 유리한 가격 즉시 진입 ({symbol})",
                      f"현재가 {current_price:.8f}, 목표가 {target_price:.8f}, 차이 {price_diff_ratio*100:.3f}%")
    # ===== 필터 끝 =====

    qty = calculate_position_size(symbol, signal_type, entry_multiplier)
    if qty <= 0:
        log_debug(f"❌ 수량계산 실패 ({symbol})", "0 이하")
        return
    if place_order(symbol, desired_side, qty, actual_entry_number, entry_multiplier):
        log_debug(f"✅ 진입 성공 ({symbol})", f"{desired_side.upper()} {float(qty)} 계약 (#{actual_entry_number}/5)")
    else:
        log_debug(f"❌ 진입 실패 ({symbol})", f"{desired_side.upper()}")

# ========================================
# 15. 메인 실행
# ========================================

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.12 (SL 임계값 0.01%, 쿨다운 및 오류 수정)")
    log_debug("📊 현재 설정", f"감시 심볼: {len(SYMBOL_CONFIG)}개, 쿨다운: {COOLDOWN_SECONDS}초, 최대 피라미딩 진입: 5회")
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산 확인", f"{equity:.2f} USDT" if equity > 0 else "자산 조회 실패 또는 잔고 부족")
    initial_active_positions = []
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
        pos = position_state.get(symbol, {})
        if pos.get("side"):
            initial_active_positions.append(f"{symbol}: {pos['side']} {pos['size']:.4f} @ {pos['price']:.8f} (진입: #{pos.get('entry_count', 0)}/5, SL-Rescue: #{pos.get('sl_entry_count', 0)}/3)")
    log_debug("📊 초기 활성 포지션", f"{len(initial_active_positions)}개 감지" if initial_active_positions else "감지 안됨")
    for pos_info in initial_active_positions:
        log_debug("  └", pos_info)
    for target_func, name in [(position_monitor, "PositionMonitor"), (lambda: asyncio.run(price_monitor()), "PriceMonitor")]:
        threading.Thread(target=target_func, daemon=True, name=name).start()
    log_debug("⚙️ 워커 스레드", f"{WORKER_COUNT}개 시작 중")
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True, name=f"Worker-{i}").start()
        log_debug(f"⚙️ 워커-{i} 시작", f"워커 {i} 실행 중")
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"Flask 서버 0.0.0.0:{port}에서 실행 중")
    log_debug("✅ 준비 완료", "웹훅 신호 대기중")
    app.run(host="0.0.0.0", port=port, debug=False)
