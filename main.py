#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.15 - 최종 완성 버전 (슬리피지 로직 최종 개선)
- 슬리피지 허용치를 '10틱 또는 0.05% 중 더 큰 값'으로 적용
- TP는 슬리피지 연동형으로 유지
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
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info: logger.exception("")

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
# 변경: 슬리피지 비율을 0.05%로 상향
PRICE_DEVIATION_LIMIT_PCT = Decimal("0.0005") # 0.05%
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
    "ONDO_USDT": "ONDO_USDT", "ONDOUSDT.P": "ONDO_USDT", "ONDOUSDTPERP": "ONDO_USDT", "ONDO_USDT": "ONDO_USDT", "ONDO": "ONDO_USDT",
}
PRICE_MULTIPLIERS = {
    "PEPE_USDT": Decimal("100000000.0"),
    "SHIB_USDT": Decimal("1000000.0")
}
SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"), "min_notional": Decimal("5"), "tp_mult": 0.55, "sl_mult": 0.55, "tick_size": Decimal("0.1")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"), "min_notional": Decimal("5"), "tp_mult": 0.65, "sl_mult": 0.65, "tick_size": Decimal("0.01")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 0.8, "sl_mult": 0.8, "tick_size": Decimal("0.001")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.0001")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.001")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.001")},
    "PEPE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000000"), "min_notional": Decimal("5"), "tp_mult": 1.2, "sl_mult": 1.2, "tick_size": Decimal("0.00000001")},
    "XRP_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.0001")},
    "DOGE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.2, "sl_mult": 1.2, "tick_size": Decimal("0.00001")},
    "ONDO_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.0001")},
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
            if isinstance(e, gate_api_exceptions.ApiException): error_msg = f"API Error {e.status}: {e.reason}"
            else: error_msg = str(e)
            if attempt < max_retries - 1: log_debug("⚠️ API 호출 재시도", f"시도 {attempt+1}/{max_retries}: {error_msg}, 잠시 후 재시도")
            else: log_debug("❌ API 호출 최종 실패", error_msg, exc_info=True)
    return None

def normalize_symbol(raw_symbol): return SYMBOL_MAPPING.get(str(raw_symbol).upper().strip())

