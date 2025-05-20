import os
import json
import time
import asyncio
import threading
import websockets
from decimal import Decimal
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, PositionLeverage

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"
MARGIN_BUFFER = Decimal("0.9")
POSITION_RATIO = Decimal("0.33")

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {
        "min_qty": Decimal("10"),
        "qty_step": Decimal("10"),
        "sl_pct": Decimal("0.0075"),
        "tp_pct": Decimal("0.008"),
        "sl_rsi": Decimal("0.004"),
        "tp_rsi": Decimal("0.008"),
        "leverage": 3,
        "min_order_usdt": Decimal("5")
    },
    "BTC_USDT": {
        "min_qty": Decimal("0.0001"),
        "qty_step": Decimal("0.0001"),
        "sl_pct": Decimal("0.004"),
        "tp_pct": Decimal("0.006"),
        "sl_rsi": Decimal("0.002"),
        "tp_rsi": Decimal("0.006"),
        "leverage": 2,
        "min_order_usdt": Decimal("5")
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "sl_pct": Decimal("0.0075"),
        "tp_pct": Decimal("0.008"),
        "sl_rsi": Decimal("0.004"),
        "tp_rsi": Decimal("0.008"),
        "leverage": 3,
        "min_order_usdt": Decimal("5")
    }
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}
account_cache = {"time": 0, "data": None}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        accounts = api.list_futures_accounts(SETTLE)
        available = Decimal(str(accounts.available))
        safe = available * MARGIN_BUFFER
        account_cache.update({"time": now, "data": safe})
        log_debug("💰 계정 정보", f"가용: {available}, 안전가용: {safe}")
        return safe
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return Decimal("100")

