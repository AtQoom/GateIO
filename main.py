import os
import json
import time
import asyncio
import threading
import websockets
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
MARGIN_BUFFER = 0.8  # 사용 가능 증거금의 80%만 사용
ALLOCATION_RATIO = 0.33  # 각 코인당 33%씩 진입

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": 10, "qty_step": 10, "sl_pct": 0.0075},
    "BTC_USDT": {"min_qty": 0.0001, "qty_step": 0.0001, "sl_pct": 0.004},
    "SUI_USDT": {"min_qty": 1, "qty_step": 1, "sl_pct": 0.0075}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

position_state = {}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_account_info():
    """실제 사용 가능한 증거금(available)의 80%만 사용"""
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        available = float(getattr(accounts, 'available', 0))
        safe_available = available * MARGIN_BUFFER
        log_debug("💰 사용 가능 증거금", f"{safe_available:.2f} USDT (원본: {available:.2f})")
        return safe_available
    except Exception as e:
        log_debug("❌ 증거금 조회 실패", str(e))
        return 160 * MARGIN_BUFFER

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(getattr(pos, 'size', 0))
        leverage = float(getattr(pos, 'leverage', 1))
        if size != 0:
            position_state[symbol] = {
                "price": float(getattr(pos, 'entry_price', 0)),
                "side": "buy" if size > 0 else "sell",
                "size": size,
                "leverage": leverage
            }
            log_debug(f"📊 포지션 상태 ({symbol})", f"사이즈: {size}, 진입가: {position_state[symbol]['price']}, 방향: {position_state[symbol]['side']}, 레버리지: {leverage}x")
        else:
            position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1}
            log_debug(f"📊 포지션 상태 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1}

def close_position(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(getattr(pos, 'size', 0))
        if size == 0:
            log_debug(f"📭 포지션 없음 ({symbol})", "청산할 포지션이 없습니다.")
            return
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 전체 청산 성공 ({symbol})", result.to_dict())
        for _ in range(10):
            pos = api_instance.get_position(SETTLE, symbol)
            if float(getattr(pos, 'size', 0)) == 0:
                break
            time.sleep(0.5)
        position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1}
    except Exception as e:
        log_debug(f"❌ 전체 청산 실패 ({symbol})", str(e))

def get_current_price(symbol):
    try:
        tickers = api_instance.list_futures_tickers(settle=SETTLE, contract=symbol)
        if tickers and hasattr(tickers[0], 'last'):
            price = float(tickers[0].last)
            log_debug(f"💲 가격 조회 ({symbol})", f"현재가: {price}")
            return price
        pos = api_instance.get_position(SETTLE, symbol)
        if hasattr(pos, 'mark_price') and pos.mark_price:
            price = float(pos.mark_price)
            log_debug(f"💲 가격 조회 ({symbol})", f"마크가: {price}")
            return price
    except Exception as e:
        log_debug(f"⚠️ {symbol} 가격 조회 실패", str(e))
    return 0

def get_max_qty(symbol, desired_side):
    """레버리지 반영, 기존 포지션/최소 단위 고려 추가 진입 수량 계산"""
    try:
        config = SYMBOL_CONFIG[symbol]
        safe_available = get_account_info()
        current_price = get_current_price(symbol)
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        leverage = state.get("leverage", 1)
        # 목표 마진(증거금) 및 레버리지 적용
        target_margin = safe_available * ALLOCATION_RATIO
        target_value = target_margin * leverage
        current_size = state.get("size", 0)
        current_side = state.get("side")
        current_value = abs(current_size) * current_price
        # 같은 방향이면 추가 진입, 반대면 전체 진입
        if current_side == desired_side:
            additional_value = max(target_value - current_value, 0)
        else:
            additional_value = target_value
        additional_qty = additional_value / current_price
        # 최소 단위 내림
        step = Decimal(str(config["qty_step"]))
        raw_qty_dec = Decimal(str(additional_qty))
        quantized = (raw_qty_dec / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
        qty = float(quantized)
        final_qty = max(qty, config["min_qty"])
        log_debug(f"📊 {symbol} 수량 계산", 
                 f"레버리지: {leverage}x, 목표가치: {target_value:.2f} USDT, "
                 f"추가수량: {final_qty}")
        return final_qty
    except Exception as e:
        log_debug(f"❌ {symbol} 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol]["min_qty"]

def place_order(symbol, side, qty, reduce_only=False, retry=2):
    if qty <= 0:
        log_debug(f"⚠️ 주문 무시 ({symbol})", "수량이 0 이하")
        return False
    try:
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        fill_price = float(getattr(result, 'fill_price', 0))
        fill_size = float(getattr(result, 'size', 0))
        log_debug(f"✅ 주문 성공 ({symbol})", f"수량: {fill_size}, 체결가: {fill_price}")
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", error_msg)
        # 증거금 부족 또는 단위 오류 시 수량 감소 후 재시도
        if retry > 0 and ("INSUFFICIENT_AVAILABLE" in error_msg or "INVALID_PARAM_VALUE" in error_msg):
            reduced_qty = qty * 0.5
            if reduced_qty >= SYMBOL_CONFIG[symbol]["min_qty"]:
                log_debug(f"🔄 주문 재시도 ({symbol})", f"수량 감소: {qty} → {reduced_qty}")
                return place_order(symbol, side, reduced_qty, reduce_only, retry-1)
        return False

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    contracts = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": contracts
                }))
                log_debug("📡 WebSocket 연결 성공", f"구독 중: {contracts}")
                last_ping_time = time.time()
                last_msg_time = time.time()
                while True:
                    try:
                        current_time = time.time()
                        if current_time - last_ping_time > 30:
                            await ws.send(json.dumps({
                                "time": int(current_time),
                                "channel": "futures.ping",
                                "event": "ping",
                                "payload": []
                            }))
                            last_ping_time = current_time
                        if current_time - last_msg_time > 300:
                            log_debug("⚠️ 오랜 시간 메시지 없음", "연결 재설정")
                            break
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        last_msg_time = time.time()
                        data = json.loads(msg)
                        if 'event' in data and data['event'] in ['ping', 'pong']:
                            continue
                        if 'event' in data and data['event'] == 'subscribe':
                            log_debug("✅ WebSocket 구독완료", f"{data.get('channel')} - {data.get('payload')}")
                            continue
                        if "result" not in data or not isinstance(data["result"], dict):
                            continue
                        symbol = data["result"].get("contract")
                        if not symbol or symbol not in SYMBOL_CONFIG:
                            continue
                        price = float(data["result"].get("last", 0))
                        if price <= 0:
                            continue
                        state = position_state.get(symbol, {})
                        entry_price = state.get("price")
                        entry_side = state.get("side")
                        if not entry_price or not entry_side:
                            continue
                        sl_pct = SYMBOL_CONFIG[symbol]["sl_pct"]
                        sl_hit = (
                            (entry_side == "buy" and price <= entry_price * (1 - sl_pct)) or
                            (entry_side == "sell" and price >= entry_price * (1 + sl_pct))
                        )
                        if sl_hit:
                            log_debug(f"🛑 손절 조건 충족 ({symbol})", f"현재가: {price}, 진입가: {entry_price}, 손절폭: {sl_pct*100}%")
                            close_position(symbol)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        log_debug("❌ WebSocket 메시지 처리 오류", str(e))
                        continue
        except Exception as e:
            log_debug("❌ WebSocket 연결 오류", str(e))
            await asyncio.sleep(5)

