#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import time
import asyncio
import threading
import queue
import logging
from decimal import Decimal, ROUND_DOWN
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi
from gate_api import exceptions as gate_api_exceptions
import websockets

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 5
SETTLE = "usdt"
WORKER_COUNT = 3

PRICE_DEVIATION_LIMIT_PCT = 0.01
MAX_SLIPPAGE_TICKS = 10
PREMIUM_TP_MULTIPLIERS = {
    "first_entry": Decimal("1.8"),
    "after_normal": Decimal("1.2"),
    "after_premium": Decimal("1.0")
}
ws_last_payload = []
ws_last_subscribed_at = 0
tpsl_storage = {}

# API 키 & 초기화
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
if not API_KEY or not API_SECRET:
    logger.critical("API 키 미설정")
    exit(1)

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# 글로벌 상태, 큐
task_q = queue.Queue(maxsize=200)
position_lock = threading.RLock()
position_state = {}
dynamic_symbols = set()
latest_prices = {}

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
    "ONDO_USDT":{"min_qty":Decimal("1"),"qty_step":Decimal("1"),"contract_size":Decimal("1"),"min_notional":Decimal("5"),"tp_mult":1.0,"sl_mult":1.0,"tick_size":Decimal("0.0001")},
}
  
def get_symbol_config(symbol):
    if symbol in SYMBOL_CONFIG:
        return SYMBOL_CONFIG[symbol]
    log_debug(f"⚠️ 누락된 심볼 설정 ({symbol})", "기본값으로 진행")
    SYMBOL_CONFIG[symbol] = DEFAULT_SYMBOL_CONFIG.copy()
    return SYMBOL_CONFIG[symbol]

def normalize_symbol(raw):
    if not raw:
        return None
    raw = raw.upper().strip()
    return SYMBOL_MAPPING.get(raw, None)

def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception("")

def get_obv_macd_value():
    # TODO: 외부 데이터 연동
    return 30.0

def get_base_qty():
    """그리드 전략 기본 수량"""
    return Decimal("2.0")

def calculate_grid_qty(base_qty: Decimal, obv_macd_val: float) -> Decimal:
    """
    그리드 전용 수량 계산 (양방향 동일)
    OBV MACD 절대값 기준으로 가중치 적용
    """
    ratio = 1.0
    abs_val = abs(obv_macd_val)
    
    if abs_val >= 20 and abs_val < 30:
        ratio = 2.1
    elif abs_val >= 30 and abs_val < 40:
        ratio = 2.2
    elif abs_val >= 40 and abs_val < 50:
        ratio = 2.3
    elif abs_val >= 50 and abs_val < 60:
        ratio = 2.4
    elif abs_val >= 60 and abs_val < 70:
        ratio = 2.5
    elif abs_val >= 70 and abs_val < 80:
        ratio = 2.6
    elif abs_val >= 80 and abs_val < 90:
        ratio = 2.7
    elif abs_val >= 90 and abs_val < 100:
        ratio = 2.8
    elif abs_val >= 100 and abs_val < 110:
        ratio = 2.9
    elif abs_val >= 110:
        ratio = 3.0
    
    return base_qty * Decimal(str(ratio))

def place_order(symbol, side, qty: Decimal, price: Decimal, wait_for_fill=True):
    """
    주문 발주
    wait_for_fill=True: 체결 확인 (기존 전략)
    wait_for_fill=False: 주문 발주만 확인 (그리드 전략)
    """
    with position_lock:
        try:
            order_size = qty.quantize(Decimal("1"), rounding=ROUND_DOWN)
            if side == "short":
                order_size = -order_size
            
            order = FuturesOrder(contract=symbol, size=int(order_size), price=str(price), tif="gtc")
            result = api.create_futures_order(SETTLE, order)
            
            if not result:
                log_debug("❌ 주문 실패", f"{symbol}_{side}")
                return False
            
            log_debug("✅ 주문 발주", f"{symbol}_{side} price={price} qty={qty}")
            
            if not wait_for_fill:
                return True
            
            update_all_position_states()
            original_size = position_state.get(symbol, {}).get(side, {}).get("size", Decimal("0"))
            
            for _ in range(15):
                time.sleep(1)
                update_all_position_states()
                new_size = position_state.get(symbol, {}).get(side, {}).get("size", Decimal("0"))
                if new_size > original_size:
                    return True
            
            log_debug("⚠️ 체결 대기 타임아웃", f"{symbol}_{side}")
            return True
            
        except Exception as ex:
            log_debug("❌ 주문에러", str(ex), exc_info=True)
            return False