def update_position_state(symbol):
    try:
        pos = api.get_position(SETTLE, symbol)
        size = Decimal(str(getattr(pos, "size", "0")))
        leverage = Decimal(str(getattr(pos, "leverage", "1")))
        # 레버리지 0이면 강제 세팅
        if leverage == 0:
            log_debug(f"⚠️ 레버리지 0 감지, 강제 설정 ({symbol})", "")
            set_leverage(symbol)
            leverage = Decimal(str(SYMBOL_CONFIG[symbol].get("leverage", 2)))
        if size != 0:
            entry_price = Decimal(str(getattr(pos, "entry_price", "0")))
            mark_price = Decimal(str(getattr(pos, "mark_price", "0")))
            position_value = abs(size) * mark_price
            margin = position_value / leverage
            position_state[symbol] = {
                "price": entry_price,
                "side": "buy" if size > 0 else "sell",
                "leverage": leverage,
                "size": abs(size),
                "value": position_value,
                "margin": margin
            }
            log_debug(f"📊 포지션 ({symbol})", 
                    f"수량: {abs(size)}, 진입가: {entry_price}, 방향: {'롱' if size > 0 else '숏'}, "
                    f"레버리지: {leverage}x, 포지션가치: {position_value}, 증거금: {margin}")
        else:
            position_state[symbol] = {
                "price": None, "side": None, "leverage": leverage,
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
            }
            log_debug(f"📊 포지션 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
        position_state[symbol] = {
            "price": None, "side": None, "leverage": Decimal("1"),
            "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
        }

def get_price(symbol):
    try:
        tickers = api.list_futures_tickers(SETTLE, contract=symbol)
        if tickers and hasattr(tickers[0], "last"):
            price = Decimal(str(tickers[0].last))
            log_debug(f"💲 가격 조회 ({symbol})", f"{price}")
            return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
    return Decimal("0")

def set_leverage(symbol):
    try:
        lev = SYMBOL_CONFIG[symbol].get("leverage", 2)
        leverage_data = PositionLeverage(
            leverage=str(int(lev)),
            mode="cross"  # 또는 "isolated"
        )
        api.update_position_leverage(SETTLE, symbol, leverage_data)
        log_debug(f"⚡ 레버리지 설정 완료 ({symbol})", f"{lev}x")
    except Exception as e:
        log_debug(f"❌ 레버리지 설정 실패 ({symbol})", str(e))

def get_max_qty(symbol, side):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        safe = get_account_info()
        price = get_price(symbol)
        if price <= 0:
            log_debug(f"❌ 가격 0 이하 ({symbol})", "최소 수량만 반환")
            return float(cfg["min_qty"])
        leverage = Decimal(str(cfg.get("leverage", 2)))
        order_value = safe * leverage
        raw_qty = order_value / price
        step = cfg["qty_step"]
        qty_decimal = (raw_qty // step) * step
        qty = max(qty_decimal, cfg["min_qty"])
        min_order_usdt = cfg.get("min_order_usdt", Decimal("5"))
        if qty * price < min_order_usdt:
            qty = cfg["min_qty"]
            log_debug(f"⚠️ 최소 주문 금액 미달 ({symbol})", f"{qty * price} USDT < {min_order_usdt} USDT, 최소 수량 사용")
        log_debug(f"📐 최대수량 계산 ({symbol})", 
                f"가용증거금: {safe}, 레버리지: {leverage}x, 가격: {price}, "
                f"주문가치: {order_value}, 최대수량: {qty}")
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e))
        return float(cfg["min_qty"])

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    try:
        if qty <= 0:
            log_debug("⛔ 수량 0 이하", symbol)
            return False
        set_leverage(symbol)  # 코인별 레버리지 설정
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        reduced_qty = Decimal(str(qty)) * POSITION_RATIO
        if symbol == "BTC_USDT":
            reduced_qty = (reduced_qty // Decimal("0.0001")) * Decimal("0.0001")
            if reduced_qty < Decimal("0.0001"):
                reduced_qty = Decimal("0.0001")
            str_qty = f"{float(reduced_qty):.4f}"
            log_debug(f"🔍 BTC 수량 변환", f"원래: {reduced_qty} → 변환: {str_qty}")
            reduced_qty = Decimal(str_qty)
        else:
            reduced_qty = (reduced_qty // step) * step
        reduced_qty = max(reduced_qty, cfg["min_qty"])
        size = str(reduced_qty) if side == "buy" else str(-reduced_qty)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api.create_futures_order(SETTLE, order)
        fill_price = result.fill_price if hasattr(result, 'fill_price') else "알 수 없음"
        log_debug(f"✅ 주문 ({symbol})", 
                f"{side.upper()} {reduced_qty} @ {fill_price} "
                f"(원래: {qty}의 33% = {Decimal(str(qty)) * POSITION_RATIO}, 최종: {reduced_qty})")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", error_msg)
        if retry > 0 and symbol == "BTC_USDT" and "INVALID_PARAM_VALUE" in error_msg:
            retry_qty = Decimal("0.0001")
            log_debug(f"🔄 BTC 최소단위 재시도", f"수량: {retry_qty}")
            time.sleep(1)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        if retry > 0 and ("INSUFFICIENT_AVAILABLE" in error_msg or "Bad Request" in error_msg):
            retry_qty = Decimal(str(qty)) * Decimal("0.2")
            retry_qty = (retry_qty // step) * step
            retry_qty = max(retry_qty, cfg["min_qty"])
            log_debug(f"🔄 주문 재시도 ({symbol})", f"수량 감소: {qty} → {retry_qty}")
            time.sleep(1)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False

def close_position(symbol):
    try:
        order = FuturesOrder(contract=symbol, size="0", price="0", tif="ioc", close=True)
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 청산 완료", symbol)
        time.sleep(0.5)
        update_position_state(symbol)
        account_cache["time"] = 0
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    payload = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": payload
                }))
                log_debug("📡 WebSocket", f"연결 성공 - {payload}")
                last_ping_time = time.time()
                last_msg_time = time.time()
                reconnect_delay = 5
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
                        if 'event' in data:
                            if data['event'] == 'subscribe':
                                log_debug("✅ 구독 완료", data.get('channel', ''))
                            continue
                        if "result" not in data:
                            continue
                        result = data["result"]
                        if not isinstance(result, dict):
                            continue
                        contract = result.get("contract")
                        last = result.get("last")
                        if not contract or not last or contract not in SYMBOL_CONFIG:
                            continue
                        last_price = Decimal(str(last))
                        state = position_state.get(contract, {})
                        entry_price = state.get("price")
                        side = state.get("side")
                        if not entry_price or not side:
                            continue
                        sl = SYMBOL_CONFIG[contract]["sl_pct"]
                        tp = SYMBOL_CONFIG[contract].get("tp_pct", None)
                        # SL
                        if (side == "buy" and last_price <= entry_price * (1 - sl)) or \
                           (side == "sell" and last_price >= entry_price * (1 + sl)):
                            log_debug(f"🛑 손절 발생 ({contract})", f"현재가: {last_price}, 진입가: {entry_price}, 손절폭: {sl}")
                            close_position(contract)
                            continue
                        # TP (익절)
                        if tp:
                            if (side == "buy" and last_price >= entry_price * (1 + tp)) or \
                               (side == "sell" and last_price <= entry_price * (1 - tp)):
                                log_debug(f"🎯 익절 발생 ({contract})", f"현재가: {last_price}, 진입가: {entry_price}, 익절폭: {tp}")
                                close_position(contract)
                                continue
                    except asyncio.TimeoutError:
                        pass
                    except Exception as e:
                        log_debug("❌ 메시지 처리 오류", str(e))
                        continue
        except Exception as e:
            log_debug("❌ WS 연결 오류", str(e))
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

