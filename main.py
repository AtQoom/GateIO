import os
import json
import time
import asyncio
import threading
import websockets
import logging
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

# ---------------------------- 로그 설정 ----------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
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
        "tp_pct": Decimal("0.006")
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.001"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006")
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006")
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006")
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006")
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("0.1"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006")
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "contract_size": Decimal("10000"),
        "sl_pct": Decimal("0.0035"),
        "tp_pct": Decimal("0.006")
    }
}

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
actual_entry_prices = {}
last_signals = {}  
position_counts = {} 

# ---------------------------- 총 담보금 조회 (수정) ----------------------------
def get_total_collateral(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        total = Decimal(str(acc.total))
        account_cache.update({"time": now, "data": total})
        log_debug("💰 계정", f"총 담보금: {total} USDT")
        return total
    except Exception as e:
        log_debug("❌ 계정 조회 실패", str(e), exc_info=True)
        return Decimal("100")

# ---------------------------- 실시간 가격 조회 ----------------------------
def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        return Decimal(str(ticker[0].last))
    except Exception as e:
        log_debug("❌ 가격 조회 실패", str(e), exc_info=True)
        return Decimal("0")

# ---------------------------- 포지션 동기화 ----------------------------
def sync_position(symbol):
    with position_lock:
        try:
            pos = api.get_position(SETTLE, symbol)
            if pos.size == 0:
                if symbol in position_state:
                    log_debug(f"📊 포지션 ({symbol})", "청산됨")
                    position_state.pop(symbol, None)
                    actual_entry_prices.pop(symbol, None)
                return True
            entry_price = Decimal(str(pos.entry_price))
            position_state[symbol] = {
                "size": abs(pos.size),
                "side": "long" if pos.size > 0 else "short",
                "entry": entry_price
            }
            actual_entry_prices[symbol] = entry_price
            return True
        except Exception as e:
            log_debug(f"❌ 포지션 동기화 실패 ({symbol})", str(e), exc_info=True)
            return False

# ---------------------------- 실시간 SL/TP 모니터링 ----------------------------
async def price_monitor():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": list(SYMBOL_CONFIG.keys())
                }))
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "result" in data:
                        process_ticker(data["result"])
        except Exception as e:
            log_debug("❌ 웹소켓 오류", str(e))
            await asyncio.sleep(5)

def process_ticker(ticker_data):
    try:
        if isinstance(ticker_data, list):
            for item in ticker_data:
                process_ticker(item)
            return
        contract = ticker_data.get("contract")
        last = ticker_data.get("last")
        if not contract or contract not in SYMBOL_CONFIG:
            return
        price = Decimal(str(last))
        with position_lock:
            pos = position_state.get(contract)
            if not pos or pos["size"] == 0:
                return
            entry = pos["entry"]
            cfg = SYMBOL_CONFIG[contract]
            # SL/TP 3회 연속 트리거 시 청산
            trigger_count = 0
            for _ in range(3):
                if (pos["side"] == "long" and (price <= entry*(1-cfg["sl_pct"]) or price >= entry*(1+cfg["tp_pct"]))) or \
                   (pos["side"] == "short" and (price >= entry*(1+cfg["sl_pct"]) or price <= entry*(1-cfg["tp_pct"]))):
                    trigger_count +=1
                time.sleep(1)
            if trigger_count >=2:
                log_debug(f"⚡ 트리거 확인 ({contract})", f"{trigger_count}/3회 충족")
                close_position(contract)
    except Exception as e:
        log_debug("❌ 티커 처리 실패", str(e), exc_info=True)

# ---------------------------- 백그라운드 작업 ----------------------------
def backup_position_loop():
    while True:
        try:
            for sym in SYMBOL_CONFIG:
                sync_position(sym)
            time.sleep(300)
        except Exception as e:
            log_debug("❌ 백업 루프 오류", str(e))
            time.sleep(300)