def place_tp_sl_order(symbol, side, entry_price, qty, tp_pct, sl_pct):
    """
    익절/손절 지정가 주문 발주 (기존 전략 전용)
    """
    try:
        if side == "long":
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)
            close_size = -abs(int(qty))
        else:
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)
            close_size = abs(int(qty))
        
        try:
            tp_order = FuturesOrder(
                contract=symbol, 
                size=close_size, 
                price=str(tp_price), 
                tif="gtc", 
                reduce_only=True
            )
            tp_result = api.create_futures_order(SETTLE, tp_order)
            if tp_result:
                log_debug("✅ TP 주문", f"{symbol}_{side.upper()} TP:{tp_price}")
        except Exception as e:
            log_debug("❌ TP 주문 오류", f"{symbol}_{side.upper()}: {str(e)}")
        
        try:
            sl_order = FuturesOrder(
                contract=symbol, 
                size=close_size, 
                price=str(sl_price), 
                tif="gtc", 
                reduce_only=True
            )
            sl_result = api.create_futures_order(SETTLE, sl_order)
            if sl_result:
                log_debug("✅ SL 주문", f"{symbol}_{side.upper()} SL:{sl_price}")
        except Exception as e:
            log_debug("❌ SL 주문 오류", f"{symbol}_{side.upper()}: {str(e)}")
            
    except Exception as e:
        log_debug("❌ TP/SL 계산 오류", f"{symbol}_{side}: {str(e)}")

def update_position_state(symbol, side, price, qty):
    with position_lock:
        if symbol not in position_state:
            position_state[symbol] = {"long": {"price": None, "size": Decimal("0")}, "short": {"price": None, "size": Decimal("0")}}
        pos = position_state[symbol][side]
        current_size = pos["size"]
        current_price = pos["price"] or Decimal("0")
        new_size = current_size + qty
        if new_size == 0:
            pos["price"] = None
            pos["size"] = Decimal("0")
        else:
            avg_price = ((current_price * current_size) + (price * qty)) / new_size
            pos["price"] = avg_price
            pos["size"] = new_size

def handle_grid_entry(data):
    symbol = normalize_symbol(data.get("symbol"))
    if symbol != "ETH_USDT":
        return
    
    side = data.get("side", "").lower()
    price = Decimal(str(data.get("price", "0")))
    
    # ⭐ OBV MACD 기반 동적 수량
    obv_macd_val = get_obv_macd_value()
    base_qty = get_base_qty()
    qty = calculate_grid_qty(base_qty, obv_macd_val)
    
    if qty < Decimal('1'):
        return

    success = place_order(symbol, side, qty, price, wait_for_fill=False)
    if success:
        log_debug("📈 그리드 주문", f"{symbol} {side} qty={qty} price={price} (OBV:{obv_macd_val})")

