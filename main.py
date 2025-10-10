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
    return 30.0  # 임시 값

def get_base_qty():
    # TODO: 잔고 기반 실제 동적 계산을 위해 API 잔고 조회 호출 필요
    return Decimal("2.0")

def calculate_dynamic_qty(base_qty: Decimal, obv_macd_val: float, side: str) -> Decimal:
    ratio = 1.0
    if side == "long":  # 역방향 숏 진입시 적용
        if obv_macd_val <= -20 and obv_macd_val > -30:
            ratio = 2.1
        elif obv_macd_val <= -30 and obv_macd_val > -40:
            ratio = 2.2
        elif obv_macd_val <= -40 and obv_macd_val > -50:
            ratio = 2.3
        elif obv_macd_val <= -50 and obv_macd_val > -60:
            ratio = 2.4
        elif obv_macd_val <= -60 and obv_macd_val > -70:
            ratio = 2.5
        elif obv_macd_val <= -70 and obv_macd_val > -80:
            ratio = 2.6
        elif obv_macd_val <= -80 and obv_macd_val > -90:
            ratio = 2.7
        elif obv_macd_val <= -90 and obv_macd_val > -100:
            ratio = 2.8
        elif obv_macd_val <= -100 and obv_macd_val > -110:
            ratio = 2.9
        elif obv_macd_val <= -110:
            ratio = 3.0
    elif side == "short":  # 역방향 롱 진입시 적용
        if obv_macd_val >= 20 and obv_macd_val < 30:
            ratio = 2.1
        elif obv_macd_val >= 30 and obv_macd_val < 40:
            ratio = 2.2
        elif obv_macd_val >= 40 and obv_macd_val < 50:
            ratio = 2.3
        elif obv_macd_val >= 50 and obv_macd_val < 60:
            ratio = 2.4
        elif obv_macd_val >= 60 and obv_macd_val < 70:
            ratio = 2.5
        elif obv_macd_val >= 70 and obv_macd_val < 80:
            ratio = 2.6
        elif obv_macd_val >= 80 and obv_macd_val < 90:
            ratio = 2.7
        elif obv_macd_val >= 90 and obv_macd_val < 100:
            ratio = 2.8
        elif obv_macd_val >= 100 and obv_macd_val < 110:
            ratio = 2.9
        elif obv_macd_val >= 110:
            ratio = 3.0
    return base_qty * Decimal(str(ratio))

def place_order(symbol, side, qty: Decimal):
    with position_lock:
        try:
            update_all_position_states()
            original_size = position_state.get(symbol, {}).get(side, {}).get("size", Decimal("0"))
            order_size = qty.quantize(Decimal("1"), rounding=ROUND_DOWN)
            if side == "short":
                order_size = -order_size
            order = FuturesOrder(contract=symbol, size=int(order_size), price="0", tif="ioc")
            result = api.create_futures_order(SETTLE, order)
            if not result:
                log_debug("❌ 주문 실패", f"{symbol}_{side}")
                return False
            for _ in range(15):
                time.sleep(1)
                update_all_position_states()
                new_size = position_state.get(symbol, {}).get(side, {}).get("size", Decimal("0"))
                if new_size > original_size:
                    return True
            log_debug("❌ 포지션 갱신 실패", f"{symbol}_{side}")
            return False
        except Exception as ex:
            log_debug("❌ 주문에러", str(ex), exc_info=True)
            return False

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
    obv_macd_val = get_obv_macd_value()
    base_qty = get_base_qty()
    dynamic_qty = calculate_dynamic_qty(base_qty, obv_macd_val, side)
    if dynamic_qty < Decimal('1'):
        return
    success = place_order(symbol, side, dynamic_qty)
    if success:
        update_position_state(symbol, side, price, dynamic_qty)
        log_debug("📈 그리드 진입", f"{symbol} {side} qty={dynamic_qty} price={price}")

