#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.12 - 5단계 피라미딩 완전 대응

주요 기능:
1. 피라미딩 5회 진입 (0.2%→0.4%→1.2%→4.8%→9.6%)
2. 심볼별 가중치 적용 (BTC 0.5, ETH 0.6, SOL 0.8, PEPE/DOGE 1.2)
3. 시간 경과 TP/SL 감소 (15초마다)
4. 손절직전 진입 150% 가중치
5. 시간대별 진입량 조절 (야간 50%)
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
import queue
import pytz

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

# 🔧 수정: 쿨다운 10초로 변경 (사용자 요청)
COOLDOWN_SECONDS = 10

# 심볼 매핑
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
}

# 🔧 수정: 심볼별 가중치 업데이트 (PineScript v6.12 기준)
SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("0.0001"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.5,  # 0.6 → 0.5로 변경
        "sl_mult": 0.5   # 0.6 → 0.5로 변경
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("0.01"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.6,  # 동일
        "sl_mult": 0.6   # 동일
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("1"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.8,  # 0.9 → 0.8로 변경
        "sl_mult": 0.8   # 0.9 → 0.8로 변경
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
        "tp_mult": 1.2,
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
pyramid_tracking = {}
task_q = queue.Queue(maxsize=100)
WORKER_COUNT = min(6, max(2, os.cpu_count() * 2))
KST = pytz.timezone('Asia/Seoul')

# ========================================
# 5. 핵심 유틸리티 함수
# ========================================

def normalize_symbol(raw_symbol):
    """심볼 정규화"""
    if not raw_symbol:
        return None
    
    symbol = str(raw_symbol).upper().strip()
    
    if symbol in SYMBOL_MAPPING:
        return SYMBOL_MAPPING[symbol]
    
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
        try:
            unified = unified_api.list_unified_accounts()
            for attr in ['unified_account_total_equity', 'equity']:
                if hasattr(unified, attr):
                    equity = Decimal(str(getattr(unified, attr)))
                    account_cache.update({"time": now, "data": equity})
                    return equity
        except Exception:
            pass
        
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
# 6. TP/SL 저장 및 관리 (수정됨)
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
    
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    # 🔧 수정: 0.005 → 0.006 (0.5% → 0.6%), 4.0% 유지
    default_tp = Decimal("0.006") * Decimal(str(cfg["tp_mult"]))  # 변경됨
    default_sl = Decimal("0.04") * Decimal(str(cfg["sl_mult"]))   # 동일
    return default_tp, default_sl, time.time()

# ========================================
# 7. 중복 신호 체크 및 시간대 조절
# ========================================

def get_time_based_multiplier():
    """한국 시간 기준, 시간대별 진입 배수를 반환"""
    now_kst = datetime.now(KST)
    hour = now_kst.hour

    # 한국 시간 밤 10시(22)부터 아침 9시(08:59)까지는 50%만 진입
    if hour >= 22 or hour < 9:
        log_debug("⏰ 시간대 수량 조절", f"야간 시간({now_kst.strftime('%H:%M')}), 배수: 0.5 적용")
        return Decimal("0.5")
    
    return Decimal("1.0")
    
def is_duplicate(data):
    """중복 신호 체크 (수정된 쿨다운 시간 적용)"""
    with signal_lock:
        now = time.time()
        symbol = data.get("symbol", "")
        side = data.get("side", "")
        action = data.get("action", "")
        signal_id = data.get("id", "")
        
        if action == "entry":
            if signal_id and signal_id in recent_signals:
                signal_data = recent_signals[signal_id]
                if now - signal_data["time"] < 5:
                    return True
            
            key = f"{symbol}_{side}"
            
            if key in recent_signals:
                last_signal = recent_signals[key]
                time_diff = now - last_signal["time"]
                
                # 🔧 수정: 12초 → 10초로 변경
                if time_diff < 10:
                    return True
            
            recent_signals[key] = {"time": now, "id": signal_id}
            
            if signal_id:
                recent_signals[signal_id] = {"time": now}
            
            opposite = f"{symbol}_{'short' if side == 'long' else 'long'}"
            recent_signals.pop(opposite, None)
        
        recent_signals.update({
            k: v for k, v in recent_signals.items() 
            if now - v["time"] < 300
        })
        
        return False

# ========================================
# 8. 포지션 크기 계산 (수정됨)
# ========================================

def calculate_position_size(symbol, signal_type, data=None, entry_multiplier=Decimal("1.0")):
    """포지션 크기 계산 (손절직전 가중치 포함)"""
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    
    if equity <= 0 or price <= 0:
        log_debug(f"⚠️ 계산 불가 ({symbol})", f"자산: {equity}, 가격: {price}")
        return Decimal("0")
    
    # 현재 진입 횟수
    entry_count = 0
    if symbol in position_state:
        entry_count = position_state[symbol].get("entry_count", 0)
    
    # 🔧 수정: PineScript v6.12의 5단계 비율 적용
    entry_ratios = [
        Decimal("0.20"), Decimal("0.40"), Decimal("1.2"), 
        Decimal("4.8"), Decimal("9.6")
    ]
    
    if entry_count >= len(entry_ratios):
        log_debug(f"⚠️ 최대 진입 도달 ({symbol})", f"진입 횟수: {entry_count}")
        return Decimal("0")
    
    # 👇 손절 직전 진입 가중치 체크
    is_sl_rescue = signal_type == "sl_rescue" or (data and ("SL_Rescue" in data.get("type", "")))
    
    ratio = entry_ratios[entry_count]
    next_entry_number = entry_count + 1
    
    # 손절 직전이면 50% 추가 가중치 적용 (총 150%)
    if is_sl_rescue:
        ratio = ratio * Decimal("1.5")  # 150% 적용
        log_debug(f"🚨 손절직전 가중치 적용 ({symbol})", f"기본 → 150% 증량")
    
    # 정밀도 향상을 위한 단계별 계산
    ratio_decimal = ratio / Decimal("100")
    position_value = equity * ratio_decimal * entry_multiplier
    contract_value = price * cfg["contract_size"]
    raw_qty = position_value / contract_value
    
    # 수량 조정
    qty_adjusted = (raw_qty / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN)
    final_qty = qty_adjusted * cfg["qty_step"]
    final_qty = max(final_qty, cfg["min_qty"])
    
    # 로그에 상세 정보 추가
    weight_info = "가중(+50%)" if is_sl_rescue else "일반"
    log_debug(f"📊 수량 계산 상세 ({symbol})", 
             f"진입 #{next_entry_number}/5, 비율: {float(ratio)}% ({weight_info}), "
             f"자산: {equity}, 포지션가치: {position_value}, "
             f"계약가치: {contract_value}, 최종수량: {final_qty}")
    
    # 최소 주문금액 체크
    value = final_qty * price * cfg["contract_size"]
    if value < cfg["min_notional"]:
        log_debug(f"⚠️ 최소 주문금액 미달 ({symbol})", f"계산값: {value}, 최소: {cfg['min_notional']}")
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
                existing_count = position_state.get(symbol, {}).get("entry_count", 0)
                existing_time = position_state.get(symbol, {}).get("entry_time", time.time())
                existing_sl_count = position_state.get(symbol, {}).get("sl_entry_count", 0)
                
                position_state[symbol] = {
                    "price": Decimal(str(pos.entry_price)),
                    "side": "buy" if size > 0 else "sell",
                    "size": abs(size),
                    "value": abs(size) * Decimal(str(pos.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"],
                    "entry_count": existing_count,
                    "entry_time": existing_time,
                    "sl_entry_count": existing_sl_count
                }
            else:
                position_state[symbol] = {
                    "price": None, 
                    "side": None, 
                    "size": Decimal("0"), 
                    "value": Decimal("0"),
                    "entry_count": 0,
                    "entry_time": None,
                    "sl_entry_count": 0
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

def place_order(symbol, side, qty, entry_number, time_multiplier):
    """주문 실행"""
    with position_lock:
        try:
            cfg = SYMBOL_CONFIG[symbol]
            qty_dec = Decimal(str(qty)).quantize(cfg["qty_step"], rounding=ROUND_DOWN)
            
            if qty_dec < cfg["min_qty"]:
                log_debug(f"⚠️ 최소 수량 미달 ({symbol})", f"계산: {qty_dec}, 최소: {cfg['min_qty']}")
                return False
            
            size = float(qty_dec) if side == "buy" else -float(qty_dec)
            
            order_value = qty_dec * get_price(symbol) * cfg["contract_size"]            
            if order_value > get_total_collateral() * Decimal("10"):
                log_debug(f"⚠️ 과도한 주문 방지 ({symbol})", f"주문가: {order_value}, 자산: {get_total_collateral()}")
                return False

            order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=False)
            api.create_futures_order(SETTLE, order)
            
            current_count = position_state.get(symbol, {}).get("entry_count", 0)
            position_state.setdefault(symbol, {})["entry_count"] = current_count + 1
            position_state[symbol]["entry_time"] = time.time()
            
            # 최초 진입 시에만 시간대 배수를 저장
            if current_count == 0:
                position_state[symbol]['time_multiplier'] = time_multiplier
                log_debug("💾 배수 저장", f"최초 진입({symbol}), 배수 {time_multiplier} 고정")

            log_debug(f"✅ 주문 성공 ({symbol})", 
                     f"{side.upper()} {float(qty_dec)} 계약 (진입 #{current_count + 1}/5)")
            
            time.sleep(2)
            update_position_state(symbol)
            return True
            
        except Exception as e:
            log_debug(f"❌ 주문 실패 ({symbol})", str(e))
            return False

def close_position(symbol, reason="manual"):
    """포지션 청산 (손절직전 카운터 리셋 포함)"""
    with position_lock:
        try:
            api.create_futures_order(
                SETTLE, 
                FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
            )
            
            log_debug(f"✅ 청산 완료 ({symbol})", f"이유: {reason}")
            
            # sl_entry_count 리셋
            if symbol in position_state:
                position_state[symbol]["entry_count"] = 0
                position_state[symbol]["entry_time"] = None
                position_state[symbol]["sl_entry_count"] = 0
            
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
# 11. Flask 라우트 (복원 및 강화)
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

# ✅ 복원: 누락된 기본 경로 라우트 추가
@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    """웹훅 처리 (완전 복원된 파싱 로직)"""
    try:
        # ✅ 복원: 강력한 데이터 파싱 로직
        raw_data = request.get_data(as_text=True)
        log_debug("📩 원본 데이터", f"길이: {len(raw_data)}, 내용: {raw_data[:200]}...")
        
        if not raw_data:
            return jsonify({"error": "Empty data"}), 400
        
        data = None
        
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            if request.form:
                data = request.form.to_dict()
                log_debug("📝 폼 데이터", f"파싱됨: {data}")
            elif "&" not in raw_data and "=" not in raw_data:
                try:
                    import urllib.parse
                    decoded = urllib.parse.unquote(raw_data)
                    data = json.loads(decoded)
                    log_debug("🔓 URL 디코딩", f"성공: {data}")
                except Exception:
                    pass
        
        if not data:
            if "{{" in raw_data and "}}" in raw_data:
                return jsonify({
                    "error": "TradingView placeholder detected",
                    "solution": "Use {{strategy.order.alert_message}} in TradingView alert message field"
                }), 400
            return jsonify({"error": "Failed to parse data"}), 400
        
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        
        log_debug("📊 파싱된 데이터", f"심볼: {raw_symbol}, 방향: {side}, 액션: {action}")
        
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            log_debug("❌ 잘못된 심볼", f"원본: {raw_symbol}, 정규화: {symbol}")
            return jsonify({"error": f"Invalid symbol: {raw_symbol}"}), 400
        
        if is_duplicate(data):
            log_debug(f"🔄 중복 신호 무시 ({symbol})", f"{side} {action}")
            return jsonify({"status": "duplicate_ignored"}), 200

        if action == "entry" and side in ["long", "short"]:
            try:
                task_q.put_nowait(data)
                log_debug(f"📝 큐 추가 ({symbol})", f"{side} 진입, 대기열: {task_q.qsize()}")
                return jsonify({
                    "status": "queued",
                    "symbol": symbol,
                    "side": side,
                    "queue_size": task_q.qsize()
                }), 200
            except queue.Full:
                return jsonify({"status": "queue_full"}), 429
        
        if action == "exit":
            if data.get("reason") in ["TP", "SL"]:
                return jsonify({"status": "ignored", "reason": "tp_sl_handled_by_server"}), 200
            
            update_position_state(symbol)
            if position_state.get(symbol, {}).get("side"):
                close_position(symbol, data.get("reason", "signal"))
            return jsonify({"status": "success", "action": "exit"})
        
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
# 12. WebSocket 모니터링 (수정된 TP/SL 감쇠 로직)
# ========================================

async def price_monitor():
    """실시간 가격 모니터링 및 TP/SL 체크"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                
                log_debug("📡 웹소켓", f"구독 완료: {symbols}")
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    data = json.loads(msg)
                    
                    if data.get("event") == "error":
                        log_debug("❌ 웹소켓 에러", data.get("message", "Unknown error"))
                        continue
                    
                    if data.get("event") == "subscribe":
                        log_debug("✅ 구독 확인", data.get("channel", ""))
                        continue
                    
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
    """TP/SL 체크 (15초마다 감소) - 수정된 감쇠율 적용"""
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

            symbol_weight = Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
            
            original_tp, original_sl, entry_time = get_tp_sl(symbol, entry_count)
            
            time_elapsed = time.time() - entry_time
            periods_15s = int(time_elapsed / 15)
            
            # 🔧 수정: PineScript v6.12의 감쇠율 적용 (0.002%, 0.004%)
            tp_decay_weighted = (Decimal("0.002") / 100) * symbol_weight
            tp_reduction = Decimal(str(periods_15s)) * tp_decay_weighted
            adjusted_tp = max(Decimal("0.0012"), original_tp - tp_reduction)  # 최소 0.12%
            
            sl_decay_weighted = (Decimal("0.004") / 100) * symbol_weight
            sl_reduction = Decimal(str(periods_15s)) * sl_decay_weighted
            adjusted_sl = max(Decimal("0.0009"), original_sl - sl_reduction)  # 최소 0.09%

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
            time.sleep(300)
            
            total_value = Decimal("0")
            active_positions = []
            
            for symbol in SYMBOL_CONFIG:
                if update_position_state(symbol):
                    pos = position_state.get(symbol, {})
                    if pos.get("side"):
                        total_value += pos["value"]
                        entry_count = pos.get("entry_count", 0)
                        tp_mult = SYMBOL_CONFIG[symbol]["tp_mult"]
                        
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

# ========================================
# 워커 스레드 정의 (완전 복원)
# ========================================

def worker(idx):
    """향상된 워커 스레드"""
    while True:
        try:
            data = task_q.get(timeout=1)
            try:
                handle_entry(data)
            except Exception as e:
                log_debug(f"❌ Worker-{idx} 처리 오류", str(e), exc_info=True)
            finally:
                task_q.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            log_debug(f"❌ Worker-{idx} 심각한 오류", str(e))
            time.sleep(1)

def handle_entry(data):
    """진입 처리 로직 (완전 복원 + 수정사항 적용)"""
    try:
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower()
        signal_type = data.get("signal", "none")
        entry_type = data.get("type", "")
        
        log_debug("📊 처리 시작", f"심볼: {raw_symbol}, 방향: {side}, 타입: {entry_type}")
        
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            log_debug(f"❌ 잘못된 심볼 ({raw_symbol})", "처리 중단")
            return
            
        update_position_state(symbol)
        entry_count = position_state.get(symbol, {}).get("entry_count", 0)
        current = position_state.get(symbol, {}).get("side")
        desired = "buy" if side == "long" else "sell"

        entry_multiplier = Decimal("1.0")
        if entry_count == 0:
            entry_multiplier = get_time_based_multiplier()
        else:
            entry_multiplier = position_state.get(symbol, {}).get('time_multiplier', Decimal("1.0"))

        if current and current != desired:
            log_debug(f"🔄 반대 포지션 감지 ({symbol})", f"현재: {current} → 목표: {desired}")
            if not close_position(symbol, "reverse"):
                log_debug(f"❌ 반대 포지션 청산 실패 ({symbol})", "진입 중단")
                return
            time.sleep(1)
            update_position_state(symbol)
            entry_count = 0

        if entry_count >= 5:
            log_debug(f"⚠️ 최대 진입 도달 ({symbol})", f"현재: {entry_count}/5")
            return
        
        # 손절 직전 진입 vs 기존 추가 진입 구분
        is_sl_rescue = "SL_Rescue" in entry_type or signal_type == "sl_rescue"
        
        if entry_count > 0 and not is_sl_rescue:
            # 기존 추가 진입 로직 (동일)
            if symbol not in pyramid_tracking:
                pyramid_tracking[symbol] = {"signal_count": 0, "last_entered": False}
            
            tracking = pyramid_tracking[symbol]
            tracking["signal_count"] += 1
            signal_count = tracking["signal_count"]
            
            current_price = get_price(symbol)
            avg_price = position_state[symbol]["price"]
            price_ok = (current == "buy" and current_price < avg_price) or \
                       (current == "sell" and current_price > avg_price)
            
            should_skip = False
            skip_reason = ""
            if signal_count == 1:
                should_skip, skip_reason = True, "첫 번째 추가 진입 신호 건너뛰기"
            elif signal_count == 2:
                if not price_ok: 
                    should_skip, skip_reason = True, "가격 조건 미충족"
            else:
                if tracking["last_entered"]:
                    should_skip, skip_reason = True, "이전 진입함 - 건너뛰기"
                elif not price_ok:
                    should_skip, skip_reason = True, "가격 조건 미충족"

            if should_skip:
                tracking["last_entered"] = False
                log_debug(f"⏭️ 추가 진입 건너뛰기 ({symbol})", f"신호 #{signal_count}, 이유: {skip_reason}")
                return
            else:
                tracking["last_entered"] = True
        
        elif is_sl_rescue:
            # 손절 직전 진입 로직
            sl_entry_count = position_state.get(symbol, {}).get("sl_entry_count", 0)
            if sl_entry_count >= 3:
                log_debug(f"⚠️ 손절직전 최대 진입 도달 ({symbol})", f"현재: {sl_entry_count}/3")
                return
            
            # 손절가 근접 조건 재검증
            entry_price = position_state[symbol]["price"]
            current_price = get_price(symbol)
            symbol_weight = Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
            _, original_sl, entry_time = get_tp_sl(symbol, entry_count)
            
            time_elapsed = time.time() - entry_time
            periods_15s = int(time_elapsed / 15)
            
            sl_decay_weighted = (Decimal("0.004") / 100) * symbol_weight
            sl_reduction = Decimal(str(periods_15s)) * sl_decay_weighted
            adjusted_sl = max(Decimal("0.0009"), original_sl - sl_reduction)
            
            sl_price = entry_price * (1 - adjusted_sl) if side == "long" else entry_price * (1 + adjusted_sl)
            sl_proximity_threshold = Decimal("0.0005")  # 0.05%
            
            is_near_sl = False
            if side == "long":
                if current_price > sl_price and current_price < sl_price * (1 + sl_proximity_threshold):
                    is_near_sl = True
            else:
                if current_price < sl_price and current_price > sl_price * (1 - sl_proximity_threshold):
                    is_near_sl = True
            
            if not is_near_sl:
                log_debug(f"⏭️ 손절직전 조건 불충족 ({symbol})", "손절가에 충분히 근접하지 않음")
                return
            
            # 손절 직전 카운터 증가
            position_state[symbol]["sl_entry_count"] = sl_entry_count + 1
            log_debug(f"🚨 손절직전 진입 ({symbol})", f"#{sl_entry_count + 1}/3회, 손절가: {sl_price}, 현재가: {current_price}")
        
        # 모든 조건 통과 후 실제 진입 실행
        actual_entry_number = entry_count + 1
        
        # TP/SL 계산 (손절 직전은 기존 TP/SL 유지)
        if not is_sl_rescue:
            # 🔧 수정: PineScript v6.12의 단계별 TP/SL 배열 적용
            tp_map = [0.005, 0.0035, 0.003, 0.002, 0.0015]
            sl_map = [0.04, 0.038, 0.035, 0.033, 0.03]
            entry_idx = actual_entry_number - 1
            if entry_idx < len(tp_map):
                symbol_weight = SYMBOL_CONFIG[symbol]["tp_mult"]
                tp = Decimal(str(tp_map[entry_idx])) * Decimal(str(symbol_weight))
                sl = Decimal(str(sl_map[entry_idx])) * Decimal(str(symbol_weight))
                store_tp_sl(symbol, tp, sl, actual_entry_number)
        
        # 수량 계산 (손절 직전은 150% 가중치 적용)
        qty = calculate_position_size(symbol, data.get("signal", "none"), data, entry_multiplier)
        
        if qty <= 0:
            log_debug(f"❌ 수량 계산 실패 ({symbol})", "수량이 0 이하")
            return
        
        success = place_order(symbol, desired, qty, actual_entry_number, entry_multiplier)
        
        if success:
            entry_desc = "손절직전(+50%)" if is_sl_rescue else "일반"
            log_debug(f"✅ 워커 진입 성공 ({symbol})", 
                     f"{side} {float(qty)} 계약, 진입 #{actual_entry_number}/5 ({entry_desc})")
        else:
            log_debug(f"❌ 워커 진입 실패 ({symbol})", f"{side} 주문 실패")
            
    except Exception as e:
        log_debug(f"❌ handle_entry 오류 ({data.get('symbol', 'Unknown')})", str(e), exc_info=True)

# ========================================
# 14. 메인 실행
# ========================================

if __name__ == "__main__":
    """서버 시작"""
    
    log_debug("🚀 서버 시작", "v6.12 - 5단계 피라미딩 완전 대응 (문제 해결 버전)")
    log_debug("📊 설정", f"심볼: {len(SYMBOL_CONFIG)}개, 쿨다운: {COOLDOWN_SECONDS}초, 최대 진입: 5회")
    
    # 심볼별 가중치 로그
    log_debug("🎯 심볼별 가중치", "")
    for symbol, cfg in SYMBOL_CONFIG.items():
        tp_weight = cfg["tp_mult"]
        symbol_name = symbol.replace("_USDT", "")
        log_debug(f"  └ {symbol_name}", f"TP/SL 가중치: {tp_weight*100}%")
    
    # 🔧 수정: 전략 설정 로그 업데이트
    log_debug("📈 기본 설정", "익절률: 0.6%, 손절률: 4.0%")  # 0.5% → 0.6%로 변경
    log_debug("🔄 TP/SL 감소", "15초마다 TP -0.002%*가중치, SL -0.004%*가중치")
    log_debug("📊 진입 비율", "1차: 0.2%, 2차: 0.4%, 3차: 1.2%, 4차: 4.8%, 5차: 9.6%")  # 수정됨
    log_debug("📉 단계별 TP", "1차: 0.5%, 2차: 0.35%, 3차: 0.3%, 4차: 0.2%, 5차: 0.15%")
    log_debug("📉 단계별 SL", "1차: 4.0%, 2차: 3.8%, 3차: 3.5%, 4차: 3.3%, 5차: 3.0%")
    log_debug("🔄 추가진입", "1차: 건너뛰기, 2차: 가격 유리시, 3차+: 이전 진입시 건너뛰기")
    log_debug("⏰ 시간대 조절", "야간(22:00-09:00) 50% 진입, 주간 100% 진입")
    
    # 초기 자산 확인
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산", f"{equity:.2f} USDT")
    
    if equity <= 0:
        log_debug("⚠️ 경고", "자산 조회 실패 또는 잔고 부족 - API 키 확인 필요")
    
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
        target=lambda: asyncio.run(price_monitor()), 
        daemon=True, 
        name="PriceMonitor"
    ).start()

    # 워커 스레드 시작
    for i in range(WORKER_COUNT):
        t = threading.Thread(target=worker, args=(i,), daemon=True, name=f"Worker-{i}")
        t.start()
        log_debug("🔄 워커 시작", f"Worker-{i} 시작")
        
    # Flask 실행
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹서버", f"포트 {port}에서 실행")
    log_debug("✅ 준비 완료", "파인스크립트 v6.12 웹훅 대기중...")
    
    # ✅ 추가: 테스트 웹훅 확인 메시지
    log_debug("🔍 테스트 방법", f"POST http://localhost:{port}/webhook 또는 /status로 상태 확인")
    
    app.run(host="0.0.0.0", port=port, debug=False)
