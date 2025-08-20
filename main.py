#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.22 - 최종 완성 버전 (실행 및 설정 오류 수정)
- __name__ 오타를 수정하여 서버가 정상 실행되도록 함
- SYMBOL_CONFIG에 누락된 코인 설정을 추가하여 '알 수 없는 심볼' 오류 해결
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
logger = logging.getLogger(__name__) # [수정] **name** -> __name__
logging.getLogger('werkzeug').setLevel(logging.ERROR)

def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception("")

# ========================================
# 2. Flask 앱 및 API 설정
# ========================================
app = Flask(__name__) # [수정] **name** -> __name__
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# ========================================
# [핵심 수정] 3. 상수 및 설정 (SYMBOL_CONFIG에 ONDO 추가)
# ========================================
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
    "ONDO_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0, "sl_mult": 1.0, "tick_size": Decimal("0.0001")} # [추가] ONDO 설정 추가
}

# ========================================
# 4. 양방향 상태 관리
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

def get_default_pos_side_state():
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
                error_msg = f"API Error {e.status}: {e.body if hasattr(e, 'body') else e.reason}"
            else:
                error_msg = str(e)
            
            if attempt < max_retries - 1:
                log_debug("⚠️ API 호출 재시도", f"시도 {attempt+1}/{max_retries}: {error_msg}, 잠시 후 재시도")
            else:
                log_debug("❌ API 호출 최종 실패", error_msg, exc_info=True)
    return None

def normalize_symbol(raw_symbol):
    return SYMBOL_MAPPING.get(str(raw_symbol).upper().strip().replace("/", "_"))

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

# ========================================
# 6. 파인스크립트 연동 함수
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
# 7. 양방향 TP/SL 관리
# ========================================
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
                return side_storage[entry_number].values()
            elif side_storage:
                return side_storage[max(side_storage.keys())].values()
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    return Decimal("0.005") * Decimal(str(cfg["tp_mult"])), Decimal("0.04") * Decimal(str(cfg["sl_mult"])), Decimal("0"), time.time()


# ========================================
# [수정] 8. 중복 신호 체크 (로그 추가)
# ========================================
def is_duplicate(data):
    with signal_lock:
        now = time.time()
        # 웹훅 페이로드에서 symbol과 side를 안전하게 가져옴
        symbol = data.get('symbol')
        side = data.get('side')
        
        # symbol이나 side가 None이면, 중복 체크를 할 수 없으므로 False 반환
        if not symbol or not side:
            return False
            
        symbol_id = f"{symbol}_{side}"
        
        last_signal = recent_signals.get(symbol_id)
        if last_signal and (now - last_signal.get("last_processed_time", 0) < COOLDOWN_SECONDS):
            return True
        
        recent_signals[symbol_id] = {"last_processed_time": now}
        
        # 오래된 신호 정리
        # 300초(5분) 이상 지난 신호는 메모리에서 삭제
        recent_signals.update({k: v for k, v in recent_signals.items() if now - v.get("last_processed_time", 0) < 300})
        
        return False


# ... (9번부터 11번까지 변경 없음) ...