def handle_entry(data):
    """기존 전략 진입 처리 - ETH_USDT 제외"""
    symbol = normalize_symbol(data.get("symbol"))
    if symbol == "ETH_USDT":
        return
    
    side = data.get("side", "").lower()
    signal_type = data.get("type", "normal")
    entry_score = data.get("entry_score", 50)
    tv_tp_pct = Decimal(str(data.get("tp_pct", "0"))) / 100
    tv_sl_pct = Decimal(str(data.get("sl_pct", "0"))) / 100
    
    if not all([symbol, side, data.get('price'), tv_tp_pct > 0, tv_sl_pct > 0]):
        log_debug("❌ 진입 불가", f"필수 정보 누락: {data}")
        return
    
    cfg = get_symbol_config(symbol)
    current_price = get_price(symbol)
    if current_price <= 0:
        log_debug("⚠️ 진입 취소", f"{symbol} 가격 정보 없음")
        return

    signal_price = Decimal(str(data['price'])) / cfg.get("price_multiplier", Decimal("1.0"))
    
    update_all_position_states()
    state = position_state[symbol][side]

    if state["entry_count"] == 0 and abs(current_price - signal_price) > max(signal_price * PRICE_DEVIATION_LIMIT_PCT, Decimal(str(MAX_SLIPPAGE_TICKS)) * cfg['tick_size']):
        log_debug("⚠️ 첫 진입 취소", "슬리피지 큼")
        return
    
    entry_limits = {"premium": 5, "normal": 5, "rescue": 3}
    base_type = signal_type.replace("_long", "").replace("_short", "")
    if state["entry_count"] >= sum(entry_limits.values()) or state.get(f"{base_type}_entry_count", 0) >= entry_limits.get(base_type, 99):
        log_debug("⚠️ 추가 진입 제한", f"{symbol} {side} 최고 횟수 도달")
        return

    current_signal_count = state.get(f"{base_type}_entry_count", 0) if "rescue" not in signal_type else 0
    qty, final_ratio = calculate_position_size(symbol, f"{base_type}_{side}", entry_score, current_signal_count)
    
    if qty > 0 and place_order(symbol, side, qty, signal_price, wait_for_fill=True):
        state["entry_count"] += 1
        state[f"{base_type}_entry_count"] = current_signal_count + 1
        state["entry_time"] = time.time()
        
        if "rescue" not in signal_type:
            state["last_entry_ratio"] = final_ratio
        
        if base_type == "premium":
            if state["premium_entry_count"] == 1 and state["normal_entry_count"] == 0:
                new_mul = PREMIUM_TP_MULTIPLIERS["first_entry"]
            elif state["premium_entry_count"] == 1 and state["normal_entry_count"] > 0:
                new_mul = PREMIUM_TP_MULTIPLIERS["after_normal"]
            else:
                new_mul = PREMIUM_TP_MULTIPLIERS["after_premium"]
            cur_mul = state.get("premium_tp_multiplier", Decimal("1.0"))
            state["premium_tp_multiplier"] = min(cur_mul, new_mul) if cur_mul > Decimal("1.0") else new_mul
            log_debug("✨ 프리미엄 TP 배수 적용", f"{symbol} {side.upper()}={state['premium_tp_multiplier']}")
        elif state["entry_count"] == 1:
            state["premium_tp_multiplier"] = Decimal("1.0")
        
        store_tp_sl(symbol, side, tv_tp_pct, tv_sl_pct, state["entry_count"])
        log_debug("✅ 진입 성공", f"{symbol} {side} qty={float(qty)} 진입 횟수={state['entry_count']}")

def update_all_position_states():
    """Gate.io API로 포지션 최신화"""
    with position_lock:
        api_positions = _get_api_response(api.list_positions, settle=SETTLE)
        if api_positions is None:
            return
        
        cleared = set()
        for symbol, sides in position_state.items():
            for side in ["long", "short"]:
                if sides[side]["size"] > 0:
                    cleared.add((symbol, side))
        
        for pos in api_positions:
            pos_size = Decimal(str(pos.size))
            if pos_size == 0:
                continue
            symbol = normalize_symbol(pos.contract)
            if not symbol:
                continue
            
            if symbol not in position_state:
                position_state[symbol] = {"long": get_default_pos_side_state(), "short": get_default_pos_side_state()}
            
            cfg = get_symbol_config(symbol)
            side = "long" if pos_size > 0 else "short"
            cleared.discard((symbol, side))
            
            state = position_state[symbol][side]
            old_size = state["size"]
            state.update({
                "price": Decimal(str(pos.entry_price)),
                "size": abs(pos_size),
                "value": abs(pos_size) * Decimal(str(pos.mark_price)) * cfg["contract_size"]
            })

        for symbol, side in cleared:
            position_state[symbol][side] = get_default_pos_side_state()

def initialize_grid_orders():
    """ETH 그리드 최초 주문"""
    symbol = "ETH_USDT"
    gap_pct = Decimal("0.15") / Decimal("100")
    current_price = Decimal(str(get_price(symbol)))

    up_price = current_price * (1 + gap_pct)
    down_price = current_price * (1 - gap_pct)

    handle_grid_entry({"symbol": symbol, "side": "short", "price": str(up_price)})
    handle_grid_entry({"symbol": symbol, "side": "long", "price": str(down_price)})
    log_debug("🎯 그리드 초기화", f"ETH 양방향 주문 완료 (고정수량: {get_base_qty()})")

def on_grid_fill_event(symbol, fill_price):
    """
    체결 발생 시 양방향 그리드 재배치
    - 고정 수량 사용
    """
    gap_pct = Decimal("0.15") / Decimal("100")
    
    # ⭐ 그리드는 항상 고정 수량
    qty = get_base_qty()

    up_price = Decimal(str(fill_price)) * (1 + gap_pct)
    down_price = Decimal(str(fill_price)) * (1 - gap_pct)

    log_debug("🔄 그리드 재배치", f"{symbol} 체결가:{fill_price} 수량:{qty}")
    handle_grid_entry({"symbol": symbol, "side": "short", "price": str(up_price)})
    handle_grid_entry({"symbol": symbol, "side": "long", "price": str(down_price)})