def start_price_listener():
    for sym in SYMBOL_CONFIG:
        update_position_state(sym)
        # 레버리지 0 방지: 포지션 없을 때도 강제 세팅
        set_leverage(sym)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        if not request.is_json:
            log_debug("⚠️ 잘못된 요청", "JSON 형식이 아님")
            return jsonify({"error": "JSON 형식만 허용됩니다"}), 400
        data = request.get_json()
        log_debug("📥 웹훅 원본 데이터", json.dumps(data))
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw, raw)
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        strategy = data.get("strategy", "unknown")
        log_debug("📩 웹훅 수신", f"심볼: {symbol}, 신호: {side}, 액션: {action}, 전략: {strategy}")
        if side in ["buy"]: side = "long"
        if side in ["sell"]: side = "short"
        desired = "buy" if side == "long" else "sell"
        if symbol not in SYMBOL_CONFIG:
            log_debug("⚠️ 알 수 없는 심볼", symbol)
            return jsonify({"error": "지원하지 않는 심볼입니다"}), 400
        if side not in ["long", "short"]:
            log_debug("⚠️ 잘못된 방향", side)
            return jsonify({"error": "long 또는 short만 지원합니다"}), 400
        if action not in ["entry", "exit"]:
            log_debug("⚠️ 잘못된 액션", action)
            return jsonify({"error": "entry 또는 exit만 지원합니다"}), 400
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        current = state.get("side")
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "success", "message": "청산 완료", "symbol": symbol})
        if current and current != desired:
            log_debug(f"🔄 반대 포지션 감지 ({symbol})", f"{current} → {desired}로 전환")
            close_position(symbol)
            time.sleep(1)
        get_account_info(force=True)
        qty = get_max_qty(symbol, desired)
        place_order(symbol, desired, qty)
        return jsonify({
            "status": "success", 
            "message": "진입 완료", 
            "symbol": symbol,
            "side": desired
        })
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"status": "error", "message": "서버 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        positions = {}
        for symbol in SYMBOL_CONFIG:
            update_position_state(symbol)
            positions[symbol] = position_state.get(symbol, {})
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": {k: {sk: (float(sv) if isinstance(sv, Decimal) else sv) 
                            for sk, sv in v.items()} 
                        for k, v in positions.items()}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", f"WebSocket 리스너 실행됨 - 버전: 1.1.1")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
