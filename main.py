import os
import json
import time
import asyncio
import threading
import websockets
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

# ---------------------------- 로그 설정 ----------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s'))
logger.addHandler(console_handler)

def log_debug(tag, msg, exc_info=False):
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception(msg)

# ---------------------------- API 클라이언트 패치 ----------------------------
class PatchedApiClient(ApiClient):
    def __call_api(self, *args, **kwargs):
        kwargs['headers']['Timestamp'] = str(int(time.time()))
        return super().__call_api(*args, **kwargs)

# ---------------------------- 서버 초기화 ----------------------------
app = Flask(__name__)
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
SETTLE = "usdt"

if not API_KEY or not API_SECRET:
    logger.critical("환경변수 API_KEY/API_SECRET 미설정")
    raise RuntimeError("API 키를 설정해야 합니다.")

config = Configuration(key=API_KEY, secret=API_SECRET)
client = PatchedApiClient(config)
api = FuturesApi(client)

# ---------------------------- 거래 설정 ----------------------------
BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT",
    "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT"
}

SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.0001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10000"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006"),
        "leverage": 2
    }
}

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
actual_entry_prices = {}

# ---------------------------- 코어 함수 ----------------------------
def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        total_str = str(acc.total) if hasattr(acc, 'total') else "0"
        total_equity = Decimal(total_str.upper().replace("E", "e"))
        if total_equity < Decimal("10"):
            total_equity = Decimal("100")  # 최소 100 USDT fallback
        account_cache.update({"time": now, "data": total_equity})
        log_debug("💰 계정", f"총 담보금: {total_equity} USDT")
        return total_equity
    except Exception as e:
        log_debug("❌ 계정 조회 실패", str(e), exc_info=True)
        return Decimal("100")

def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker or len(ticker) == 0:
            return Decimal("0")
        price_str = str(ticker[0].last).upper().replace("E", "e")
        return Decimal(price_str).normalize()
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def get_max_qty(symbol, side):
    try:
        cfg = SYMBOL_CONFIG[symbol]
        total_equity = get_account_info(force=True)
        price = get_price(symbol)
        if price <= 0:
            return float(cfg["min_qty"])
        # (총담보금 / (현재가 × 계약크기)) × 2
        base_qty = (total_equity / (price * cfg["contract_size"]))
        base_qty = base_qty.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
        qty = (base_qty * 2).quantize(cfg["qty_step"], rounding=ROUND_DOWN)
        qty = (qty // cfg["qty_step"]) * cfg["qty_step"]
        qty = max(qty, cfg["min_qty"])
        log_debug(f"📊 수량 계산 ({symbol})", f"총담보금:{total_equity}, 현재가:{price}, 계약크기:{cfg['contract_size']}, 2배수량:{qty}")
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e), exc_info=True)
        return float(cfg["min_qty"])

