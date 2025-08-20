#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.30 - 최종 완성 버전 (안정 버전 기반 완벽 재구성)
- 단방향 모드에서 안정적으로 작동하던 개별 심볼 조회 로직을 뼈대로 채택.
- 양방향 모드의 list_positions API를 위 구조에 완벽히 이식하여 포지션 인식 문제를 근본적으로 해결.
- 수동 포지션 및 유령 포지션 동기화 로직 강화.
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

# ========
# 1. 로깅 및 Flask 앱 설정
# ========
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
app = Flask(__name__)

def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception("")

# ========
# 2. API 및 기본 설정
# ========
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

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
# 4. 양방향 상태 관리
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

# ========
# 7. 양방향 TP/SL 관리
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
                return side_storage[entry_number].values()
            elif side_storage:
                return side_storage[max(side_storage.keys())].values()
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    return Decimal("0.005") * Decimal(str(cfg["tp_mult"])), Decimal("0.04") * Decimal(str(cfg["sl_mult"])), Decimal("0"), time.time()

# ========
# 8. 중복 신호 체크
# ========
def is_duplicate(data):
    with signal_lock:
        now = time.time()
        symbol = normalize_symbol(data.get('symbol',''))
        side = data.get('side', '').lower()
        if not symbol or not side: return False
        symbol_id = f"{symbol}_{side}"
        if symbol_id in recent_signals and (now - recent_signals[symbol_id] < COOLDOWN_SECONDS):
            return True
        recent_signals[symbol_id] = now
        recent_signals.update({k: v for k, v in recent_signals.items() if now - v < 300})
        return False

