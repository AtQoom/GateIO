#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버 v6.12 - PineScript "심볼별 가중치 단타 전략 v6.12" 완전 호환

주요 기능:
1. 5단계 피라미딩 (0.2%→0.4%→1.2%→4.8%→9.6%)
2. 손절직전 진입 (SL_Rescue) - 150% 가중치, 최대 3회
3. 단계별 TP/SL 설정 및 시간 경과에 따른 실시간 감소
4. 심볼별 가중치 적용 (BTC 0.5, ETH 0.6, SOL 0.8, PEPE/DOGE 1.2 등)
5. 메인/엔걸핑/SL-Rescue 신호 타입에 따른 차등 처리
"""

import os
import json
import time
import queue
import asyncio
import threading
import logging
import signal
import sys
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
import pytz
import websockets
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

# ========================================
# 1. 로깅 및 기본 설정
# ========================================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)

# API 설정 (환경 변수에서 로드)
API_KEY = os.environ.get("API_KEY", "YOUR_API_KEY")
API_SECRET = os.environ.get("API_SECRET", "YOUR_API_SECRET")
SETTLE = "usdt"

# Gate.io API 클라이언트
config = Configuration(key=API_KEY, secret=API_SECRET)
api = FuturesApi(ApiClient(config))

# ========================================
# 2. PineScript v6.12 전략 상수
# ========================================
COOLDOWN_SECONDS = 10  # 사용자 요청에 따라 10초로 설정 (PineScript는 15초)

# 5단계 진입 비율 (%)
ENTRY_RATIOS = [Decimal(x) for x in ("0.20 0.40 1.2 4.8 9.6".split())]
# 5단계 TP/SL 레벨 (비율)
TP_LEVELS = [Decimal(x) for x in ("0.005 0.0035 0.003 0.002 0.0015".split())]
SL_LEVELS = [Decimal(x) for x in ("0.04 0.038 0.035 0.033 0.03".split())]

# 심볼 설정 (가중치, 계약 크기 등)
SYMBOL_CONFIG = {
    "BTC_USDT":  {"contract_size": Decimal("0.0001"), "min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("0.5"), "min_notional": Decimal("5")},
    "ETH_USDT":  {"contract_size": Decimal("0.01"),   "min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("0.6"), "min_notional": Decimal("5")},
    "SOL_USDT":  {"contract_size": Decimal("1"),      "min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("0.8"), "min_notional": Decimal("5")},
    "PEPE_USDT": {"contract_size": Decimal("10000000"),"min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("1.2"), "min_notional": Decimal("5")},
    "DOGE_USDT": {"contract_size": Decimal("10"),     "min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("1.2"), "min_notional": Decimal("5")},
    "ADA_USDT":  {"contract_size": Decimal("10"),     "min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("1.0"), "min_notional": Decimal("5")},
    "SUI_USDT":  {"contract_size": Decimal("1"),      "min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("1.0"), "min_notional": Decimal("5")},
    "LINK_USDT": {"contract_size": Decimal("1"),      "min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("1.0"), "min_notional": Decimal("5")},
    "XRP_USDT":  {"contract_size": Decimal("10"),     "min_qty": Decimal("1"), "qty_step": Decimal("1"), "weight": Decimal("1.0"), "min_notional": Decimal("5")},
}

# 심볼 정규화 매핑 (모든 종류의 심볼 이름 지원)
MAP = {k.replace("_USDT", ""): k for k in SYMBOL_CONFIG}
MAP.update({s: s for s in SYMBOL_CONFIG})
MAP.update({s.replace("_USDT", "USDT"): s for s in SYMBOL_CONFIG})
MAP.update({s.replace("_USDT", "USDT.P"): s for s in SYMBOL_CONFIG})
MAP.update({s.replace("_USDT", "USDTPERP"): s for s in SYMBOL_CONFIG})

# ========================================
# 3. 전역 상태 및 동기화
# ========================================
position_state = {}
tpsl_storage = {}
recent_signals = {}
account_cache = {"time": 0, "data": None}

pos_lock = threading.RLock()
sig_lock = threading.RLock()
tpsl_lock = threading.RLock()

task_q = queue.Queue(maxsize=100)
WORKER_COUNT = max(2, min(6, os.cpu_count() or 2))
KST = pytz.timezone('Asia/Seoul')
shutdown_event = threading.Event()

# ========================================
# 4. 핵심 유틸리티 함수
# ========================================
def normalize_symbol(raw_symbol):
    return MAP.get(str(raw_symbol).upper().strip())

def get_total_collateral(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 30 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        total = Decimal(str(acc.total))
        account_cache.update({"time": now, "data": total})
        return total
    except Exception as e:
        logger.error(f"[자산 조회 실패] {e}")
        return Decimal("0")

def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        return Decimal(str(ticker[0].last)) if ticker else Decimal("0")
    except Exception as e:
        logger.warning(f"[가격 조회 실패] {symbol}: {e}")
        return Decimal("0")

def is_duplicate_signal(data):
    with sig_lock:
        now = time.time()
        signal_id = data.get("id", "")
        key = f"{data['symbol']}_{data['side']}"
        
        if signal_id and signal_id in recent_signals and now - recent_signals[signal_id] < 5:
            return True
        if key in recent_signals and now - recent_signals[key] < COOLDOWN_SECONDS:
            return True
            
        recent_signals[key] = now
        if signal_id:
            recent_signals[signal_id] = now
        
        for k in list(recent_signals.keys()):
            if now - recent_signals[k] > 300:
                del recent_signals[k]
        return False

# ========================================
# 5. TP/SL 관리 (PineScript v6.12 완벽 호환)
# ========================================
def store_tp_sl(symbol, entry_number):
    with tpsl_lock:
        idx = min(entry_number - 1, len(TP_LEVELS) - 1)
        weight = SYMBOL_CONFIG[symbol]["weight"]
        
        base_tp = TP_LEVELS[idx] * weight
        base_sl = SL_LEVELS[idx] * weight
        
        tpsl_storage.setdefault(symbol, {})[entry_number] = {
            "base_tp": base_tp, "base_sl": base_sl, "entry_time": time.time()
        }
        logger.info(f"[TP/SL 저장] {symbol} #{entry_number}: TP={base_tp:.4%}, SL={base_sl:.4%}")

def get_tp_sl_with_decay(symbol):
    with tpsl_lock:
        pos = position_state.get(symbol, {})
        entry_count = pos.get("entry_count", 0)
        if not entry_count or symbol not in tpsl_storage or entry_count not in tpsl_storage[symbol]:
            weight = SYMBOL_CONFIG.get(symbol, {}).get("weight", Decimal("1.0"))
            return Decimal("0.006") * weight, Decimal("0.04") * weight, time.time()
        
        data = tpsl_storage[symbol][entry_count]

    seconds_passed = time.time() - data["entry_time"]
    periods = int(seconds_passed / 15)
    weight = SYMBOL_CONFIG[symbol]["weight"]
    
    tp = max(Decimal("0.0012"), data["base_tp"] - (Decimal("0.00002") * weight * periods))
    sl = max(Decimal("0.0009"), data["base_sl"] - (Decimal("0.00004") * weight * periods))
    
    return tp, sl, data["entry_time"]

def clear_tp_sl(symbol):
    with tpsl_lock:
        tpsl_storage.pop(symbol, None)

# ========================================
# 6. 포지션 관리 및 조건 체크
# ========================================
def update_position_state(symbol):
    with pos_lock:
        try:
            p = api.get_position(SETTLE, symbol)
            size = Decimal(str(p.size))
            if size != 0:
                existing_state = position_state.get(symbol, {})
                position_state[symbol] = {
                    "price": Decimal(str(p.entry_price)),
                    "side": "buy" if size > 0 else "sell",
                    "size": abs(size),
                    "entry_count": existing_state.get("entry_count", 0),
                    "sl_entry_count": existing_state.get("sl_entry_count", 0),
                    "entry_time": existing_state.get("entry_time", time.time())
                }
            else:
                position_state[symbol] = {"entry_count": 0, "sl_entry_count": 0, "size": Decimal("0")}
                clear_tp_sl(symbol)
        except Exception:
            position_state[symbol] = {"entry_count": 0, "sl_entry_count": 0, "size": Decimal("0")}

def is_sl_rescue_condition(symbol):
    with pos_lock:
        pos = position_state.get(symbol)
        if not pos or pos.get("size", 0) == 0 or pos.get("entry_count", 0) >= 5 or pos.get("sl_entry_count", 0) >= 3:
            return False
        
        current_price = get_price(symbol)
        if current_price <= 0: return False
        
        avg_price = pos["price"]
        side = pos["side"]
        _, sl_pct, _ = get_tp_sl_with_decay(symbol)
        
        sl_price = avg_price * (1 - sl_pct) if side == "buy" else avg_price * (1 + sl_pct)
        is_near_sl = abs(current_price - sl_price) / sl_price <= Decimal("0.0005")
        is_underwater = (side == "buy" and current_price < avg_price) or (side == "sell" and current_price > avg_price)
        
        if is_near_sl and is_underwater:
            logger.info(f"[SL-Rescue 조건] {symbol} 충족: SL가 근접 및 평가손 상태")
        return is_near_sl and is_underwater

def close_position(symbol, reason="manual"):
    with pos_lock:
        try:
            api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, tif="ioc", close=True))
            logger.info(f"[청산 완료] {symbol} (이유: {reason})")
            position_state[symbol] = {"entry_count": 0, "sl_entry_count": 0, "size": Decimal("0")}
            clear_tp_sl(symbol)
            return True
        except Exception as e:
            logger.error(f"[청산 실패] {symbol}: {e}")
            return False

# ========================================
# 7. 수량 계산 및 주문 실행
# ========================================
def calculate_position_size(symbol, is_sl_rescue):
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    if equity <= 0 or price <= 0: return Decimal("0")
    
    entry_count = position_state.get(symbol, {}).get("entry_count", 0)
    if entry_count >= 5: return Decimal("0")
    
    ratio = ENTRY_RATIOS[entry_count] / Decimal("100")
    position_value = equity * ratio
    
    if is_sl_rescue:
        position_value *= Decimal("1.5")
    
    raw_qty = position_value / (price * cfg["contract_size"])
    qty_adjusted = (raw_qty / cfg["qty_step"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * cfg["qty_step"]
    
    if qty_adjusted < cfg["min_qty"] or (qty_adjusted * price * cfg["contract_size"]) < cfg.get("min_notional", Decimal("5")):
        return Decimal("0")
    return qty_adjusted

def place_order(symbol, side, qty, is_sl_rescue):
    with pos_lock:
        try:
            size = float(qty) if side == "buy" else -float(qty)
            api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=size, tif="ioc"))
            
            current_state = position_state.get(symbol, {"entry_count": 0, "sl_entry_count": 0})
            new_entry_count = current_state["entry_count"] + 1
            position_state[symbol] = {
                **current_state,
                "entry_count": new_entry_count,
                "sl_entry_count": current_state.get("sl_entry_count", 0) + (1 if is_sl_rescue else 0),
            }
            if not is_sl_rescue:
                store_tp_sl(symbol, new_entry_count)
            
            logger.info(f"[주문 성공] {symbol} {side.upper()} {qty} (진입 #{new_entry_count}/5, SL-Rescue: {is_sl_rescue})")
            time.sleep(2)
            update_position_state(symbol)
            return True
        except Exception as e:
            logger.error(f"[주문 실패] {symbol}: {e}")
            return False

# ========================================
# 8. 워커 및 웹훅 처리
# ========================================
def handle_entry(data):
    symbol = normalize_symbol(data["symbol"])
    if not symbol: return
    
    update_position_state(symbol)
    current_side = position_state.get(symbol, {}).get("side")
    desired_side = "buy" if data["side"] == "long" else "sell"
    
    if current_side and current_side != desired_side:
        if not close_position(symbol, "reverse"): return
        time.sleep(1)
        update_position_state(symbol)

    is_sl_rescue = data.get("signal") == "sl_rescue" and is_sl_rescue_condition(symbol)
    qty = calculate_position_size(symbol, is_sl_rescue)
    if qty > 0:
        place_order(symbol, desired_side, qty, is_sl_rescue)

# 🔧 수정된 워커 함수
def worker(idx):
    """워커 스레드: 큐에서 작업을 가져와 처리"""
    while not shutdown_event.is_set():
        try:
            # 1. 큐에서 작업 가져오기 (타임아웃 시 queue.Empty 발생)
            data = task_q.get(timeout=1)
        except queue.Empty:
            # 큐가 비어있으면 다음 루프로 넘어감
            continue

        try:
            # 2. 가져온 작업 처리
            handle_entry(data)
        except Exception as e:
            logger.error(f"[Worker-{idx} 작업 처리 오류] 데이터: {data}, 오류: {e}")
        finally:
            # 3. 작업이 성공하든 실패하든, 가져온 작업은 완료 처리
            task_q.task_done()

# ========================================
# 9. 실시간 모니터링 (WebSocket)
# ========================================
async def price_monitor():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"time": int(time.time()), "channel": "futures.tickers", "event": "subscribe", "payload": symbols}))
                logger.info("[WebSocket] 구독 완료")
                while not shutdown_event.is_set():
                    msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    data = json.loads(msg)
                    if data.get("event") == "update" and isinstance(data.get("result"), list):
                        for item in data["result"]:
                            check_tp_sl(item)
        except Exception as e:
            if not shutdown_event.is_set():
                logger.error(f"[WebSocket 오류] {e}, 5초 후 재연결")
                await asyncio.sleep(5)

def check_tp_sl(ticker):
    symbol = ticker.get("contract")
    price = Decimal(str(ticker.get("last", "0")))
    if not symbol or price <= 0: return

    with pos_lock:
        pos = position_state.get(symbol)
        if not pos or pos.get("size", 0) == 0: return
        avg_price = pos["price"]
        side = pos["side"]
        
    tp_pct, sl_pct, _ = get_tp_sl_with_decay(symbol)
    tp_price = avg_price * (1 + tp_pct) if side == "buy" else avg_price * (1 - tp_pct)
    sl_price = avg_price * (1 - sl_pct) if side == "buy" else avg_price * (1 + sl_pct)
    
    if (side == "buy" and price >= tp_price) or (side == "sell" and price <= tp_price):
        close_position(symbol, "TP")
    elif (side == "buy" and price <= sl_price) or (side == "sell" and price >= sl_price):
        close_position(symbol, "SL")

# ========================================
# 10. Flask 웹 서버
# ========================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data: return jsonify({"error": "No data"}), 400
        
        symbol_name = data.get("symbol")
        if not normalize_symbol(symbol_name): return jsonify({"error": f"Invalid symbol: {symbol_name}"}), 400
        
        action = data.get("action", "").lower()
        if action == "entry":
            if is_duplicate_signal(data): return jsonify({"status": "duplicate_ignored"}), 200
            task_q.put_nowait(data)
            return jsonify({"status": "queued", "queue_size": task_q.qsize()}), 200
        elif action == "exit":
            if data.get("reason") in ["TP", "SL"]: return jsonify({"status": "ignored", "reason": "server_handled"}), 200
            close_position(normalize_symbol(symbol_name), data.get("reason", "signal"))
            return jsonify({"status": "exit_triggered"}), 200
        return jsonify({"error": "Invalid action"}), 400
    except queue.Full: return jsonify({"error": "Queue full"}), 429
    except Exception as e: logger.error(f"[웹훅 오류] {e}"); return jsonify({"error": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status": "running", "version": "v6.12",
        "balance": float(get_total_collateral(True)),
        "positions": {s: {k: str(v) if isinstance(v, Decimal) else v for k, v in d.items()} 
                      for s, d in position_state.items() if d.get("size", 0) > 0},
        "queue_size": task_q.qsize(),
    })

# ========================================
# 11. 메인 실행 및 종료 처리
# ========================================
def shutdown_handler(signum, frame):
    logger.info("종료 신호 수신, 정리 중...")
    shutdown_event.set()
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logger.info("서버 시작 (v6.12 - PineScript 완전 호환)")
    for sym in SYMBOL_CONFIG: update_position_state(sym)
    
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True, name=f"Worker-{i}").start()
    
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True, name="PriceMonitor").start()
    
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Flask 웹 서버 실행 (Port: {port})")
    app.run(host="0.0.0.0", port=port, debug=False)