# ---------------------------- 수량 계산 (총담보금 100%) ----------------------------
def calculate_position_size(symbol):
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral(force=True)
    price = get_price(symbol)
    if price <= 0 or equity <= 0:
        return Decimal("0")
    try:
        raw_qty = equity / (price * cfg["contract_size"])
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        return max(qty, cfg["min_qty"])
    except Exception as e:
        log_debug(f"❌ 수량 계산 오류 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

# ---------------------------- 주문 실행 (피라미딩 2회 제한) ----------------------------
def execute_order(symbol, side, qty):
    for attempt in range(3):
        try:
            cfg = SYMBOL_CONFIG[symbol]
            qty_dec = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN)
            if qty_dec < cfg["min_qty"]:
                log_debug(f"⛔ 최소 수량 미달 ({symbol})", f"{qty_dec} < {cfg['min_qty']}")
                return False
            # 피라미딩 2회 제한
            current_count = position_counts.get(symbol, 0)
            if current_count >= 2:
                log_debug(f"🚫 피라미딩 제한 ({symbol})", "최대 2회")
                return False
            size = float(qty_dec) if side == "buy" else -float(qty_dec)
            order = FuturesOrder(
                contract=symbol,
                size=size,
                price="0",
                tif="ioc"
            )
            api.create_futures_order(SETTLE, order)
            log_debug(f"✅ 주문 완료 ({symbol})", f"{side} {qty_dec} 계약")
            sync_position(symbol)
            return True
        except Exception as e:
            log_debug(f"❌ 주문 실패 ({symbol})", str(e))
            time.sleep(1)
    return False

# ---------------------------- 청산 로직 ----------------------------
def close_position(symbol):
    acquired = position_lock.acquire(timeout=10)
    if not acquired:
        log_debug(f"❌ 청산 실패 ({symbol})", "락 획득 시간 초과")
        return False
    try:
        for attempt in range(5):
            try:
                pos = api.get_position(SETTLE, symbol)
                current_size = abs(Decimal(str(pos.size)))
                if current_size == 0:
                    log_debug(f"📊 포지션 ({symbol})", "이미 청산됨")
                    position_counts[symbol] = 0
                    return True
                api.cancel_all_futures_orders(SETTLE, symbol)
                order = FuturesOrder(
                    contract=symbol,
                    size=0,
                    price="0",
                    tif="ioc",
                    close=True
                )
                api.create_futures_order(SETTLE, order)
                for check in range(10):
                    time.sleep(0.5)
                    if sync_position(symbol) and position_state.get(symbol, {}).get("size", 0) == 0:
                        log_debug(f"✅ 청산 완료 ({symbol})", f"{current_size} 계약")
                        position_counts[symbol] = 0
                        return True
                log_debug(f"⚠️ 청산 미확인 ({symbol})", "재시도 중...")
            except Exception as e:
                log_debug(f"❌ 청산 실패 ({symbol})", str(e))
                time.sleep(1)
        return False
    finally:
        position_lock.release()

# ---------------------------- 웹훅 처리 ----------------------------
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        symbol = BINANCE_TO_GATE_SYMBOL.get(data["symbol"].upper())
        if not symbol:
            return jsonify({"error": "Invalid symbol"}), 400
        action = data.get("action", "").lower()
        signal_key = f"{symbol}_{action}"
        # 5초 내 중복 신호 차단
        if signal_key in last_signals and time.time() - last_signals[signal_key] < 5:
            return jsonify({"status": "duplicate_blocked"}), 200
        last_signals[signal_key] = time.time()
        with position_lock:
            if action == "exit":
                success = close_position(symbol)
                return jsonify({"status": "success" if success else "error"})
            if action == "entry":
                if position_counts.get(symbol, 0) >= 2:
                    return jsonify({"status": "pyramiding_limit"}), 200
                current_side = position_state.get(symbol, {}).get("side")
                desired_side = "buy" if data.get("side") == "long" else "sell"
                if current_side and current_side != desired_side:
                    if not close_position(symbol):
                        return jsonify({"status": "error"}), 500
                    time.sleep(3)
                qty = calculate_position_size(symbol)
                if qty <= 0:
                    return jsonify({"status": "error"}), 400
                success = execute_order(symbol, desired_side, qty)
                if success:
                    position_counts[symbol] = position_counts.get(symbol, 0) + 1
                return jsonify({"status": "success" if success else "error"})
        return jsonify({"error": "Invalid action"}), 400
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e), exc_info=True)
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    threading.Thread(target=backup_position_loop, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 서버 시작 (포트: {port})")
    app.run(host="0.0.0.0", port=port)
