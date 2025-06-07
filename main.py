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

# 피라미딩/중복신호 방지
last_signals = {}  # {symbol_side_action: timestamp}
position_counts = {}  # {symbol: count}

# ---------------------------- 코어 함수 (오류/보완) ----------------------------
def get_account_info(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        total_str = str(getattr(acc, 'total', '0')).replace('E', 'e')
        try:
            total_equity = Decimal(total_str)
        except (InvalidOperation, ValueError):
            total_equity = Decimal("100")
        if total_equity <= 0:
            total_equity = Decimal("100")
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
        price_str = str(ticker[0].last).replace('E', 'e')
        return Decimal(price_str)
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def calculate_position_size(symbol):
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_account_info(force=True)
    price = get_price(symbol)
    if price <= 0 or equity <= 0:
        return Decimal("0")
    try:
        raw_qty = equity / (price * cfg["contract_size"])
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        log_debug(f"📊 수량 계산 ({symbol})", f"계산값:{raw_qty} → 최종:{final_qty}")
        return final_qty
    except Exception as e:
        log_debug(f"❌ 수량 계산 오류 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

def sync_position(symbol):
    with position_lock:
        try:
            pos = api.get_position(SETTLE, symbol)
            old_state = position_state.get(symbol, {})
            old_size = old_state.get("size", 0)
            if pos.size == 0:
                if symbol in position_state and old_size > 0:
                    log_debug(f"📊 포지션 ({symbol})", "청산됨")
                    position_state.pop(symbol, None)
                    actual_entry_prices.pop(symbol, None)
                return True
            entry_price = Decimal(str(pos.entry_price))
            new_size = abs(pos.size)
            new_side = "long" if pos.size > 0 else "short"
            position_state[symbol] = {
                "size": new_size,
                "side": new_side,
                "entry": entry_price
            }
            if old_size != new_size or old_state.get("side") != new_side:
                log_debug(f"📊 포지션 변경 ({symbol})", f"{new_side} {new_size} 계약 | 진입가: {entry_price}")
            return True
        except Exception as e:
            log_debug(f"❌ 포지션 동기화 실패 ({symbol})", str(e), exc_info=True)
            return False

def execute_order(symbol, side, qty, retry=3):
    for attempt in range(retry):
        try:
            cfg = SYMBOL_CONFIG[symbol]
            qty_dec = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN)
            if qty_dec < cfg["min_qty"]:
                log_debug(f"⛔ 최소 수량 미달 ({symbol})", f"{qty_dec} < {cfg['min_qty']}")
                return False
            size = float(qty_dec) if side == "buy" else -float(qty_dec)
            order = FuturesOrder(
                contract=symbol,
                size=size,
                price="0",
                tif="ioc"
            )
            log_debug(f"📤 주문 시도 ({symbol})", f"{side} {qty_dec} 계약 ({attempt+1}/{retry})")
            created_order = api.create_futures_order(SETTLE, order)
            time.sleep(0.3)
            log_debug(f"✅ 주문 성공 ({symbol})", f"{side} {qty_dec} 계약")
            sync_position(symbol)
            return True
        except Exception as e:
            error = str(e)
            log_debug(f"❌ 주문 실패 ({symbol})", f"{error}")
            if "INSUFFICIENT_AVAILABLE" in error:
                cfg = SYMBOL_CONFIG[symbol]
                step = cfg["qty_step"]
                new_qty = (qty * Decimal("0.5")).quantize(step, rounding=ROUND_DOWN)
                new_qty = max(new_qty, cfg["min_qty"])
                log_debug(f"🔄 수량 조정 ({symbol})", f"{qty} → {new_qty}")
                if new_qty == qty or new_qty < cfg["min_qty"]:
                    break
                qty = new_qty
            time.sleep(0.5)
    return False

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
                order = FuturesOrder(
                    contract=symbol,
                    size=0,
                    price="0",
                    tif="ioc",
                    close=True
                )
                log_debug(f"🔄 청산 시도 ({symbol})", f"{current_size} 계약 ({attempt+1}/5)")
                api.create_futures_order(SETTLE, order)
                for check in range(10):
                    time.sleep(0.5)
                    current_pos = api.get_position(SETTLE, symbol)
                    if abs(Decimal(str(current_pos.size))) == 0:
                        log_debug(f"✅ 청산 완료 ({symbol})", f"{current_size} 계약")
                        position_state.pop(symbol, None)
                        actual_entry_prices.pop(symbol, None)
                        position_counts[symbol] = 0
                        return True
                log_debug(f"⚠️ 청산 미확인 ({symbol})", "재시도 중...")
            except Exception as e:
                log_debug(f"❌ 청산 실패 ({symbol})", f"{str(e)} ({attempt+1}/5)")
                time.sleep(1)
        log_debug(f"❌ 최종 청산 실패 ({symbol})", "5회 재시도 실패")
        return False
    finally:
        position_lock.release()

# ---------------------------- 웹훅 처리 (피라미딩/중복 신호 보완) ----------------------------
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
        action = data.get("action", "").lower()
        reason = data.get("reason", "")
        side = data.get("side", "").lower()
        # 중복 신호 차단(5초)
        signal_key = f"{symbol}_{action}_{side}"
        now = time.time()
        if signal_key in last_signals and now - last_signals[signal_key] < 5:
            log_debug("🚫 중복 신호 차단", signal_key)
            return jsonify({"status": "duplicate_blocked"}), 200
        last_signals[signal_key] = now
        with position_lock:
            # 청산 처리
            if action == "exit" or reason in ["reverse_signal", "take_profit", "stop_loss"]:
                success = close_position(symbol)
                return jsonify({"status": "success" if success else "error"})
            # 피라미딩 2회 제한
            if action == "entry":
                if position_counts.get(symbol, 0) >= 2:
                    log_debug(f"🚫 피라미딩 제한 ({symbol})", "최대 2회")
                    return jsonify({"status": "pyramiding_limit"}), 200
                position_counts[symbol] = position_counts.get(symbol, 0) + 1
                if side not in ["long", "short"]:
                    return jsonify({"error": "Invalid side"}), 400
                current_side = position_state.get(symbol, {}).get("side")
                desired_side = "buy" if side == "long" else "sell"
                if current_side and current_side != desired_side:
                    if not close_position(symbol):
                        return jsonify({"status": "error", "message": "역포지션 청산 실패"}), 500
                    for _ in range(10):
                        time.sleep(1)
                        sync_position(symbol)
                        if position_state.get(symbol, {}).get("size", 0) == 0:
                            break
                    else:
                        return jsonify({"status": "error", "message": "청산 확인 실패"}), 500
                qty = calculate_position_size(symbol)
                if qty <= 0:
                    return jsonify({"status": "error", "message": "수량 계산 오류"}), 400
                success = execute_order(symbol, desired_side, qty)
                return jsonify({"status": "success" if success else "error", "qty": float(qty)})
        return jsonify({"error": "Invalid action"}), 400
    except Exception as e:
        log_debug(f"❌ 웹훅 처리 실패 ({symbol or 'unknown'})", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    try:
        return "pong", 200
    except:
        return "error", 500

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_account_info(force=True)
        positions = {}
        for sym in SYMBOL_CONFIG:
            if sync_position(sym):
                pos = position_state.get(sym, {})
                if pos.get("side"):
                    positions[sym] = {k: float(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": positions,
            "actual_entry_prices": {k: float(v) for k, v in actual_entry_prices.items()},
            "position_counts": position_counts
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

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
        if not contract or not last or contract not in SYMBOL_CONFIG:
            return
        price = Decimal(str(last)).quantize(Decimal('1e-8'))
        with position_lock:
            pos = position_state.get(contract)
            if not pos or pos["size"] == 0:
                return
            entry = pos["entry"]
            cfg = SYMBOL_CONFIG[contract]
            trigger_count = 0
            for _ in range(3):
                if pos["side"] == "long":
                    if price <= entry * (1 - cfg["sl_pct"]) or price >= entry * (1 + cfg["tp_pct"]):
                        trigger_count +=1
                else:
                    if price >= entry * (1 + cfg["sl_pct"]) or price <= entry * (1 - cfg["tp_pct"]):
                        trigger_count +=1
                time.sleep(1)
            if trigger_count >=2:
                log_debug(f"⚡ 트리거 확인 ({contract})", f"{trigger_count}/3회 충족")
                close_position(contract)
    except Exception as e:
        log_debug("❌ 티커 처리 실패", str(e), exc_info=True)

def backup_position_loop():
    while True:
        try:
            for sym in SYMBOL_CONFIG:
                sync_position(sym)
            time.sleep(300)
        except Exception as e:
            log_debug("❌ 백업 루프 오류", str(e))
            time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=backup_position_loop, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 서버 시작 (포트: {port})")
    app.run(host="0.0.0.0", port=port)
