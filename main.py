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

# 🔐 환경변수에서 API 키 로딩
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
STOP_LOSS_PCT = 0.0075

# ✅ 코인별 세부 설정
SYMBOL_CONFIG = {
    "ADAUSDT": {"symbol": "ADA_USDT", "min_qty": 10, "qty_step": 10},
    "BTCUSDT": {"symbol": "BTC_USDT", "min_qty": 0.0001, "qty_step": 0.0001},
    "SUIUSDT": {"symbol": "SUI_USDT", "min_qty": 1, "qty_step": 1},
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

# 📊 상태 저장
position_state = {}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_equity():
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        return float(accounts.available)
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return 0

def get_available_equity():
    try:
        total_equity = get_equity()
        positions = api_instance.list_futures_positions(SETTLE)
        used_margin = sum([
            abs(float(p.size) * float(p.entry_price)) / float(p.leverage)
            for p in positions if hasattr(p, 'size') and hasattr(p, 'entry_price') and hasattr(p, 'leverage')
        ])
        available = max(total_equity - used_margin, 0)
        return available / len(SYMBOL_CONFIG)
    except Exception as e:
        log_debug("❌ 사용 가능 증거금 계산 실패", str(e))
        return 0

def update_position_state(symbol_key):
    try:
        if symbol_key not in SYMBOL_CONFIG:
            return
        
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size) if hasattr(pos, 'size') else 0
        
        if size != 0:
            position_state[symbol_key] = {
                "price": float(pos.entry_price) if hasattr(pos, 'entry_price') else 0,
                "side": "buy" if size > 0 else "sell"
            }
        else:
            position_state[symbol_key] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol_key})", str(e))
        position_state[symbol_key] = {"price": None, "side": None}

def close_position(symbol_key):
    try:
        if symbol_key not in SYMBOL_CONFIG:
            return
            
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size) if hasattr(pos, 'size') else 0
        
        if size == 0:
            log_debug(f"📭 포지션 없음 ({symbol_key})", "청산할 포지션이 없습니다.")
            return
            
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 전체 청산 성공 ({symbol_key})", result.to_dict())
        
        # 🔁 포지션 종료 확인 (최대 10번, 5초)
        attempts = 0
        while attempts < 10:
            pos = api_instance.get_position(SETTLE, symbol)
            if float(pos.size) == 0:
                break
            time.sleep(0.5)
            attempts += 1
            
        position_state[symbol_key] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 전체 청산 실패 ({symbol_key})", str(e))

def get_max_qty(symbol_key):
    try:
        config = SYMBOL_CONFIG[symbol_key]
        symbol = config["symbol"]
        available = get_available_equity()
        pos = api_instance.get_position(SETTLE, symbol)
        leverage = float(pos.leverage) if hasattr(pos, 'leverage') else 1.0
        mark_price = float(pos.mark_price) if hasattr(pos, 'mark_price') else 0
        
        if mark_price <= 0:
            return config["min_qty"]
            
        raw_qty = available * leverage / mark_price
        step = Decimal(str(config["qty_step"]))
        raw_qty_dec = Decimal(str(raw_qty))
        
        # 정확한 소수점 계산
        quantized = (raw_qty_dec / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
        qty = float(quantized)
        
        log_debug(f"📐 {symbol_key} 수량 계산", f"{qty=}, {available=}, {leverage=}, {mark_price=}")
        return max(qty, config["min_qty"])
    except Exception as e:
        log_debug(f"❌ {symbol_key} 최대 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol_key]["min_qty"]

def place_order(symbol_key, side, qty, reduce_only=False):
    try:
        symbol = SYMBOL_CONFIG[symbol_key]["symbol"]
        size = qty if side == "buy" else -qty
        
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol_key})", result.to_dict())
        
        if not reduce_only:
            fill_price = float(result.fill_price) if hasattr(result, 'fill_price') and result.fill_price else 0
            position_state[symbol_key] = {
                "price": fill_price,
                "side": side
            }
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol_key})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = [v["symbol"] for v in SYMBOL_CONFIG.values()]
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                # WebSocket 구독
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                
                log_debug("📡 WebSocket", f"연결 성공 - 구독 중: {symbols}")
                
                # 메시지 처리
                while True:
                    try:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        
                        # 구독 확인 메시지
                        if 'event' in data and data['event'] == 'subscribe':
                            log_debug("✅ WebSocket 구독완료", f"{data.get('channel')} - {data.get('payload')}")
                            continue
                        
                        # 응답 정보가 없거나 유효하지 않은 경우 스킵
                        if 'result' not in data or not isinstance(data['result'], dict):
                            continue
                            
                        # 'contract' 키가 있는지 확인
                        if 'contract' not in data['result']:
                            continue
                            
                        # 심볼 정보 추출
                        symbol = data['result']['contract']
                        symbol_key = next((k for k, v in SYMBOL_CONFIG.items() if v["symbol"] == symbol), None)
                        
                        if not symbol_key:
                            continue
                            
                        # 가격 확인
                        price = float(data['result'].get("last", 0))
                        if price <= 0:
                            continue
                            
                        # 포지션 상태 업데이트
                        update_position_state(symbol_key)
                        state = position_state.get(symbol_key, {})
                        entry_price = state.get("price")
                        entry_side = state.get("side")
                        
                        if not entry_price or not entry_side:
                            continue
                        
                        # 손절 조건 확인
                        sl_hit = (
                            (entry_side == "buy" and price <= entry_price * (1 - STOP_LOSS_PCT)) or
                            (entry_side == "sell" and price >= entry_price * (1 + STOP_LOSS_PCT))
                        )
                        
                        if sl_hit:
                            log_debug(f"🛑 손절 조건 충족 ({symbol_key})", f"{price=}, {entry_price=}")
                            close_position(symbol_key)
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        log_debug("❌ WebSocket 메시지 처리 오류", str(e))
                        continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_debug("❌ WebSocket 연결 오류", str(e))
            # 연결 끊김 시 5초 후 재연결
            await asyncio.sleep(5)

def start_price_listener():
    # 모든 포지션 초기화
    for symbol_key in SYMBOL_CONFIG:
        update_position_state(symbol_key)
    
    # WebSocket 리스너 시작
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(price_listener())
    except Exception as e:
        log_debug("❌ 가격 리스너 오류", str(e))

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()
        symbol_key = data.get("symbol", "").upper()

        if signal == "buy":
            signal = "long"
        elif signal == "sell":
            signal = "short"

        if signal not in ["long", "short"] or action not in ["entry", "exit"] or symbol_key not in SYMBOL_CONFIG:
            return jsonify({"error": "유효하지 않은 요청"}), 400

        update_position_state(symbol_key)
        state = position_state.get(symbol_key, {})

        if action == "exit":
            close_position(symbol_key)
            return jsonify({"status": "청산 완료"})

        side = "buy" if signal == "long" else "sell"
        entry_side = state.get("side")

        if entry_side and ((signal == "long" and entry_side == "sell") or (signal == "short" and entry_side == "buy")):
            log_debug(f"🔁 반대포지션 감지 ({symbol_key})", f"{entry_side=} → 청산 후 재진입")
            close_position(symbol_key)
            time.sleep(0.5)

        qty = get_max_qty(symbol_key)
        place_order(symbol_key, side, qty)
        return jsonify({"status": "진입 완료", "symbol": symbol_key, "qty": qty})

    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
