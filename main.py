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

# ✅ Binance → Gate.io 심볼 변환 테이블
BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

# ✅ 코인별 설정
SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": 10, "qty_step": 10, "sl_pct": 0.0075},
    "BTC_USDT": {"min_qty": 0.0001, "qty_step": 0.0001, "sl_pct": 0.004},
    "SUI_USDT": {"min_qty": 1, "qty_step": 1, "sl_pct": 0.0075}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

# 현재 포지션 상태 저장
position_state = {}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_equity():
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        total_equity = float(accounts.total)  # available 대신 total 사용
        log_debug("💰 계정 잔액", f"총 증거금: {total_equity} USDT")
        return total_equity
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return 1000  # 기본값으로 1000 USDT 설정

def get_available_equity():
    try:
        total_equity = get_equity()
        # 메소드명 수정: list_position → list_futures_positions
        positions = api_instance.list_futures_positions(settle=SETTLE)
        
        used_margin = 0
        for p in positions:
            try:
                size = abs(float(p.size))
                entry_price = float(p.entry_price)
                leverage = float(p.leverage or 1)
                position_margin = (size * entry_price) / leverage
                used_margin += position_margin
            except (TypeError, ValueError, AttributeError) as e:
                log_debug("⚠️ 포지션 마진 계산 오류", f"{p.contract if hasattr(p, 'contract') else 'unknown'}: {str(e)}")
                continue
                
        available = max(total_equity - used_margin, 0)
        per_symbol = available / len(SYMBOL_CONFIG)
        
        log_debug("💵 자금 분배", f"총액: {total_equity}, 사용중: {used_margin}, 가용: {available}, 통화당: {per_symbol}")
        return per_symbol
    except Exception as e:
        log_debug("❌ 사용 가능 증거금 계산 실패", str(e))
        return total_equity / len(SYMBOL_CONFIG)  # 오류 시에도 최소한의 금액 반환

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size) if hasattr(pos, 'size') else 0
        if size != 0:
            position_state[symbol] = {
                "price": float(pos.entry_price),
                "side": "buy" if size > 0 else "sell"
            }
        else:
            position_state[symbol] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None}

def close_position(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size)
        if size == 0:
            log_debug(f"📭 포지션 없음 ({symbol})", "청산할 포지션이 없습니다.")
            return
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 전체 청산 성공 ({symbol})", result.to_dict())
        for _ in range(10):
            pos = api_instance.get_position(SETTLE, symbol)
            if float(pos.size) == 0:
                break
            time.sleep(0.5)
        position_state[symbol] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 전체 청산 실패 ({symbol})", str(e))

def get_max_qty(symbol):
    try:
        config = SYMBOL_CONFIG[symbol]
        available = get_available_equity()
        pos = api_instance.get_position(SETTLE, symbol)
        leverage = float(pos.leverage or 2.0)  # 기본값 2.0 설정
        mark_price = float(pos.mark_price or 0)
        
        # 실제 가격 가져오기 (mark_price가 0이면)
        if mark_price <= 0:
            try:
                tickers = api_instance.list_futures_tickers(settle=SETTLE, contract=symbol)
                mark_price = float(tickers[0].mark_price) if tickers else 0
            except Exception:
                pass

        # 가격이 여전히 0이면 백업으로 최소 수량 사용
        if mark_price <= 0 or leverage <= 0:
            log_debug(f"⚠️ {symbol} 가격 조회 실패", f"최소 수량으로 진행")
            return config["min_qty"]

        raw_qty = available * leverage / mark_price
        step = Decimal(str(config["qty_step"]))
        raw_qty_dec = Decimal(str(raw_qty))
        quantized = (raw_qty_dec / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
        qty = float(quantized)

        # 더 자세한 디버그 로그 추가
        log_debug(f"📊 {symbol} 수량 계산", f"""
- 가용자금: {available:.2f} USDT
- 레버리지: {leverage}
- 마크가격: {mark_price:.6f}
- 원시수량: {raw_qty:.6f}
- 단위조정: {qty:.6f} (최소: {config['min_qty']})
""")
        return max(qty, config["min_qty"])
    except Exception as e:
        log_debug(f"❌ {symbol} 최대 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol]["min_qty"]

def place_order(symbol, side, qty, reduce_only=False):
    try:
        size = qty if side == "buy" else -qty
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", result.to_dict())
        if not reduce_only:
            fill_price = float(result.fill_price) if hasattr(result, 'fill_price') and result.fill_price else 0
            position_state[symbol] = {
                "price": fill_price,
                "side": side
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
                last_msg_time = time.time()
                
                while True:
                    try:
                        # 30초 타임아웃 설정
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        last_msg_time = time.time()
                        
                        data = json.loads(msg)
                        # 핑/퐁 처리
                        if 'event' in data and data['event'] in ['ping', 'pong']:
                            continue
                            
                        # 구독 확인 메시지
                        if 'event' in data and data['event'] == 'subscribe':
                            log_debug("✅ WebSocket 구독완료", f"{data.get('channel')} - {data.get('payload')}")
                            continue
                        
                        if "result" not in data or not isinstance(data["result"], dict):
                            continue
                            
                        symbol = data["result"].get("contract")
                        if symbol not in SYMBOL_CONFIG:
                            continue
                            
                        price = float(data["result"].get("last", 0))
                        if price <= 0:
                            continue
                            
                        update_position_state(symbol)
                        state = position_state.get(symbol, {})
                        entry_price = state.get("price")
                        entry_side = state.get("side")
                        
                        if not entry_price or not entry_side:
                            continue
                            
                        sl_pct = SYMBOL_CONFIG[symbol]["sl_pct"]
                        sl_hit = (
                            entry_side == "buy" and price <= entry_price * (1 - sl_pct) or
                            entry_side == "sell" and price >= entry_price * (1 + sl_pct)
                        )
                        
                        if sl_hit:
                            log_debug(f"🛑 손절 조건 충족 ({symbol})", f"{price=}, {entry_price=}")
                            close_position(symbol)
                    except asyncio.TimeoutError:
                        # 30초간 메시지 없으면 핑 전송
                        await ws.send(json.dumps({
                            "time": int(time.time()),
                            "channel": "futures.ping",
                            "event": "ping",
                            "payload": []
                        }))
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
            return jsonify({"error": "JSON 형식의 요청만 허용됩니다"}), 400
            
        data = request.get_json()
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()
        symbol_raw = data.get("symbol", "").upper().replace(".P", "").replace(".F", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(symbol_raw, symbol_raw)

        if symbol not in SYMBOL_CONFIG or signal not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "유효하지 않은 요청"}), 400

        update_position_state(symbol)
        state = position_state.get(symbol, {})
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "청산 완료"})

        side = "buy" if signal == "long" else "sell"
        entry_side = state.get("side")

        if entry_side and ((side == "buy" and entry_side == "sell") or (side == "sell" and entry_side == "buy")):
            log_debug(f"🔁 반대 포지션 감지 ({symbol})", f"{entry_side=} → 청산 후 재진입")
            close_position(symbol)
            time.sleep(0.5)

        qty = get_max_qty(symbol)
        place_order(symbol, side, qty)
        return jsonify({"status": "진입 완료", "symbol": symbol, "qty": qty})
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 내부 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