def get_total_collateral(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 30 and account_cache["data"]: return account_cache["data"]
    acc = _get_api_response(api.list_futures_accounts, SETTLE)
    equity = Decimal(str(getattr(acc, 'available', '0'))) if acc else Decimal("0")
    account_cache.update({"time": now, "data": equity})
    return equity

def get_price(symbol):
    ticker = _get_api_response(api.list_futures_tickers, SETTLE, contract=symbol)
    if ticker and isinstance(ticker, list) and len(ticker) > 0: return Decimal(str(ticker[0].last))
    return Decimal("0")

# ========================================
# 6. 파인스크립트 연동을 위한 함수
# ========================================
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

# ========================================
# 7. TP/SL 및 슬리피지 저장/관리
# ========================================
def store_tp_sl(symbol, tp, sl, slippage_pct, entry_number):
    with tpsl_lock: 
        tpsl_storage.setdefault(symbol, {})[entry_number] = {
            "tp": tp, 
            "sl": sl, 
            "entry_slippage_pct": slippage_pct,
            "entry_time": time.time()
        }

def get_tp_sl(symbol, entry_number=None):
    with tpsl_lock:
        if symbol in tpsl_storage:
            if entry_number and entry_number in tpsl_storage[symbol]: 
                return tpsl_storage[symbol][entry_number].values()
            elif tpsl_storage[symbol]: 
                return tpsl_storage[symbol][max(tpsl_storage[symbol].keys())].values()
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    return Decimal("0.005") * Decimal(str(cfg["tp_mult"])), Decimal("0.04") * Decimal(str(cfg["sl_mult"])), Decimal("0"), time.time()

# ========================================
# 8. 중복 신호 체크
# ... (이하 코드 변경 없음)
# ========================================
def is_duplicate(data):
    with signal_lock:
        now = time.time()
        symbol_id = f"{data.get('symbol', '')}_{data.get('side', '')}"
        if recent_signals.get(symbol_id) and (now - recent_signals[symbol_id]["last_processed_time"] < COOLDOWN_SECONDS): return True
        recent_signals[symbol_id] = {"last_processed_time": now}
        recent_signals.update({k: v for k, v in recent_signals.items() if now - v["last_processed_time"] < 300})
        return False

# ========================================
# 9. 수량 계산
# ... (이하 코드 변경 없음)
# ========================================
def calculate_position_size(symbol, signal_type, entry_score=50, current_signal_count=0):
    cfg, equity, price = SYMBOL_CONFIG[symbol], get_total_collateral(), get_price(symbol)
    if equity <= 0 or price <= 0: return Decimal("0")
    entry_ratios = [Decimal("5.0"), Decimal("10.0"), Decimal("25.0"), Decimal("60.0"), Decimal("200.0")]
    current_ratio = entry_ratios[min(current_signal_count, len(entry_ratios) - 1)]
    signal_multiplier = get_signal_type_multiplier(signal_type)
    score_weight = get_entry_weight_from_score(entry_score)
    final_position_ratio = current_ratio * signal_multiplier * score_weight
    position_value = equity * (final_position_ratio / Decimal("100"))
    contract_value = price * cfg["contract_size"]
    calculated_qty = (position_value / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
    final_qty = max(calculated_qty, cfg["min_qty"])
    if final_qty * contract_value < cfg["min_notional"]:
        final_qty = (cfg["min_notional"] / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
    return final_qty

# ========================================
# 10. 포지션 상태 관리
# ... (이하 코드 변경 없음)
# ========================================
def update_position_state(symbol):
    with position_lock:
        pos_info = _get_api_response(api.get_position, SETTLE, symbol)
        size = Decimal(str(pos_info.size)) if pos_info and pos_info.size else Decimal("0")
        if size != 0:
            existing = position_state.get(symbol, {})
            position_state[symbol] = {
                "price": Decimal(str(pos_info.entry_price)), "side": "buy" if size > 0 else "sell", "size": abs(size),
                "value": abs(size) * Decimal(str(pos_info.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"],
                "entry_count": existing.get("entry_count", 0), "normal_entry_count": existing.get("normal_entry_count", 0),
                "premium_entry_count": existing.get("premium_entry_count", 0), "rescue_entry_count": existing.get("rescue_entry_count", 0),
                "entry_time": existing.get("entry_time", time.time()), 'last_entry_ratio': existing.get('last_entry_ratio', Decimal("0"))
            }
            return False
        else:
            position_state[symbol] = {
                "price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"),
                "entry_count": 0, "normal_entry_count": 0, "premium_entry_count": 0, "rescue_entry_count": 0,
                "entry_time": None, 'last_entry_ratio': Decimal("0")
            }
            tpsl_storage.pop(symbol, None)
            return True

# ========================================
# 11. 주문 실행
# ... (이하 코드 변경 없음)
# ========================================
def place_order(symbol, side, qty, signal_type, final_position_ratio=Decimal("0")):
    with position_lock:
        cfg = SYMBOL_CONFIG[symbol]
        size = float(qty) if side == "buy" else -float(qty)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc")
        if not _get_api_response(api.create_futures_order, SETTLE, order): return False
        pos = position_state.setdefault(symbol, {})
        pos["entry_count"] = pos.get("entry_count", 0) + 1
        if "premium" in signal_type: pos["premium_entry_count"] = pos.get("premium_entry_count", 0) + 1
        elif "normal" in signal_type: pos["normal_entry_count"] = pos.get("normal_entry_count", 0) + 1
        elif "rescue" in signal_type: pos["rescue_entry_count"] = pos.get("rescue_entry_count", 0) + 1
        if "rescue" not in signal_type and final_position_ratio > 0: pos['last_entry_ratio'] = final_position_ratio
        pos["entry_time"] = time.time()
        time.sleep(2)
        update_position_state(symbol)
        return True

def close_position(symbol, reason="manual"):
    with position_lock:
        if not _get_api_response(api.create_futures_order, SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)): return False
        update_position_state(symbol)
        with signal_lock: recent_signals.pop(f"{symbol}_long", None); recent_signals.pop(f"{symbol}_short", None)
        return True

# ========================================
# 12. 웹훅 라우트 및 관리용 API
# ... (이하 코드 변경 없음)
# ========================================
@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
            pos = position_state.get(sym, {})
            if pos.get("side"):
                positions[sym] = {
                    "side": pos["side"], "size": float(pos["size"]), "price": float(pos["price"]), "value": float(pos["value"]),
                    "entry_count": pos.get("entry_count", 0), "normal_entry_count": pos.get("normal_entry_count", 0),
                    "premium_entry_count": pos.get("premium_entry_count", 0), "rescue_entry_count": pos.get("rescue_entry_count", 0),
                    "last_entry_ratio": float(pos.get('last_entry_ratio', Decimal("0"))),
                }
        return jsonify({
            "status": "running", "version": "v6.15_final_max_slippage",
            "current_time_kst": datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            "balance_usdt": float(equity), "active_positions": positions,
            "queue_info": {"size": task_q.qsize(), "max_size": task_q.maxsize}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 중 오류 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["POST"])
def webhook():
    try:
        raw_data = request.get_data(as_text=True)
        data = json.loads(raw_data)
        action = data.get("action", "").lower()
        if action == "entry":
            if is_duplicate(data): return jsonify({"status": "duplicate_ignored"}), 200
            task_q.put_nowait(data)
            return jsonify({"status": "queued"}), 200
        elif action == "exit":
            symbol = normalize_symbol(data.get("symbol", ""))
            reason = data.get("reason", "").upper()
            update_position_state(symbol)
            if position_state.get(symbol, {}).get("side"): close_position(symbol, reason)
            return jsonify({"status": "exit_processed"}), 200
        return jsonify({"error": "Invalid action"}), 400
    except Exception as e:
        log_debug("❌ 웹훅 처리 중 예외 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ========================================
# 13. 웹소켓 모니터링 (슬리피지 연동형 TP 적용)
# ... (이하 코드 변경 없음)
# ========================================
async def price_monitor():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols_to_subscribe = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"time": int(time.time()), "channel": "futures.tickers", "event": "subscribe", "payload": symbols_to_subscribe}))
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    result = json.loads(msg).get("result")
                    if isinstance(result, list): [check_tp_sl(item) for item in result]
                    elif isinstance(result, dict): check_tp_sl(result)
        except Exception as e:
            log_debug("🔌 웹소켓 연결 문제", f"재연결 시도... ({type(e).__name__})")
            await asyncio.sleep(5)

def check_tp_sl(ticker):
    try:
        symbol, price = ticker.get("contract"), Decimal(str(ticker.get("last", "0")))
        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0: return
            
        with position_lock:
            pos = position_state.get(symbol, {})
            side, entry_count = pos.get("side"), pos.get("entry_count", 0)
            if not side or entry_count == 0: return
                
            entry_price = pos.get("price")
            if not entry_price: return
            
            cfg = SYMBOL_CONFIG[symbol]
            tp_mult = Decimal(str(cfg["tp_mult"]))
            sl_mult = Decimal(str(cfg["sl_mult"]))
            
            original_tp, original_sl, entry_slippage_pct, entry_start_time = get_tp_sl(symbol, entry_count)
            if not entry_start_time: return
            
            compensated_tp = original_tp - entry_slippage_pct
            
            time_elapsed = time.time() - entry_start_time
            periods_15s = int(time_elapsed / 15) if (entry_start_time and time_elapsed > 0) else 0

            # 동적 TP 계산
            tp_decay_amt = Decimal("0.00002")
            tp_min_pct = Decimal("0.0012")
            tp_reduction = Decimal(str(periods_15s)) * (tp_decay_amt * tp_mult)
            adjusted_tp = max(tp_min_pct * tp_mult, compensated_tp - tp_reduction)
            tp_price = entry_price * (1 + adjusted_tp) if side == "buy" else entry_price * (1 - adjusted_tp)

            # 동적 SL 계산
            sl_decay_amt = Decimal("0.00004")
            sl_min_pct = Decimal("0.0009")
            sl_reduction = Decimal(str(periods_15s)) * (sl_decay_amt * sl_mult)
            adjusted_sl = max(sl_min_pct * sl_mult, original_sl - sl_reduction)
            sl_price = entry_price * (1 - adjusted_sl) if side == "buy" else entry_price * (1 + adjusted_sl)

            # 청산 실행
            if (side == "buy" and price >= tp_price) or (side == "sell" and price <= tp_price):
                log_debug(f"🎯 TP 트리거 ({symbol})", f"현재가: {price:.8f}, 동적TP가: {tp_price:.8f} (Slippage Comp: {-entry_slippage_pct:.4%})")
                close_position(symbol, "TP")
            elif (side == "buy" and price <= sl_price) or (side == "sell" and price >= sl_price):
                log_debug(f"🛑 SL 트리거 ({symbol})", f"현재가: {price:.8f}, 동적SL가: {sl_price:.8f}")
                close_position(symbol, "SL")
                
    except Exception as e:
        log_debug(f"❌ TP/SL 체크 오류 ({ticker.get('contract', 'Unknown')})", str(e), exc_info=True)

# ========================================
# 14. 워커 스레드 및 진입 처리 (최종 슬리피지 로직 적용)
# ========================================
def worker(idx):
    while True:
        try:
            data = task_q.get(timeout=1)
            try: handle_entry(data)
            except Exception as e: log_debug(f"❌ 워커-{idx} 처리 오류", f"작업 처리 중 예외: {str(e)}", exc_info=True)
            finally: task_q.task_done()
        except queue.Empty: continue
        except Exception as e: log_debug(f"❌ 워커-{idx} 심각 오류", f"워커 스레드 오류: {str(e)}", exc_info=True)

def handle_entry(data):
    symbol_raw = data.get("symbol", "")
    side = data.get("side", "").lower()
    signal_type = data.get("type", "normal_long")
    entry_score = data.get("entry_score", 50)
    signal_price_raw = data.get('price')
    tp_pct = Decimal(str(data.get("tp_pct", "0.5"))) / 100
    sl_pct = Decimal(str(data.get("sl_pct", "4.0"))) / 100
    symbol = normalize_symbol(symbol_raw)

    if not symbol or not signal_price_raw: return
    
    cfg = SYMBOL_CONFIG.get(symbol)
    if not cfg:
        log_debug(f"⚠️ 진입 취소 ({symbol})", "알 수 없는 심볼입니다.")
        return

    # 변경: 최종 슬리피지 보호 로직 (max 방식)
    current_price = get_price(symbol)
    price_multiplier = PRICE_MULTIPLIERS.get(symbol, Decimal("1.0"))
    signal_price = Decimal(str(signal_price_raw)) / price_multiplier
    
    if current_price <= 0 or signal_price <= 0: return
        
    price_diff = abs(current_price - signal_price)
    price_diff_pct = price_diff / signal_price if signal_price > 0 else Decimal("0")
    
    # 허용치 계산 (max(A, B) 방식)
    allowed_slippage_by_pct = signal_price * PRICE_DEVIATION_LIMIT_PCT
    allowed_slippage_by_ticks = Decimal(str(MAX_SLIPPAGE_TICKS)) * cfg['tick_size']
    max_allowed_slippage = max(allowed_slippage_by_pct, allowed_slippage_by_ticks)
    
    if price_diff > max_allowed_slippage:
        log_debug(f"⚠️ 진입 취소: 슬리피지 초과 ({symbol})", 
                  f"신호가: {signal_price:.8f}, 현재가: {current_price:.8f}, 실제차이: {price_diff:.8f}, 허용치: {max_allowed_slippage:.8f}")
        return

    update_position_state(symbol)
    pos = position_state.get(symbol, {})
    current_pos_side = pos.get("side")
    desired_side = "buy" if side == "long" else "sell"
    entry_action = "첫진입" if not current_pos_side else "추가진입" if current_pos_side == desired_side else "역전진입"

    if entry_action == "역전진입":
        if not close_position(symbol, "reverse_entry"): return
        time.sleep(1); update_position_state(symbol)
    
    # 추가 진입 시 평단가 조건
    if entry_action == "추가진입" and "rescue" not in signal_type:
        avg_price = pos.get("price")
        if avg_price and current_price > 0 and ((desired_side == "buy" and current_price <= avg_price) or (desired_side == "sell" and current_price >= avg_price)):
            log_debug(f"⚠️ 추가 진입 보류 ({symbol})", f"평단가보다 불리한 가격. 현재가: {current_price:.8f}, 평단가: {avg_price:.8f}")
            return

    # 피라미딩 제한
    pos = position_state.get(symbol, {})
    if pos.get("entry_count", 0) >= 10: return
    if "premium" in signal_type and pos.get("premium_entry_count", 0) >= 5: return
    if "normal" in signal_type and pos.get("normal_entry_count", 0) >= 5: return
    if "rescue" in signal_type and pos.get("rescue_entry_count", 0) >= 3: return

    # 수량 계산
    current_signal_count = pos.get("premium_entry_count", 0) if "premium" in signal_type else pos.get("normal_entry_count", 0)
    qty = calculate_position_size(symbol, signal_type, entry_score, current_signal_count)
    final_position_ratio = Decimal("0")
    if "rescue" in signal_type:
        last_ratio = pos.get('last_entry_ratio', Decimal("5.0"))
        if last_ratio > 0:
            equity, contract_val = get_total_collateral(), get_price(symbol) * cfg["contract_size"]
            rescue_ratio = last_ratio * Decimal("1.5")
            qty = max((equity * rescue_ratio / 100 / contract_val).quantize(Decimal('1'), rounding=ROUND_DOWN), cfg["min_qty"])
            final_position_ratio = rescue_ratio
    
    if qty > 0:
        if place_order(symbol, desired_side, qty, signal_type, final_position_ratio):
            log_debug(f"✅ {entry_action} 성공 ({symbol})", f"{desired_side.upper()} {float(qty)} 계약 (총 #{pos.get('entry_count',0)+1}/10)")
            store_tp_sl(symbol, tp_pct, sl_pct, price_diff_pct, pos.get("entry_count", 0) + 1)
        else:
            log_debug(f"❌ {entry_action} 실패 ({symbol})", f"{desired_side.upper()} 주문 실행 중 오류 발생")

# ========================================
# 15. 포지션 모니터링 및 메인 실행
# ... (이하 코드 변경 없음)
# ========================================
def position_monitor():
    while True:
        time.sleep(30)
        try:
            total_value = Decimal("0")
            active_positions_log = []
            
            for symbol in SYMBOL_CONFIG:
                update_position_state(symbol)
                pos = position_state.get(symbol, {})
                
                if pos.get("side"):
                    total_value += pos.get("value", Decimal("0"))
                    pyramid_info = (f"총:{pos.get('entry_count', 0)}/10, "
                                   f"일반:{pos.get('normal_entry_count', 0)}/5, "
                                   f"프리미엄:{pos.get('premium_entry_count', 0)}/5, "
                                   f"레스큐:{pos.get('rescue_entry_count', 0)}/3")
                    active_positions_log.append(
                        f"{symbol}: {pos['side']} {pos['size']:.4f} @ "
                        f"{pos.get('price', 0):.8f} "
                        f"({pyramid_info}, 명목가치: {pos.get('value', 0):.2f} USDT)"
                    )
            
            if active_positions_log:
                equity = get_total_collateral()
                exposure_pct = (total_value / equity * 100) if equity > 0 else 0
                log_debug("🚀 포지션 현황", 
                         f"활성 포지션: {len(active_positions_log)}개, "
                         f"총 명목가치: {total_value:.2f} USDT, "
                         f"총자산: {equity:.2f} USDT, 노출도: {exposure_pct:.1f}%")
                for pos_info in active_positions_log:
                    log_debug("  └", pos_info)
            else:
                log_debug("📊 포지션 현황 보고", "현재 활성 포지션이 없습니다.")
                
        except Exception as e:
            log_debug("❌ 포지션 모니터링 오류", str(e), exc_info=True)

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.15 (Final with Max Slippage)")
    log_debug("📊 현재 설정", f"감시 심볼: {len(SYMBOL_CONFIG)}개, 쿨다운: {COOLDOWN_SECONDS}초, 워커: {WORKER_COUNT}개")
    log_debug("🎯 전략 핵심", "독립 피라미딩 + 점수 기반 가중치 + 동적 TP/SL + 레스큐 진입")
    log_debug("🛡️ 안전장치", f"동적 슬리피지 (비율 {PRICE_DEVIATION_LIMIT_PCT:.2%} 또는 {MAX_SLIPPAGE_TICKS}틱 중 큰 값), TP 슬리피지 연동")
    
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산 확인", f"{equity:.2f} USDT" if equity > 0 else "자산 조회 실패")
    
    initial_active_positions = []
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
        pos = position_state.get(symbol, {})
        if pos.get("side"):
            initial_active_positions.append(
                f"{symbol}: {pos['side']} {pos['size']:.4f} @ {pos['price']:.8f}"
            )
    
    log_debug("📊 초기 활성 포지션", 
              f"{len(initial_active_positions)}개 감지" if initial_active_positions else "감지 안됨")
    for pos_info in initial_active_positions:
        log_debug("  └", pos_info)
    
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"Flask 서버 0.0.0.0:{port}에서 실행 중")
    log_debug("✅ 준비 완료", "파인스크립트 v6.15 연동 시스템 대기중")
    
    app.run(host="0.0.0.0", port=port, debug=False)
