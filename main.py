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
MANUAL_EQUITY = float(os.environ.get("MANUAL_EQUITY", "0"))
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

def get_total_equity():
    try:
        if MANUAL_EQUITY > 0:
            log_debug("💰 수동 설정 증거금", f"{MANUAL_EQUITY} USDT")
            return MANUAL_EQUITY
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        total = float(getattr(accounts, 'total', 0))
        unrealized_pnl = float(getattr(accounts, 'unrealised_pnl', 0))
        total_equity = total + unrealized_pnl
        log_debug("💰 선물 계정 잔액", f"총액: {total} USDT, 미실현 손익: {unrealized_pnl} USDT, 총 가용: {total_equity} USDT")
        return total_equity if total_equity > 1 else 160
    except Exception as e:
        log_debug("❌ 증거금 조회 실패", str(e))
        return 160

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(getattr(pos, 'size', 0))
        if size != 0:
            position_state[symbol] = {
                "price": float(getattr(pos, 'entry_price', 0)),
                "side": "buy" if size > 0 else "sell",
                "size": size
            }
            log_debug(f"📊 포지션 상태 ({symbol})", f"사이즈: {size}, 진입가: {position_state[symbol]['price']}, 방향: {position_state[symbol]['side']}")
        else:
            position_state[symbol] = {"price": None, "side": None, "size": 0}
            log_debug(f"📊 포지션 상태 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None, "size": 0}

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
        position_state[symbol] = {"price": None, "side": None, "size": 0}
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
    """기존 포지션을 고려하여 추가 진입 수량 계산"""
    try:
        config = SYMBOL_CONFIG[symbol]
        total_equity = get_total_equity()
        current_price = get_current_price(symbol)
        if current_price <= 0:
            log_debug(f"⚠️ {symbol} 가격이 0임", f"심볼 최소 수량 사용")
            return config["min_qty"]

        # 목표 포지션 가치
        target_value = total_equity * ALLOCATION_RATIO

        # 현재 포지션 정보
        pos = api_instance.get_position(SETTLE, symbol)
        current_size = float(getattr(pos, 'size', 0))
        current_side = "buy" if current_size > 0 else "sell" if current_size < 0 else None
        current_value = abs(current_size) * current_price

        # 같은 방향이면 추가 진입, 반대면 전체 진입
        if current_side == desired_side:
            additional_value = max(target_value - current_value, 0)
            additional_qty = additional_value / current_price
        else:
            additional_qty = target_value / current_price

        # 최소 단위, 최소 수량 적용
        step = Decimal(str(config["qty_step"]))
        raw_qty_dec = Decimal(str(additional_qty))
        quantized = (raw_qty_dec / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
        qty = float(quantized)
        final_qty = max(qty, config["min_qty"])
        log_debug(f"📊 {symbol} 수량 계산", 
                 f"현재 포지션: {current_size}, 방향: {current_side}, 목표 가치: {target_value:.2f} USDT, 추가 수량: {additional_qty:.2f} → 최종: {final_qty}")
        return final_qty
    except Exception as e:
        log_debug(f"❌ {symbol} 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol]["min_qty"]

def place_order(symbol, side, qty, reduce_only=False):
    try:
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        fill_price = float(getattr(result, 'fill_price', 0))
        fill_size = float(getattr(result, 'size', 0))
        log_debug(f"✅ 주문 성공 ({symbol})", f"수량: {fill_size}, 체결가: {fill_price}")
        if not reduce_only:
            position_state[symbol] = {
                "price": fill_price,
                "side": side,
                "size": size
            }
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol})", str(e))

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
                place_order(symbol, desired_side, qty)
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
        equity = get_total_equity()
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