def update_position_state(symbol, timeout=5):
    acquired = position_lock.acquire(timeout=timeout)
    if not acquired:
        return False
    try:
        try:
            pos = api.get_position(SETTLE, symbol)
        except Exception as e:
            if "POSITION_NOT_FOUND" in str(e):
                if symbol in position_state and position_state[symbol].get("size", 0) > 0:
                    log_debug(f"📊 포지션 ({symbol})", "청산됨")
                position_state[symbol] = {
                    "price": None, "side": None, "size": Decimal("0"),
                    "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
                }
                if symbol in actual_entry_prices:
                    del actual_entry_prices[symbol]
                return True
            else:
                return False
        size = Decimal(str(pos.size))
        if size != 0:
            old_state = position_state.get(symbol, {})
            old_size = old_state.get("size", 0)
            old_side = old_state.get("side")
            api_entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            actual_price = actual_entry_prices.get(symbol)
            entry_price = actual_price if actual_price else api_entry_price
            new_side = "buy" if size > 0 else "sell"
            position_state[symbol] = {
                "price": entry_price, "side": new_side,
                "size": abs(size), "value": abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"],
                "margin": (abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]) / SYMBOL_CONFIG[symbol]["leverage"],
                "mode": "cross"
            }
            if old_size != abs(size) or old_side != new_side:
                log_debug(f"📊 포지션 변경 ({symbol})", f"{new_side} {abs(size)} 계약, 진입가: {entry_price}")
        else:
            if symbol in position_state and position_state[symbol].get("size", 0) > 0:
                log_debug(f"📊 포지션 ({symbol})", "청산됨")
            position_state[symbol] = {
                "price": None, "side": None, "size": Decimal("0"),
                "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
            }
            if symbol in actual_entry_prices:
                del actual_entry_prices[symbol]
        return True
    finally:
        position_lock.release()

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        return False
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        min_qty = cfg["min_qty"]
        qty_dec = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        if qty_dec < min_qty:
            return False
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        log_debug(f"📤 주문 시도 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        time.sleep(2)
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", f"{error_msg}")
        if retry > 0 and ("INVALID_PARAM" in error_msg or "POSITION_EMPTY" in error_msg or "INSUFFICIENT_AVAILABLE" in error_msg):
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // step) * step
            retry_qty = max(retry_qty, min_qty)
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False
    finally:
        position_lock.release()

def close_position(symbol):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        return False
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "")
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "")
        if symbol in actual_entry_prices:
            del actual_entry_prices[symbol]
        time.sleep(1)
        update_position_state(symbol)
        return True
    except Exception as e:
        log_debug(f"❌ 청산 실패 ({symbol})", str(e))
        return False
    finally:
        position_lock.release()

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/", methods=["POST"])
def webhook():
    symbol = None
    try:
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
        data = request.get_json()
        log_debug("📥 웹훅", f"수신: {json.dumps(data)}")
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        reason = data.get("reason", "")

        if action == "exit" and reason == "reverse_signal":
            success = close_position(symbol)
            log_debug(f"🔁 반대 신호 청산 ({symbol})", f"성공: {success}")
            return jsonify({"status": "success" if success else "error"})

        if side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "Invalid side/action"}), 400
        if not update_position_state(symbol, timeout=1):
            return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
        current_side = position_state.get(symbol, {}).get("side")
        desired_side = "buy" if side == "long" else "sell"
        if action == "exit":
            success = close_position(symbol)
            log_debug(f"🔁 알림 청산 ({symbol})", f"성공: {success}")
            return jsonify({"status": "success" if success else "error"})
        if current_side and current_side != desired_side:
            if not close_position(symbol):
                return jsonify({"status": "error", "message": "역포지션 청산 실패"})
            time.sleep(3)
            if not update_position_state(symbol):
                return jsonify({"status": "error", "message": "포지션 갱신 실패"})
        qty = get_max_qty(symbol, desired_side)
        if qty <= 0:
            return jsonify({"status": "error", "message": "수량 계산 오류"})
        success = place_order(symbol, desired_side, qty)
        return jsonify({"status": "success" if success else "error", "qty": qty})
    except Exception as e:
        log_debug(f"❌ 웹훅 실패 ({symbol or 'unknown'})", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            if update_position_state(sym, timeout=1):
                pos = position_state.get(sym, {})
                if pos.get("side"):
                    positions[sym] = {k: float(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": positions,
            "actual_entry_prices": {k: float(v) for k, v in actual_entry_prices.items()}
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

async def send_ping(ws):
    while True:
        try:
            await ws.ping()
        except Exception as e:
            break
        await asyncio.sleep(30)

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    while True:
        try:
            log_debug("📡 웹소켓", "연결 시도")
            async with websockets.connect(uri, ping_interval=30, ping_timeout=15) as ws:
                log_debug("📡 웹소켓", "연결 성공")
                subscribe_msg = {
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }
                await ws.send(json.dumps(subscribe_msg))
                ping_task = asyncio.create_task(send_ping(ws))
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=45)
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("event") == "subscribe":
                            log_debug("✅ 웹소켓", "티커 구독 완료")
                            continue
                        result = data.get("result")
                        if not result:
                            continue
                        if isinstance(result, list):
                            for item in result:
                                if isinstance(item, dict):
                                    process_ticker_data(item)
                        elif isinstance(result, dict):
                            process_ticker_data(result)
                    except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                        log_debug("⚠️ 웹소켓", "연결 끊김, 재연결 시도")
                        ping_task.cancel()
                        break
                    except Exception as e:
                        continue
        except Exception as e:
            log_debug("❌ 웹소켓", f"연결 실패: {str(e)}")
            await asyncio.sleep(5)

def process_ticker_data(ticker):
    try:
        contract = ticker.get("contract")
        last = ticker.get("last")
        if not contract or not last or contract not in SYMBOL_CONFIG:
            return
        real_price = Decimal(str(last).replace("E", "e")).normalize()
        acquired = position_lock.acquire(timeout=1)
        if not acquired:
            return
        try:
            if not update_position_state(contract, timeout=1):
                return
            pos = position_state.get(contract, {})
            entry = pos.get("price")
            size = pos.get("size", 0)
            side = pos.get("side")
            if not entry or size <= 0 or side not in ["buy", "sell"]:
                return
            cfg = SYMBOL_CONFIG[contract]
            if side == "buy":
                sl = entry * (1 - cfg["sl_pct"])
                tp = entry * (1 + cfg["tp_pct"])
                if real_price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{real_price} <= SL:{sl}")
                    close_position(contract)
                elif real_price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{real_price} >= TP:{tp}")
                    close_position(contract)
            else:
                sl = entry * (1 + cfg["sl_pct"])
                tp = entry * (1 - cfg["tp_pct"])
                if real_price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가:{real_price} >= SL:{sl}")
                    close_position(contract)
                elif real_price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가:{real_price} <= TP:{tp}")
                    close_position(contract)
        finally:
            position_lock.release()
    except Exception as e:
        log_debug("❌ 티커 처리 실패", str(e))

def backup_position_loop():
    while True:
        try:
            for sym in SYMBOL_CONFIG:
                update_position_state(sym, timeout=1)
            time.sleep(300)
        except Exception as e:
            time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    threading.Thread(target=backup_position_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버", f"Railway 포트 {port}에서 시작")
    app.run(host="0.0.0.0", port=port)