def get_price(symbol):
    """실시간 WebSocket 가격 반환"""
    price = latest_prices.get(symbol, Decimal("0"))
    if price > 0:
        return price
    
    try:
        ticker = api.list_futures_tickers(settle=SETTLE, contract=symbol)
        if ticker and len(ticker) > 0:
            return Decimal(str(ticker[0].last))
    except Exception as e:
        log_debug("⚠️ 가격 조회 실패", f"{symbol}: {e}")
    
    return Decimal("2000.0")

def get_total_collateral(force=False):
    try:
        account_info = api.list_futures_accounts(settle='usdt')
        return float(getattr(account_info, "total", 0))
    except Exception as ex:
        log_debug("❌ USDT 잔고 조회 오류", str(ex))
        return 0.0

def _get_api_response(api_func, *args, **kwargs):
    try:
        return api_func(*args, **kwargs)
    except Exception as e:
        log_debug("❌ API 호출 오류", str(e), exc_info=True)
        return None

def is_duplicate(data):
    return False

def calculate_position_size(symbol, entry_type, entry_score, current_signal_count):
    return Decimal("1"), Decimal("1")

def store_tp_sl(symbol, side, tp_pct, sl_pct, entry_count):
    pass

def set_manual_close_protection(symbol, side, duration):
    pass

def is_manual_close_protected(symbol, side):
    return False

def eth_grid_fill_monitor():
    """
    ETH 체결 감지 (TP/SL 없음 - 순수 그리드)
    """
    prev_long_size = Decimal("0")
    prev_short_size = Decimal("0")
    
    while True:
        time.sleep(2)
        update_all_position_states()
        
        with position_lock:
            pos = position_state.get("ETH_USDT", {})
            long_size = pos.get("long", {}).get("size", Decimal("0"))
            short_size = pos.get("short", {}).get("size", Decimal("0"))
            long_price = pos.get("long", {}).get("price", Decimal("0"))
            short_price = pos.get("short", {}).get("price", Decimal("0"))
            
            # 롱 체결 감지
            if long_size > prev_long_size:
                log_debug("✅ 롱 체결", f"ETH {long_price} (+{long_size - prev_long_size})")
                on_grid_fill_event("ETH_USDT", long_price)
            
            # 숏 체결 감지
            if short_size > prev_short_size:
                log_debug("✅ 숏 체결", f"ETH {short_price} (+{short_size - prev_short_size})")
                on_grid_fill_event("ETH_USDT", short_price)
            
            prev_long_size = long_size
            prev_short_size = short_size

def position_monitor():
    """전체 심볼 포지션 주기적 갱신"""
    while True:
        time.sleep(30)
        try:
            update_all_position_states()
        except Exception as e:
            log_debug("❌ 모니터링 오류", str(e), exc_info=True)

