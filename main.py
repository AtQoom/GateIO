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
MARGIN_BUFFER = 0.7
ALLOCATION_RATIO = 0.33

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
account_cache = {"time": 0, "data": None}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_account_info(force_refresh=False):
    current_time = time.time()
    if not force_refresh and account_cache["time"] > current_time - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        available = float(getattr(accounts, 'available', 0))
        safe_available = available * MARGIN_BUFFER
        log_debug("💰 계정 정보", f"가용: {available:.2f}, 안전가용: {safe_available:.2f} USDT")
        account_cache["time"] = current_time
        account_cache["data"] = safe_available
        return safe_available
    except Exception as e:
        log_debug("❌ 증거금 조회 실패", str(e))
        return 100 * MARGIN_BUFFER

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(getattr(pos, 'size', 0))
        mark_price = float(getattr(pos, 'mark_price', 0))
        leverage = float(getattr(pos, 'leverage', 1))
        if size != 0:
            position_state[symbol] = {
                "price": float(getattr(pos, 'entry_price', 0)),
                "side": "buy" if size > 0 else "sell",
                "size": abs(size),
                "leverage": leverage,
                "value": abs(size) * mark_price,
                "margin": (abs(size) * mark_price) / leverage
            }
            log_debug(f"📊 포지션 상태 ({symbol})", 
                    f"수량: {abs(size)}, 가격: {position_state[symbol]['price']:.4f}, "
                    f"가치: {position_state[symbol]['value']:.2f}, 증거금: {position_state[symbol]['margin']:.2f}, "
                    f"레버리지: {leverage}x")
        else:
            position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1, "value": 0, "margin": 0}
            log_debug(f"📊 포지션 상태 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1, "value": 0, "margin": 0}

def get_current_price(symbol):
    try:
        tickers = api_instance.list_futures_tickers(settle=SETTLE, contract=symbol)
        if tickers and len(tickers) > 0:
            price = float(tickers[0].last)
            return price
        pos = api_instance.get_position(SETTLE, symbol)
        if hasattr(pos, 'mark_price') and pos.mark_price:
            price = float(pos.mark_price)
            return price
    except Exception as e:
        log_debug(f"⚠️ {symbol} 가격 조회 실패", str(e))
    return 0

def get_max_qty(symbol, desired_side):
    try:
        config = SYMBOL_CONFIG[symbol]
        safe_available = get_account_info()
        current_price = get_current_price(symbol)
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        leverage = state.get("leverage", 1)
        target_margin = safe_available * ALLOCATION_RATIO
        current_side = state.get("side")
        current_margin = state.get("margin", 0)
        if current_side == desired_side and current_margin > 0:
            additional_margin = max(target_margin - current_margin, 0)
        else:
            additional_margin = target_margin
        order_value = additional_margin * leverage
        raw_qty = order_value / current_price

        # 수량을 반드시 int로, 최소단위의 정수배로 내림
        step = config["qty_step"]
        min_qty = config["min_qty"]
        qty = int(raw_qty // step * step)
        if qty < min_qty:
            qty = min_qty
        log_debug(f"📊 {symbol} 주문 계획", 
                f"가격: {current_price:.4f}, 주문수량: {qty}")
        return qty
    except Exception as e:
        log_debug(f"❌ {symbol} 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol]["min_qty"]

def place_order(symbol, side, qty, reduce_only=False, retry=3):
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
        # 주문 단위 오류/증거금 부족 시 수량 10의 배수로 줄여 재시도
        if retry > 0 and ("INSUFFICIENT_AVAILABLE" in error_msg or 
                          "INVALID_PARAM_VALUE" in error_msg or 
                          "Bad Request" in error_msg):
            config = SYMBOL_CONFIG[symbol]
            min_qty = config["min_qty"]
            step = config["qty_step"]
            reduced_qty = int((qty * 0.6) // step * step)
            if reduced_qty >= min_qty:
                log_debug(f"🔄 주문 재시도 ({symbol})", f"수량 감소: {qty} → {reduced_qty}")
                time.sleep(1)
                return place_order(symbol, side, reduced_qty, reduce_only, retry-1)
        return False
        
async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    contracts = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                # 한 번에 모든 코인 구독
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
    # 서버 시작 시 모든 포지션 정보 초기화
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
    
    # WebSocket 리스너 시작
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
        
        # 사전 계정 정보 갱신
        get_account_info(force_refresh=True)
        
        desired_side = "buy" if signal == "long" else "sell"
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        current_side = state.get("side")
        
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "청산 완료", "symbol": symbol})

        # 진입 로직 (기존 포지션과 같은 방향이면 추가 진입, 반대면 청산 후 진입)
        if current_side == desired_side:
            # 같은 방향 추가 진입
            qty = get_max_qty(symbol, desired_side)
            if qty > 0:
                success = place_order(symbol, desired_side, qty)
                if not success:
                    log_debug(f"⚠️ {symbol} 주문 실패", "다음 신호 대기")
            else:
                log_debug(f"😶 추가 진입 불필요 ({symbol})", "목표 포지션 이미 달성")
        elif current_side and current_side != desired_side:
            # 반대 포지션 - 청산 후 진입
            log_debug(f"🔁 반대 포지션 감지 ({symbol})", f"{current_side} → {desired_side}로 전환")
            close_position(symbol)
            time.sleep(1)  # 청산 API 반영 대기
            
            # 청산 후 사용 가능 증거금 갱신
            get_account_info(force_refresh=True)
            
            # 새 포지션 진입
            qty = get_max_qty(symbol, desired_side)
            place_order(symbol, desired_side, qty)
        else:
            # 신규 진입
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
            
        # 총 사용 증거금 계산
        used_margin = calculate_total_used_margin()
        
        return jsonify({
            "status": "running",
            "equity": equity,
            "used_margin": used_margin,
            "positions": position_state
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