def handle_entry(data):
    """
    기존 전략 진입 처리 - ETH_USDT 심볼은 제외
    """
    symbol = normalize_symbol(data.get("symbol"))
    if symbol == "ETH_USDT":
        # ETH는 기존 전략 제외, 별도 handle_grid_entry 에서 처리
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

    # 슬리피지 체크 (첫 진입 시)
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
    
    if qty > 0 and place_order(symbol, side, qty):
        state["entry_count"] += 1
        state[f"{base_type}_entry_count"] = current_signal_count + 1
        state["entry_time"] = time.time()
        
        if "rescue" not in signal_type:
            state["last_entry_ratio"] = final_ratio
        
        # 프리미엄 TP 배수 재계산
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
    """
    Gate.io API로 포지션 최신화 후 로컬 상태 반영
    """
    with position_lock:
        api_positions = _get_api_response(api.list_positions, settle=SETTLE)
        if api_positions is None:
            log_debug("❌ 포지션 동기화 실패", "API 응답 없음")
            return
        
        log_debug("📊 포지션 동기화", f"포지션 수={len(api_positions)}")
        
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
                log_debug("🆕 심볼 초기화", f"{symbol}")
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
            if old_size != abs(pos_size):
                log_debug("🔄 포지션 상태 갱신", f"{symbol} {side.upper()}: {old_size} -> {abs(pos_size)}")

        # API 미응답된 비보유 사이드 초기화
        for symbol, side in cleared:
            log_debug("🔄 포지션 초기화 감지", f"{symbol} {side.upper()}")
            position_state[symbol][side] = get_default_pos_side_state()

def grid_auto_order_loop():
    symbol = "ETH_USDT"
    gap_pct = Decimal("0.15") / Decimal("100")
    sleep_sec = 15  # 15초마다 한 번씩

    while True:
        # 1. 최신 체결가(또는 ws현재가) 기준
        with position_lock:
            # 최근 체결가를 포지션에서 구함 (없으면 get_price)
            last_long_price = position_state.get(symbol, {}).get("long", {}).get("price")
            last_short_price = position_state.get(symbol, {}).get("short", {}).get("price")
        
        # 최근 체결가(없으면 단순 get_price 사용)
        ref_price = None
        if last_long_price and last_short_price:
            # 롱·숏 각각 체결된게 있으면, 더 최근 체결가가 기준
            ref_price = max(last_long_price, last_short_price)
        elif last_long_price:
            ref_price = last_long_price
        elif last_short_price:
            ref_price = last_short_price
        else:
            ref_price = Decimal(str(get_price(symbol)))

        price = Decimal(str(ref_price))

        # 2. 0.15% 위/아래 지정가 산정, 역방향 지정
        up_entry_price = price * (1 + gap_pct)
        down_entry_price = price * (1 - gap_pct)

        # 3. 역방향(위:숏, 아래:롱) 진입
        data_short = {"symbol": symbol, "side": "short", "price": str(up_entry_price)}
        data_long = {"symbol": symbol, "side": "long", "price": str(down_entry_price)}

        # 4. 각각 진입(자동으로 obv macd 계수·동적수량/TP/SL 등 반영)
        handle_grid_entry(data_short)
        handle_grid_entry(data_long)

        time.sleep(sleep_sec)

def initialize_grid_orders():
    symbol = "ETH_USDT"
    gap_pct = Decimal("0.15") / Decimal("100")
    current_price = Decimal(str(get_price(symbol)))

    up_price = current_price * (1 + gap_pct)
    down_price = current_price * (1 - gap_pct)

    handle_grid_entry({"symbol": symbol, "side": "short", "price": str(up_price)})
    handle_grid_entry({"symbol": symbol, "side": "long", "price": str(down_price)})

def on_grid_fill_event(symbol, side, fill_price):
    obv_macd_val = get_obv_macd_value()
    base_qty = get_base_qty()
    dynamic_qty = calculate_dynamic_qty(base_qty, obv_macd_val, side)

    gap_pct = Decimal("0.15") / Decimal("100")
    fill_price_dec = Decimal(str(fill_price))
    if side == "long":
        new_order_price = fill_price_dec * (1 + gap_pct)
        opposite_side = "short"
    else:
        new_order_price = fill_price_dec * (1 - gap_pct)
        opposite_side = "long"

    handle_grid_entry({
        "symbol": symbol,
        "side": opposite_side,
        "price": str(new_order_price),
        "qty": dynamic_qty
    })