def start_price_listener():
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        if not request.is_json:
            log_debug("⚠️ 잘못된 요청", "JSON 형식 아님")
            return jsonify({"error": "JSON 형식의 요청만 허용됩니다"}), 400
        data = request.get_json()
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()
        symbol_raw = data.get("symbol", "").upper().replace(".P", "").replace(".F", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(symbol_raw, symbol_raw)
        log_debug("📥 웹훅 수신", f"심볼: {symbol}, 신호: {signal}, 액션: {action}")
        if symbol not in SYMBOL_CONFIG:
            log_debug("⚠️ 알 수 없는 심볼", symbol)
            return jsonify({"error": f"지원하지 않는 심볼: {symbol}"}), 400
        if signal not in ["long", "short", "buy", "sell"]:
            log_debug("⚠️ 잘못된 신호", signal)
            return jsonify({"error": f"유효하지 않은 신호: {signal}"}), 400
        if action not in ["entry", "exit"]:
            log_debug("⚠️ 잘못된 액션", action)
            return jsonify({"error": f"유효하지 않은 액션: {action}"}), 400
        if signal == "buy":
            signal = "long"
        elif signal == "sell":
            signal = "short"
        desired_side = "buy" if signal == "long" else "sell"
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        current_side = state.get("side")
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "청산 완료", "symbol": symbol})
        # 진입 로직 (기존 포지션과 같은 방향이면 추가 진입, 반대면 청산 후 진입)
        if current_side == desired_side:
            qty = get_max_qty(symbol, desired_side)
            if qty > 0:
                success = place_order(symbol, desired_side, qty)
                if not success:
                    log_debug(f"⚠️ {symbol} 주문 실패", "다음 신호 대기")
            else:
                log_debug(f"😶 추가 진입 불필요 ({symbol})", "목표 포지션 이미 달성")
        elif current_side and current_side != desired_side:
            log_debug(f"🔁 반대 포지션 감지 ({symbol})", f"{current_side} → {desired_side}로 전환")
            close_position(symbol)
            time.sleep(0.5)
            qty = get_max_qty(symbol, desired_side)
            place_order(symbol, desired_side, qty)
        else:
            qty = get_max_qty(symbol, desired_side)
            place_order(symbol, desired_side, qty)
        return jsonify({
            "status": "진입 완료", 
            "symbol": symbol, 
            "side": desired_side,
            "qty": qty
        })
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 내부 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info()
        for symbol in SYMBOL_CONFIG.keys():
            update_position_state(symbol)
        return jsonify({
            "status": "running",
            "equity": equity,
            "positions": position_state
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
