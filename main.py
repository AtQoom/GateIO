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

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10"),
        "sl_pct": Decimal("0.0075"),
        "tp_pct": Decimal("0.008"),
        "leverage": 3
    },
    "BTC_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.0001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 3
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0075"),
        "tp_pct": Decimal("0.008"),
        "leverage": 3
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0075"),
        "tp_pct": Decimal("0.008"),
        "leverage": 3
    }
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}
account_cache = {"time": 0, "data": None}

def log_debug(tag, msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {msg}")

def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 1 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        avail = Decimal(str(acc.available))
        account_cache.update({"time": now, "data": avail})
        log_debug("💰 계정", f"가용 잔고: {avail}")
        return avail
    except Exception as e:
        log_debug("❌ 계정 조회 실패", str(e))
        return Decimal("0")

def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        price = Decimal(str(ticker[0].last))
        log_debug(f"💲 가격 ({symbol})", f"{price}")
        return price
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
        return Decimal("0")

def get_max_qty(symbol, side):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        safe = get_account_info(force=True)
        price = get_price(symbol)
        lev = cfg["leverage"]
        step = cfg["qty_step"]
        min_qty = cfg["min_qty"]
        contract_size = cfg["contract_size"]

        if price <= 0:
            return float(min_qty)

        safe_margin = safe * Decimal("0.99")
        order_value = safe_margin * lev
        raw_qty = order_value / (price * contract_size)
        qty = (raw_qty // step) * step
        qty = max(qty, min_qty)

        log_debug(f"📊 수량 계산 ({symbol})", 
            f"잔고:{safe}, 레버리지:{lev}, 가격:{price}, 계약단위:{contract_size}, 최종:{qty} (계약)")
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e))
        return float(SYMBOL_CONFIG[symbol]["min_qty"])

def update_position_state(symbol):
    try:
        pos = api.get_position(SETTLE, symbol)
        current_leverage = Decimal(str(pos.leverage))
        target_leverage = SYMBOL_CONFIG[symbol]["leverage"]
        if hasattr(pos, "margin_mode") and pos.margin_mode != "cross":
            api.update_position_margin_mode(SETTLE, symbol, "cross")
            log_debug(f"⚙️ 마진모드 변경 ({symbol})", f"{pos.margin_mode} → cross")
        if current_leverage != target_leverage:
            api.update_position_leverage(SETTLE, symbol, target_leverage, "cross")
            log_debug(f"⚙️ 레버리지 변경 ({symbol})", f"{current_leverage} → {target_leverage}x (교차)")
        size = Decimal(str(pos.size))
        if size != 0:
            entry = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            margin = value / target_leverage
            position_state[symbol] = {
                "price": entry, "side": "buy" if size > 0 else "sell",
                "leverage": target_leverage, "size": abs(size),
                "value": value, "margin": margin,
                "mode": getattr(pos, "margin_mode", "cross")
            }
            log_debug(f"📊 포지션 상태 ({symbol})", f"진입가: {entry}, 사이즈: {abs(size)}, 모드: {position_state[symbol]['mode']}")
        else:
            position_state[symbol] = {
                "price": None, "side": None, "leverage": target_leverage,
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
            }
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        min_qty = cfg["min_qty"]

        qty_dec = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        if qty_dec % step != Decimal('0') or qty_dec < min_qty:
            log_debug(f"⛔ 잘못된 수량 ({symbol})", f"{qty_dec} (단위: {step})")
            return False

        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 ({symbol})", f"{side.upper()} {float(qty_dec)} (계약)")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", error_msg)
        if retry > 0 and "INVALID_PARAM" in error_msg:
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // step) * step
            retry_qty = max(retry_qty, min_qty)
            log_debug(f"🔄 재시도 ({symbol})", f"{qty} → {retry_qty}")
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False

def close_position(symbol):
    try:
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
        return False

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    log_debug("🏓 핑", "UptimeRobot 상태 체크")
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    symbol = None
    try:
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400

        data = request.get_json()
        log_debug("📥 웹훅", json.dumps(data))

        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400

        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        if side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "Invalid side/action"}), 400

        log_debug("📩 웹훅 수신", f"Symbol: {symbol}, Action: {action}, Side: {side}")

        update_position_state(symbol)
        desired = "buy" if side == "long" else "sell"
        current = position_state.get(symbol, {}).get("side")

        if action == "exit":
            if close_position(symbol):
                return jsonify({"status": "success", "message": "Position closed"})
            else:
                return jsonify({"status": "error", "message": "Close failed"}), 500

        if current and current != desired:
            if not close_position(symbol):
                return jsonify({"status": "error", "message": "Reverse close failed"}), 500
            time.sleep(1)
            update_position_state(symbol)

        qty = get_max_qty(symbol, desired)
        success = place_order(symbol, desired, qty)
        return jsonify({"status": "success" if success else "error", "qty": qty})

    except Exception as e:
        error_symbol = symbol or "unknown"
        log_debug(f"❌ 웹훅 오류 ({error_symbol})", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
            pos = position_state.get(sym, {})
            positions[sym] = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in pos.items()}
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": positions
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    reconnect_delay = 5
    max_delay = 60

    while True:
        try:
            async with websockets.connect(uri, ping_interval=30) as ws:
                log_debug("📡 웹소켓", f"Connected to {uri}")
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                reconnect_delay = 5

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)

                        if not isinstance(data, dict):
                            continue

                        if "event" in data and data["event"] == "subscribe":
                            log_debug("✅ 웹소켓 구독", data.get("channel", ""))
                            continue

                        result = data.get("result")
                        if not isinstance(result, dict):
                            continue

                        contract = result.get("contract")
                        last = result.get("last")

                        if contract and last and contract in SYMBOL_CONFIG:
                            update_position_state(contract)
                            price = Decimal(str(last))
                            pos = position_state.get(contract, {})
                            entry = pos.get("price")

                            if entry and pos.get("side"):
                                cfg = SYMBOL_CONFIG[contract]
                                side = pos["side"]

                                if side == "buy":
                                    sl_price = entry * (1 - cfg["sl_pct"])
                                    tp_price = entry * (1 + cfg["tp_pct"])
                                    if price <= sl_price:
                                        log_debug(f"🛑 손절 트리거 ({contract})", f"현재가: {price}, 손절가: {sl_price}")
                                        close_position(contract)
                                        update_position_state(contract)
                                    elif price >= tp_price:
                                        log_debug(f"🎯 익절 트리거 ({contract})", f"현재가: {price}, 익절가: {tp_price}")
                                        close_position(contract)
                                        update_position_state(contract)
                                else:
                                    sl_price = entry * (1 + cfg["sl_pct"])
                                    tp_price = entry * (1 - cfg["tp_pct"])
                                    if price >= sl_price:
                                        log_debug(f"🛑 손절 트리거 ({contract})", f"현재가: {price}, 손절가: {sl_price}")
                                        close_position(contract)
                                        update_position_state(contract)
                                    elif price <= tp_price:
                                        log_debug(f"🎯 익절 트리거 ({contract})", f"현재가: {price}, 익절가: {tp_price}")
                                        close_position(contract)
                                        update_position_state(contract)

                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        log_debug("⚠️ 웹소켓", "연결 재시도 중...")
                        break
                    except Exception as e:
                        log_debug("❌ 웹소켓 메시지 처리 실패", str(e))
                        continue

        except Exception as e:
            log_debug("❌ 웹소켓 연결 실패", f"{str(e)} - {reconnect_delay}초 후 재시도")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

def start_ws_listener():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

if __name__ == "__main__":
    threading.Thread(target=start_ws_listener, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