def handle_grid_entry(data):
    symbol = normalize_symbol(data.get("symbol"))
    if symbol != "ETH_USDT":
        return
    side = data.get("side", "").lower()
    price = Decimal(str(data.get("price", "0")))
    qty = data.get("qty")
    if qty is None:
        obv_macd_val = get_obv_macd_value()
        base_qty = get_base_qty()
        qty = calculate_dynamic_qty(base_qty, obv_macd_val, side)
    else:
        qty = Decimal(str(qty))

    if qty < Decimal('1'):
        return

    success = place_order(symbol, side, qty)
    if success:
        update_position_state(symbol, side, price, qty)
        log_debug("📈 그리드 진입", f"{symbol} {side} qty={qty} price={price}")

def get_total_collateral(force=False):
    try:
        # Gate.io 선물 USDT 계좌의 총 자산 조회
        account_info = api.list_futures_accounts(settle='usdt')
        return float(getattr(account_info, "total", 0))  # 실제 필드명에 맞게 교정
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

def get_price(symbol):
    return 2000.0

def calculate_position_size(symbol, entry_type, entry_score, current_signal_count):
    return Decimal("1"), Decimal("1")

def store_tp_sl(symbol, side, tp_pct, sl_pct, entry_count):
    pass

def set_manual_close_protection(symbol, side, duration):
    pass

def is_manual_close_protected(symbol, side):
    return False

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
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                log_debug("🔌 웹소켓 연결 성공", uri)
                last_resubscribe_time = 0
                last_app_ping_time = 0
                subscribed_symbols = set()

                while True:
                    # 60초마다 심볼 리스트 갱신 및 재구독
                    if time.time() - last_resubscribe_time > 60:
                        base_symbols = set(SYMBOL_CONFIG.keys())
                        with position_lock:
                            current_symbols_set = base_symbols | dynamic_symbols
                        
                        if subscribed_symbols != current_symbols_set:
                            # 기존 구독 해지 (중요: unsubscribe 먼저)
                            if subscribed_symbols:
                                await ws.send(json.dumps({
                                    "time": int(time.time()), 
                                    "channel": "futures.tickers",
                                    "event": "unsubscribe", 
                                    "payload": list(subscribed_symbols)
                                }))
                                log_debug("🔌 웹소켓 구독 해지", f"{len(subscribed_symbols)}개 심볼")

                            # 신규 구독
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
                            log_debug("🔌 웹소켓 구독/재구독", f"{len(payload)}개 심볼")
                        
                        last_resubscribe_time = time.time()

                    # 10초마다 앱 레벨 핑
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
                                simple_tp_monitor(item)
                        elif isinstance(result, dict):
                            simple_tp_monitor(result)
        
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
        if not symbol or price <= 0: 
            return
        
        cfg = get_symbol_config(symbol)
        if not cfg: 
            return
        
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
                                if symbol in tpsl_storage: 
                                    tpsl_storage[symbol]['long'].clear()
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
                                if symbol in tpsl_storage: 
                                    tpsl_storage[symbol]['short'].clear()
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

def initialize_states():
    """포지션/TP·SL 저장 관련 전역 상태 초기화"""
    global position_state, tpsl_storage
    # 이미 선언된 포지션 상태 초기화
    for symbol in SYMBOL_CONFIG.keys():
        position_state[symbol] = {
            "long": get_default_pos_side_state(),
            "short": get_default_pos_side_state(),
        }
    # TP·SL 스토리지 초기화 (없으면 새로 추가)
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
        "current_tp_pct": Decimal("0.0")
    }

if __name__ == "__main__":
    log_debug("🚀 서버 시작", "...")
    initialize_states()
    initialize_grid_orders()  # 최초 지정가 2개 주문 세팅 함수 추가 호출
    log_debug("💰 초기 자산", f"{get_total_collateral(force=True):.2f} USDT")
    update_all_position_states()

    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    # 기존 grid_auto_order_loop는 제거 또는 비활성화 필요

    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True).start()

    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹 서버 시작", f"0.0.0.0:{port} 대기 중...")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