# ========================================
# 9. 수량 계산
# ========================================
def calculate_position_size(symbol, signal_type, entry_score=50, current_signal_count=0):
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    if equity <= 0 or price <= 0:
        return Decimal("0")
    
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
# 10. 양방향 포지션 상태 관리
# ========================================
def update_all_position_states():
    with position_lock:
        all_positions_from_api = _get_api_response(api.list_positions, SETTLE)
        if all_positions_from_api is None:
            log_debug("❌ 포지션 업데이트 실패", "API 호출에 실패하여 상태를 업데이트할 수 없습니다.")
            return

        active_positions_set = set()
        for pos_info in all_positions_from_api:
            symbol = pos_info.contract
            api_side = pos_info.mode

            if api_side == 'dual_long':
                side = 'long'
            elif api_side == 'dual_short':
                side = 'short'
            else:
                continue
            
            if symbol not in SYMBOL_CONFIG:
                continue
            if symbol not in position_state:
                initialize_states()
            
            current_side_state = position_state[symbol][side]
            current_side_state["price"] = Decimal(str(pos_info.entry_price))
            current_side_state["size"] = Decimal(str(pos_info.size))
            current_side_state["value"] = Decimal(str(pos_info.size)) * Decimal(str(pos_info.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"]
            
            if current_side_state["entry_count"] == 0 and current_side_state["size"] > 0:
                log_debug("🔄 수동 포지션 감지", f"{symbol} {side.upper()} 포지션을 상태에 추가합니다.")
                current_side_state["entry_count"] = 1
                current_side_state["entry_time"] = time.time()
                
            active_positions_set.add((symbol, side))

        for symbol, sides in position_state.items():
            for side in ["long", "short"]:
                if (symbol, side) not in active_positions_set and sides[side]["size"] > 0:
                    log_debug(f"👻 유령 포지션 정리", f"{symbol} {side.upper()} 포지션을 메모리에서 삭제합니다.")
                    position_state[symbol][side] = get_default_pos_side_state()
                    if symbol in tpsl_storage and side in tpsl_storage[symbol]:
                        tpsl_storage[symbol][side].clear()

# ========================================
# 11. 양방향 주문 실행
# ========================================
def place_order(symbol, side, qty, signal_type, final_position_ratio=Decimal("0")):
    with position_lock:
        order = FuturesOrder(contract=symbol, size=float(qty), price="0", tif="ioc", dual_pos=side)
        if not _get_api_response(api.create_futures_order, SETTLE, order):
            return False
        
        pos_side_state = position_state.setdefault(symbol, {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()})[side]
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

def close_position(symbol, side, reason="manual"):
    with position_lock:
        order = FuturesOrder(contract=symbol, size=0, tif="ioc", close=True, dual_pos=side)
        if not _get_api_response(api.create_futures_order, SETTLE, order):
            return False
        
        pos_side_state = position_state.setdefault(symbol, {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()})
        pos_side_state[side] = get_default_pos_side_state()
        
        if symbol in tpsl_storage and side in tpsl_storage[symbol]:
            tpsl_storage[symbol][side].clear()
            
        with signal_lock:
            recent_signals.pop(f"{symbol}_{side}", None)
        return True


# ========================================
# [수정] 12. 웹훅 라우트 및 관리용 API (로그 추가)
# ========================================
@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

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
                            "last_entry_ratio": float(pos_data.get('last_entry_ratio', Decimal("0"))),
                        }
        
        return jsonify({
            "status": "running", "version": "v6.18_log_fix",
            "current_time_kst": datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            "balance_usdt": float(equity), "active_positions": active_positions,
            "queue_info": {"size": task_q.qsize(), "max_size": task_q.maxsize}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 중 오류 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.get_data(as_text=True))
        log_debug("📬 웹훅 수신", f"수신 데이터: {data}") # [추가] 모든 웹훅 수신 시 로그 기록
        
        action = data.get("action", "").lower()
        symbol = normalize_symbol(data.get("symbol", ""))
        side = data.get("side", "").lower()

        if not all([action, symbol, side]):
            log_debug("❌ 유효하지 않은 웹훅", f"필수 필드 누락: {data}")
            return jsonify({"error": "Invalid payload"}), 400
        
        if action == "entry":
            if is_duplicate(data):
                log_debug(f"🔄 중복 신호 무시 ({symbol}_{side.upper()})", "쿨다운(14초) 내 동일 신호가 감지되어 처리하지 않습니다.")
                return jsonify({"status": "duplicate_ignored"}), 200
            
            task_q.put_nowait(data)
            log_debug(f"📥 작업 큐 추가 ({symbol}_{side.upper()})", f"현재 큐 크기: {task_q.qsize()}")
            return jsonify({"status": "queued"}), 200
            
        elif action == "exit":
            reason = data.get("reason", "").upper()
            log_debug(f"🚪 청산 신호 수신 ({symbol}_{side.upper()})", f"사유: {reason}")
            update_all_position_states()
            if position_state.get(symbol, {}).get(side, {}).get("size", Decimal(0)) > 0:
                close_position(symbol, side, reason)
            return jsonify({"status": "exit_processed"}), 200
            
        return jsonify({"error": "Invalid action"}), 400
    except Exception as e:
        log_debug("❌ 웹훅 처리 중 예외 발생", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ... (13번부터 14번까지 변경 없음) ...

# ========================================
# 13. 양방향 웹소켓 모니터링
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
                    if isinstance(result, list):
                        for item in result:
                            check_tp_only(item)
                    elif isinstance(result, dict):
                        check_tp_only(result)
        except Exception as e:
            log_debug("🔌 웹소켓 연결 문제", f"재연결 시도... ({type(e).__name__})")
            await asyncio.sleep(5)

def check_tp_only(ticker):
    try:
        symbol = ticker.get("contract")
        price = Decimal(str(ticker.get("last", "0")))
        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0:
            return
            
        with position_lock:
            for side in ["long", "short"]:
                pos_side_state = position_state.get(symbol, {}).get(side, {})
                if not pos_side_state or pos_side_state.get("size", Decimal(0)) <= 0:
                    continue
                    
                entry_price = pos_side_state.get("price")
                entry_count = pos_side_state.get("entry_count")
                if not entry_price or not entry_count:
                    continue
                    
                cfg = SYMBOL_CONFIG[symbol]
                tp_mult = Decimal(str(cfg["tp_mult"]))
                
                original_tp, _, entry_slippage_pct, entry_start_time = get_tp_sl(symbol, side, entry_count)
                if not entry_start_time:
                    continue
                
                compensated_tp = original_tp - entry_slippage_pct
                time_elapsed = time.time() - entry_start_time
                periods_15s = 0
                if time_elapsed > 0:
                    periods_15s = int(time_elapsed / 15)
                    
                tp_decay_amt = Decimal("0.00002")
                tp_min_pct = Decimal("0.0012")
                
                tp_reduction = Decimal(str(periods_15s)) * (tp_decay_amt * tp_mult)
                adjusted_tp = max(tp_min_pct * tp_mult, compensated_tp - tp_reduction)
                
                if side == "long":
                    tp_price = entry_price * (1 + adjusted_tp)
                    if price >= tp_price:
                        log_debug(f"🎯 롱 TP 트리거 ({symbol})", f"현재가: {price:.8f}, 동적TP가: {tp_price:.8f}")
                        close_position(symbol, "long", "TP")
                elif side == "short":
                    tp_price = entry_price * (1 - adjusted_tp)
                    if price <= tp_price:
                        log_debug(f"🎯 숏 TP 트리거 ({symbol})", f"현재가: {price:.8f}, 동적TP가: {tp_price:.8f}")
                        close_position(symbol, "short", "TP")
                
    except Exception as e:
        log_debug(f"❌ TP 체크 오류 ({ticker.get('contract', 'Unknown')})", str(e), exc_info=True)

# ========================================
# 14. 양방향 진입 처리 로직
# ========================================
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
    symbol = normalize_symbol(data.get("symbol", ""))
    side = data.get("side", "").lower()
    signal_type = data.get("type", "normal_long")
    entry_score = data.get("entry_score", 50)
    signal_price_raw = data.get('price')
    tp_pct = Decimal(str(data.get("tp_pct", "0.5"))) / 100
    sl_pct = Decimal(str(data.get("sl_pct", "4.0"))) / 100
    
    if not all([symbol, side, signal_price_raw]):
        return
    
    cfg = SYMBOL_CONFIG.get(symbol)
    if not cfg:
        return log_debug(f"⚠️ 진입 취소 ({symbol})", "알 수 없는 심볼")
        
    current_price = get_price(symbol)
    price_multiplier = PRICE_MULTIPLIERS.get(symbol, Decimal("1.0"))
    signal_price = Decimal(str(signal_price_raw)) / price_multiplier
    
    if current_price <= 0 or signal_price <= 0:
        return
        
    price_diff = abs(current_price - signal_price)
    price_diff_pct = abs(current_price - signal_price) / signal_price
    
    allowed_slippage_by_pct = signal_price * PRICE_DEVIATION_LIMIT_PCT
    allowed_slippage_by_ticks = Decimal(str(MAX_SLIPPAGE_TICKS)) * cfg['tick_size']
    max_allowed_slippage = max(allowed_slippage_by_pct, allowed_slippage_by_ticks)
    
    if price_diff > max_allowed_slippage:
        return log_debug(f"⚠️ 진입 취소: 슬리피지 ({symbol}_{side.upper()})", f"차이: {price_diff:.8f}, 허용: {max_allowed_slippage:.8f}")
        
    update_all_position_states()
    pos_side_state = position_state.get(symbol, {}).get(side, {})
    
    if pos_side_state.get("size", Decimal(0)) > 0 and "rescue" not in signal_type:
        avg_price = pos_side_state.get("price")
        if avg_price and ((side == "long" and current_price <= avg_price) or (side == "short" and current_price >= avg_price)):
            return log_debug(f"⚠️ 추가 진입 보류 ({symbol}_{side.upper()})", f"평단가 불리. 현재가: {current_price:.8f}, 평단가: {avg_price:.8f}")

    if pos_side_state.get("entry_count", 0) >= 10: return
    if "premium" in signal_type and pos_side_state.get("premium_entry_count", 0) >= 5: return
    if "normal" in signal_type and pos_side_state.get("normal_entry_count", 0) >= 5: return
    if "rescue" in signal_type and pos_side_state.get("rescue_entry_count", 0) >= 3: return
        
    current_signal_count = pos_side_state.get("premium_entry_count", 0) if "premium" in signal_type else pos_side_state.get("normal_entry_count", 0)
    qty = calculate_position_size(symbol, signal_type, entry_score, current_signal_count)
    
    final_position_ratio = Decimal("0")
    if "rescue" in signal_type:
        last_ratio = pos_side_state.get('last_entry_ratio', Decimal("5.0"))
        if last_ratio > 0:
            equity = get_total_collateral()
            contract_val = get_price(symbol) * cfg["contract_size"]
            rescue_ratio = last_ratio * Decimal("1.5")
            qty = max((equity * rescue_ratio / 100 / contract_val).quantize(Decimal('1'), rounding=ROUND_DOWN), cfg["min_qty"])
            final_position_ratio = rescue_ratio
    
    if qty > 0:
        entry_action = "추가진입" if pos_side_state.get("size", 0) > 0 else "첫진입"
        if place_order(symbol, side, qty, signal_type, final_position_ratio):
            update_all_position_states()
            latest_pos_side_state = position_state.get(symbol, {}).get(side, {})
            log_debug(f"✅ {entry_action} 성공 ({symbol}_{side.upper()})", f"{float(qty)} 계약 (총 #{latest_pos_side_state.get('entry_count',0)})")
            store_tp_sl(symbol, side, tp_pct, sl_pct, price_diff_pct, latest_pos_side_state.get("entry_count", 0))
        else:
            log_debug(f"❌ {entry_action} 실패 ({symbol}_{side.upper()})", "주문 실행 중 오류 발생")

# ========================================
# [수정] 15. 포지션 모니터링 및 메인 실행
# ========================================
def position_monitor():
    while True:
        time.sleep(30)
        try:
            update_all_position_states()
            total_value = Decimal("0")
            active_positions_log = []
            
            with position_lock:
                is_any_position_active = False
                for symbol, sides in position_state.items():
                    for side, pos_data in sides.items():
                        if pos_data and pos_data.get("size", Decimal("0")) > 0:
                            is_any_position_active = True
                            total_value += pos_data.get("value", Decimal("0"))
                            pyramid_info = f"총:{pos_data['entry_count']}/10,일:{pos_data['normal_entry_count']}/5,프:{pos_data['premium_entry_count']}/5,레:{pos_data['rescue_entry_count']}/3"
                            active_positions_log.append(f"{symbol}_{side.upper()}: {pos_data['size']:.4f} @ {pos_data['price']:.8f} ({pyramid_info}, 가치: {pos_data['value']:.2f} USDT)")
            
            if is_any_position_active:
                equity = get_total_collateral()
                exposure_pct = (total_value / equity * 100) if equity > 0 else 0
                log_debug("🚀 포지션 현황", f"활성: {len(active_positions_log)}개, 총가치: {total_value:.2f} USDT, 노출도: {exposure_pct:.1f}%")
                for pos_info in active_positions_log:
                    log_debug("  └", pos_info)
            else:
                log_debug("📊 포지션 현황 보고", "현재 활성 포지션이 없습니다.")
                
        except Exception as e:
            log_debug("❌ 포지션 모니터링 오류", str(e), exc_info=True)

if __name__ == "__main__": # [수정] **main** -> __main__
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.22 (Config Fix)")
    log_debug("🎯 전략 핵심", "독립 피라미딩 + 점수 기반 가중치 + 슬리피지 연동형 동적 TP + 레스큐 진입")
    log_debug("🛡️ 안전장치", f"동적 슬리피지 (비율 {PRICE_DEVIATION_LIMIT_PCT:.2%} 또는 {MAX_SLIPPAGE_TICKS}틱 중 큰 값)")
    log_debug("⚠️ 중요", "Gate.io 거래소 설정에서 '양방향 포지션 모드(Two-way)'가 활성화되어야 합니다.")
    initialize_states()
    
    log_debug("📊 초기 상태 로드", "현재 계좌의 모든 포지션 정보를 불러옵니다...")
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
    log_debug("💰 초기 자산 확인", f"전체 자산: {equity:.2f} USDT" if equity > 0 else "자산 조회 실패")
    
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"Flask 서버 0.0.0.0:{port}에서 실행 중")
    log_debug("✅ 준비 완료", "파인스크립트 v6.18 (Hedge) 연동 시스템 대기중")
    
    app.run(host="0.0.0.0", port=port, debug=False)