app = Flask(__name__)

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
        
        active_positions = {}
        with position_lock:
            for symbol, sides in position_state.items():
                for side, pos_data in sides.items():
                    if pos_data["size"] != 0:
                        active_positions[f"{symbol}_{side.upper()}"] = {
                            "size": float(pos_data["size"]),
                            "price": float(pos_data["price"]),
                            "value": float(abs(pos_data["value"])),
                            "entry_count": pos_data["entry_count"]
                        }
        
        return jsonify({
            "status": "running",
            "version": "v6.60-grid-fixed-qty",
            "balance_usdt": float(equity),
            "active_positions": active_positions,
            "grid_base_qty": float(get_base_qty())
        })
    
    except Exception as e:
        log_debug("❌ status 오류", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

async def price_monitor():
    global ws_last_payload, ws_last_subscribed_at, latest_prices
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                log_debug("🔌 웹소켓 연결", uri)
                last_resubscribe_time = 0
                last_app_ping_time = 0
                subscribed_symbols = set()

                while True:
                    if time.time() - last_resubscribe_time > 60:
                        base_symbols = set(SYMBOL_CONFIG.keys())
                        with position_lock:
                            current_symbols_set = base_symbols | dynamic_symbols
                        
                        if subscribed_symbols != current_symbols_set:
                            if subscribed_symbols:
                                await ws.send(json.dumps({
                                    "time": int(time.time()), 
                                    "channel": "futures.tickers",
                                    "event": "unsubscribe", 
                                    "payload": list(subscribed_symbols)
                                }))

                            subscribed_symbols = current_symbols_set
                            payload = list(subscribed_symbols)
                            ws_last_payload = payload[:]
                            ws_last_subscribed_at = int(time.time())
                            await ws.send(json.dumps({
                                "time": ws_last_subscribed_at, 
                                "channel": "futures.tickers",
                                "event": "subscribe", 
                                "payload": payload
                            }))
                        
                        last_resubscribe_time = time.time()

                    if time.time() - last_app_ping_time > 10:
                        await ws.send(json.dumps({
                            "time": int(time.time()), 
                            "channel": "futures.ping"
                        }))
                        last_app_ping_time = time.time()
                    
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    
                    if data.get("channel") == "futures.tickers":
                        result = data.get("result")
                        if isinstance(result, list):
                            for item in result:
                                symbol = normalize_symbol(item.get("contract"))
                                if symbol:
                                    latest_prices[symbol] = Decimal(str(item.get("last", "0")))
                                simple_tp_monitor(item)
                        elif isinstance(result, dict):
                            symbol = normalize_symbol(result.get("contract"))
                            if symbol:
                                latest_prices[symbol] = Decimal(str(result.get("last", "0")))
                            simple_tp_monitor(result)
        
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
            log_debug("🔌 웹소켓 재연결", f"{type(e).__name__}")
        except Exception as e:
            log_debug("🔌 웹소켓 오류", str(e), exc_info=True)
        
        await asyncio.sleep(5)

def simple_tp_monitor(ticker):
    """기존 전략 TP 모니터링 (ETH 제외)"""
    try:
        symbol = normalize_symbol(ticker.get("contract"))
        if symbol == "ETH_USDT":
            return
            
        price = Decimal(str(ticker.get("last", "0")))
        if not symbol or price <= 0: 
            return
        
        cfg = get_symbol_config(symbol)
        if not cfg: 
            return
        
        with position_lock:
            pos = position_state.get(symbol, {})
            
            # Long TP
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
                        log_debug("🎯 롱 TP", f"{symbol} entry:{entry_price:.8f} tp:{tp_price:.8f}")
                        
                        try:
                            order = FuturesOrder(contract=symbol, size=-int(long_size), price="0", tif="ioc", reduce_only=True)
                            result = _get_api_response(api.create_futures_order, SETTLE, order)
                            if result:
                                log_debug("✅ 롱 TP 청산", f"{symbol}")
                                position_state[symbol]['long'] = get_default_pos_side_state()
                                if symbol in tpsl_storage: 
                                    tpsl_storage[symbol]['long'].clear()
                        except Exception as e:
                            log_debug("❌ 롱 TP 오류", str(e), exc_info=True)
            
            # Short TP
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
                        log_debug("🎯 숏 TP", f"{symbol} entry:{entry_price:.8f} tp:{tp_price:.8f}")
                        
                        try:
                            order = FuturesOrder(contract=symbol, size=int(short_size), price="0", tif="ioc", reduce_only=True)
                            result = _get_api_response(api.create_futures_order, SETTLE, order)
                            if result:
                                log_debug("✅ 숏 TP 청산", f"{symbol}")
                                position_state[symbol]['short'] = get_default_pos_side_state()
                                if symbol in tpsl_storage: 
                                    tpsl_storage[symbol]['short'].clear()
                        except Exception as e:
                            log_debug("❌ 숏 TP 오류", str(e), exc_info=True)
    
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

def initialize_states():
    global position_state, tpsl_storage
    for symbol in SYMBOL_CONFIG.keys():
        position_state[symbol] = {
            "long": get_default_pos_side_state(),
            "short": get_default_pos_side_state(),
        }
    tpsl_storage = {}
    for symbol in SYMBOL_CONFIG.keys():
        tpsl_storage[symbol] = {
            "long": [],
            "short": [],
        }

def get_default_pos_side_state():
    return {
        "price": Decimal("0"),
        "size": Decimal("0"),
        "value": Decimal("0"),
        "entry_count": 0,
        "normal_entry_count": 0,
        "premium_entry_count": 0,
        "rescue_entry_count": 0,
        "last_entry_ratio": Decimal("1.0"),
        "premium_tp_multiplier": Decimal("1.0"),
        "current_tp_pct": Decimal("0.0"),
        "entry_time": time.time()
    }

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "v6.60-grid-fixed-qty")
    initialize_states()
    log_debug("💰 초기 자산", f"{get_total_collateral(force=True):.2f} USDT")
    log_debug("📊 그리드 기본 수량", f"{get_base_qty()} (고정)")
    update_all_position_states()
    
    # 최초 그리드 주문
    initialize_grid_orders()

    # 쓰레드 시작
    threading.Thread(target=eth_grid_fill_monitor, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()

    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True).start()

    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