# ========
# 9. 수량 계산
# ========
def calculate_position_size(symbol, signal_type, entry_score=50, current_signal_count=0):
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    if equity <= 0 or price <= 0: return Decimal("0")
    
    entry_ratios = [Decimal("5.0"), Decimal("10.0"), Decimal("25.0"), Decimal("60.0"), Decimal("200.0")]
    current_ratio = entry_ratios[min(current_signal_count, len(entry_ratios) - 1)]
    
    signal_multiplier = get_signal_type_multiplier(signal_type)
    score_weight = get_entry_weight_from_score(entry_score)
    
    final_position_ratio = current_ratio * signal_multiplier * score_weight
    position_value = equity * (final_position_ratio / Decimal("100"))
    contract_value = price * cfg["contract_size"]
    if contract_value <= 0: return Decimal("0")
    
    calculated_qty = (position_value / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
    final_qty = max(calculated_qty, cfg["min_qty"])
    
    if final_qty * contract_value < cfg["min_notional"]:
        final_qty = (cfg["min_notional"] / contract_value / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
    
    return final_qty

# ========
# 10. 양방향 포지션 상태 관리 (최종 수정)
# ========
def update_all_position_states():
    with position_lock:
        active_positions_on_api = set()
        
        for symbol in list(SYMBOL_CONFIG.keys()):
            try:
                positions_for_symbol = _get_api_response(api.list_positions, settle=SETTLE, contract=symbol)
                
                if not positions_for_symbol:
                    continue

                for pos_info in positions_for_symbol:
                    side = 'long' if pos_info.mode == 'dual_long' else 'short' if pos_info.mode == 'dual_short' else None
                    if not side: continue

                    size = Decimal(str(pos_info.size))
                    if size <= 0: continue
                    
                    active_positions_on_api.add((symbol, side))

                    current_side_state = position_state.setdefault(symbol, {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()})[side]
                    
                    current_side_state["price"] = Decimal(str(pos_info.entry_price))
                    current_side_state["size"] = size
                    if pos_info.mark_price:
                        current_side_state["value"] = size * Decimal(str(pos_info.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"]

                    if current_side_state.get("entry_count", 0) == 0:
                        log_debug("🔄 수동 포지션 감지", f"{symbol} {side.upper()} 포지션을 상태에 추가합니다.")
                        current_side_state["entry_count"] = 1
                        current_side_state["entry_time"] = time.time()
            except Exception as e:
                log_debug(f"❌ 포지션 조회 오류 ({symbol})", str(e), exc_info=True)

        for symbol, sides in list(position_state.items()):
            for side in ["long", "short"]:
                if sides[side].get("size", Decimal("0")) > 0 and (symbol, side) not in active_positions_on_api:
                    log_debug(f"👻 유령 포지션 정리", f"API에 없는 {symbol} {side.upper()} 포지션을 메모리에서 삭제합니다.")
                    position_state[symbol][side] = get_default_pos_side_state()
                    if symbol in tpsl_storage and side in tpsl_storage[symbol]:
                        tpsl_storage[symbol][side].clear()

# ========
# 11. 양방향 주문 실행
# ========
def place_order(symbol, side, qty, signal_type, final_position_ratio=Decimal("0")):
    with position_lock:
        order = FuturesOrder(contract=symbol, size=float(qty), price="0", tif="ioc", dual_pos=side)
        if not _get_api_response(api.create_futures_order, SETTLE, order):
            return False
        
        pos_side_state = position_state.setdefault(symbol, {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()})[side]
        pos_side_state["entry_count"] += 1
        
        if "premium" in signal_type: pos_side_state["premium_entry_count"] += 1
        elif "normal" in signal_type: pos_side_state["normal_entry_count"] += 1
        elif "rescue" in signal_type: pos_side_state["rescue_entry_count"] += 1
            
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
        
        log_debug("🚪 포지션 청산", f"{symbol} {side.upper()} 청산 완료 (사유: {reason})")
        
        position_state.setdefault(symbol, {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()})[side] = get_default_pos_side_state()
        if symbol in tpsl_storage and side in tpsl_storage[symbol]:
            tpsl_storage[symbol][side].clear()
        if f"{symbol}_{side}" in recent_signals:
            del recent_signals[f"{symbol}_{side}"]
        return True

# ========
# 12. 웹훅 라우트 및 관리용 API
# ========
@app.route("/ping", methods=["GET", "HEAD"])
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
                    if pos_data and pos_data.get("size", Decimal("0")) > 0:
                        active_positions[f"{symbol}_{side.upper()}"] = {
                            "side": side, "size": float(pos_data["size"]), "price": float(pos_data.get("price", 0)),
                            "value": float(pos_data.get("value", 0)), "entry_count": pos_data.get("entry_count", 0)
                        }
        return jsonify({
            "status": "running", "version": "v6.30_final",
            "current_time_kst": datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            "balance_usdt": float(equity), "active_positions": active_positions,
            "queue_info": {"size": task_q.qsize(), "max_size": task_q.maxsize}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 오류", str(e), exc_info=True)
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
                log_debug(f"🔄 중복 신호 무시 ({symbol}_{side.upper()})", "쿨다운 내 동일 신호 감지")
                return jsonify({"status": "duplicate_ignored"}), 200
            task_q.put_nowait(data)
            return jsonify({"status": "queued"}), 200
        elif action == "exit":
            reason = data.get("reason", "").upper()
            if position_state.get(symbol, {}).get(side, {}).get("size", Decimal(0)) > 0:
                close_position(symbol, side, f"WEBHOOK_{reason}")
            return jsonify({"status": "exit_processed"}), 200
        return jsonify({"error": "Invalid action"}), 400
    except Exception as e:
        log_debug("❌ 웹훅 처리 중 예외", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ========
# 13. 양방향 웹소켓 모니터링
# ========
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
                        for item in result: check_tp_only(item)
                    elif isinstance(result, dict): check_tp_only(result)
        except Exception as e:
            log_debug("🔌 웹소켓 연결 문제", f"재연결 시도... ({type(e).__name__})")
            await asyncio.sleep(5)

def check_tp_only(ticker):
    try:
        symbol = ticker.get("contract")
        price = Decimal(str(ticker.get("last", "0")))
        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0: return
        with position_lock:
            for side in ["long", "short"]:
                pos = position_state.get(symbol, {}).get(side, {})
                if not pos or pos.get("size", Decimal(0)) <= 0: continue
                
                entry_price = pos.get("price")
                entry_count = pos.get("entry_count")
                if not all([entry_price, entry_count]): continue
                    
                tp_pct, _, _, _ = get_tp_sl(symbol, side, entry_count)
                
                if side == "long" and price >= entry_price * (1 + tp_pct):
                    log_debug(f"🎯 롱 TP 트리거 ({symbol})", f"현재가: {price}, TP가: {entry_price * (1 + tp_pct)}")
                    close_position(symbol, "long", "TP")
                elif side == "short" and price <= entry_price * (1 - tp_pct):
                    log_debug(f"🎯 숏 TP 트리거 ({symbol})", f"현재가: {price}, TP가: {entry_price * (1 - tp_pct)}")
                    close_position(symbol, "short", "TP")
    except Exception as e:
        log_debug(f"❌ TP 체크 오류 ({ticker.get('contract', 'Unknown')})", str(e), exc_info=True)

# ========
# 14. 양방향 진입 처리 로직
# ========
def worker(idx):
    while True:
        try:
            data = task_q.get(timeout=1)
            handle_entry(data)
            task_q.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            log_debug(f"❌ 워커-{idx} 오류", f"작업 처리 중 예외: {str(e)}", exc_info=True)

def handle_entry(data):
    symbol = normalize_symbol(data.get("symbol"))
    side = data.get("side", "").lower()
    if not all([symbol, side]): return
    
    update_all_position_states()
    pos_side_state = position_state.get(symbol, {}).get(side, {})
    
    # 여기서부터는 기존 로직을 대부분 유지. 필요 시 수정.
    base_type = data.get("type", "normal")
    signal_type = f"{base_type}_{side}"
    entry_score = data.get("entry_score", 50)
    
    entry_limits = {"premium": 5, "normal": 5, "rescue": 3}
    total_entry_limit = 10
    entry_type_key = next((k for k in entry_limits if k in signal_type), "normal")
    
    if pos_side_state.get("entry_count", 0) >= total_entry_limit or \
       pos_side_state.get(f"{entry_type_key}_entry_count", 0) >= entry_limits[entry_type_key]:
        log_debug(f"⚠️ 진입 제한 ({symbol}_{side.upper()})", "최대 진입 횟수 도달")
        return

    qty = calculate_position_size(symbol, signal_type, entry_score, pos_side_state.get("entry_count", 0))
    if qty > 0:
        place_order(symbol, side, qty, signal_type)

# ========
# 15. 포지션 모니터링 및 메인 실행
# ========
def position_monitor():
    while True:
        time.sleep(30)
        try:
            update_all_position_states()
            log_debug("📊 포지션 현황 보고", "주기적 상태 업데이트 완료")
        except Exception as e:
            log_debug("❌ 포지션 모니터링 오류", str(e), exc_info=True)

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "Gate.io 자동매매 서버 v6.30 (Final)")
    initialize_states()
    log_debug("📊 초기 상태 로드", "현재 계좌의 모든 포지션 정보를 불러옵니다...")
    update_all_position_states() 
    
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"Flask 서버 0.0.0.0:{port}에서 실행 중")
    app.run(host="0.0.0.0", port=port, debug=False)
