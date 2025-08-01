import os
import time
import threading
import queue
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
import pytz
import logging
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from gate_api import exceptions as gate_api_exceptions

# --- 로깅 ---
logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# --- API KEY 설정 및 Gate.io 객체생성 ---
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

KST = pytz.timezone('Asia/Seoul')

position_state = {}
position_lock = threading.RLock()
recent_signals = {}
signal_lock = threading.RLock()
task_q = queue.Queue(maxsize=100)
WORKER_COUNT = 4

COOLDOWN_SECONDS = 14
SL_RESCUE_THRESHOLD = Decimal("0.0001")
SL_RESCUE_MAX = 3
MAX_PYRAMID_ENTRIES = 5

SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "BTCUSDT.P": "BTC_USDT", "BTCUSDTPERP": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", "ETHUSDT.P": "ETH_USDT", "ETHUSDTPERP": "ETH_USDT",
    "SOLUSDT": "SOL_USDT", "SOLUSDT.P": "SOL_USDT", "SOLUSDTPERP": "SOL_USDT",
    "ADAUSDT": "ADA_USDT", "ADAUSDT.P": "ADA_USDT", "ADAUSDTPERP": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "SUIUSDT.P": "SUI_USDT", "SUIUSDTPERP": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "LINKUSDT.P": "LINK_USDT", "LINKUSDTPERP": "LINK_USDT",
    "PEPEUSDT": "PEPE_USDT", "PEPEUSDT.P": "PEPE_USDT", "PEPEUSDTPERP": "PEPE_USDT",
    "XRPUSDT": "XRP_USDT", "XRPUSDT.P": "XRP_USDT", "XRPUSDTPERP": "XRP_USDT",
    "DOGEUSDT": "DOGE_USDT", "DOGEUSDT.P": "DOGE_USDT", "DOGEUSDTPERP": "DOGE_USDT",
    "ONDOUSDT": "ONDO_USDT", "ONDOUSDT.P": "ONDO_USDT", "ONDOUSDTPERP": "ONDO_USDT",
}

SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("0.55")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("0.65")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("0.8")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
    "PEPE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000000"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.2")},
    "XRP_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"),
                 "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
    "DOGE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.2")},
    "ONDO_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
                  "min_notional": Decimal("5"), "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
}

def normalize_symbol(raw_symbol: str) -> str:
    key = str(raw_symbol).upper().strip()
    return SYMBOL_MAPPING.get(key, key)

def _get_api_response(api_call, *args, **kwargs):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return api_call(*args, **kwargs)
        except gate_api_exceptions.ApiException as e:
            logger.error(f"API Exception: {e.status}, {e.reason}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error(f"API 최종 실패: {e}")
    return None

def fetch_position(symbol: str):
    try:
        pos_info = _get_api_response(api.get_position, SETTLE, symbol)
        if not pos_info or not pos_info.size:
            return None
        size = Decimal(str(pos_info.size))
        return {
            "avg_entry_price": Decimal(str(pos_info.entry_price)),
            "size": abs(size),
            "side": "buy" if size > 0 else "sell"
        }
    except Exception as e:
        logger.error(f"fetch_position error ({symbol}): {e}")
        return None

def fetch_market_price(symbol: str) -> Decimal:
    try:
        tickers = _get_api_response(api.list_futures_tickers, SETTLE, contract=symbol)
        if tickers and len(tickers) > 0:
            return Decimal(str(tickers[0].last))
        logger.warning(f"fetch_market_price failed for {symbol}")
    except Exception as e:
        logger.error(f"fetch_market_price error: {e}")
    return Decimal("0")

# **이전 코드 기반 자산조회 함수로 복원**
def get_total_collateral(force=False):
    try:
        data = api.get_futures_account(SETTLE)
        return Decimal(str(data.total)) if data and data.total else Decimal("0")
    except Exception as e:
        logger.error(f"[자산조회] 에러: {e}", exc_info=True)
        return Decimal("0")

def execute_market_order(symbol, qty, side):
    try:
        qty = float(qty)
        order = FuturesOrder(
            contract=symbol,
            size=qty if side == "buy" else -qty,
            price=None,
            tif="ioc"
        )
        result = api.create_futures_order(SETTLE, order)
        logger.info(f"[주문성공] {symbol} {side} qty={qty} result={result}")
        return True
    except Exception as e:
        logger.error(f"[주문실패] {symbol} {side} qty={qty}: {e}", exc_info=True)
        return False

def calculate_dynamic_sl(entry_price: Decimal, elapsed_sec: int, sl_base: Decimal, sl_decay_sec: int, sl_decay_amt: Decimal, sl_multiplier: Decimal, sl_min: Decimal) -> Decimal:
    periods = elapsed_sec // sl_decay_sec
    decay_total = periods * sl_decay_amt * sl_multiplier
    return max(sl_min, sl_base - decay_total)

def is_duplicate_signal(data):
    with signal_lock:
        now = time.time()
        signal_id = data.get("id", "")
        symbol = data.get("symbol", "")
        side = data.get("side", "")
        key = f"{symbol}_{side}"
        if signal_id and signal_id in recent_signals and now - recent_signals[signal_id] < COOLDOWN_SECONDS:
            logger.info(f"중복신호 {signal_id} 무시됨")
            return True
        if key in recent_signals and now - recent_signals[key] < COOLDOWN_SECONDS:
            logger.info(f"중복신호 {key} 무시됨")
            return True
        if signal_id:
            recent_signals[signal_id] = now
        recent_signals[key] = now
        return False

def on_entry_signal_received(symbol_raw: str, side_raw: str):
    symbol = normalize_symbol(symbol_raw)
    side = "buy" if side_raw.lower() == "long" else "sell"
    with position_lock:
        existing = position_state.get(symbol)
        entry_count = existing["entry_count"] if existing else 0
        if entry_count >= MAX_PYRAMID_ENTRIES:
            logger.info(f"[Entry] {symbol} 최대 진입 {MAX_PYRAMID_ENTRIES}회 초과 진입 무시")
            return
        position_state[symbol] = {
            "entry_count": entry_count + 1,
            "sl_rescue_count": existing.get("sl_rescue_count", 0) if existing else 0,
            "entry_time": time.time(),
            "stored_sl_pct": Decimal("0.04"),
            "stored_tp_pct": Decimal("0.006"),
            "sl_min_pct": Decimal("0.0009"),
            "sl_decay_sec": 15,
            "sl_decay_amt": Decimal("0.00004"),
            "tp_min_pct": Decimal("0.0012"),
            "tp_decay_sec": 15,
            "tp_decay_amt": Decimal("0.00002"),
            "side": side
        }
    qty = Decimal("1")  # 실제 운영시 자산 조회 반영한 주문 수량 공식 추천
    success = execute_market_order(symbol, qty, side)
    logger.info(f"[Entry] {symbol} {side} 주문 실행 결과: {success}")

def is_sl_rescue_condition(symbol: str) -> bool:
    with position_lock:
        s = position_state.get(symbol)
        if not s or s.get("sl_rescue_count", 0) >= SL_RESCUE_MAX:
            return False
        pos = fetch_position(symbol)
        if not pos or pos["size"] == 0:
            return False
        elapsed = int(time.time() - s["entry_time"])
        cfg = SYMBOL_CONFIG.get(symbol, {"sl_mult": Decimal("1.0")})
        sl_mult = Decimal(cfg.get("sl_mult", "1.0"))
        sl_base = s.get("stored_sl_pct", Decimal("0.04"))
        sl_min = s.get("sl_min_pct", Decimal("0.0009"))
        sl_decay_sec = s.get("sl_decay_sec", 15)
        sl_decay_amt = s.get("sl_decay_amt", Decimal("0.00004"))
        entry_price = pos["avg_entry_price"]
        side = pos["side"]
        current_price = fetch_market_price(symbol)
        dynamic_sl_pct = calculate_dynamic_sl(entry_price, elapsed, sl_base, sl_decay_sec, sl_decay_amt, sl_mult, sl_min)
        sl_price = entry_price * (Decimal("1.0") - dynamic_sl_pct) if side == "buy" else entry_price * (Decimal("1.0") + dynamic_sl_pct)
        near_sl = abs(current_price - sl_price) / sl_price <= SL_RESCUE_THRESHOLD
        is_loss = (side == "buy" and current_price < entry_price) or (side == "sell" and current_price > entry_price)
        return near_sl and is_loss

def run_sl_rescue(symbol: str):
    with position_lock:
        s = position_state.get(symbol)
        if not s or s.get("sl_rescue_count", 0) >= SL_RESCUE_MAX:
            return
        pos = fetch_position(symbol)
        if not pos or pos["size"] == 0:
            return
        if is_sl_rescue_condition(symbol):
            base_ratios = [Decimal("0.0020"), Decimal("0.0030"), Decimal("0.0070"), Decimal("0.0160"), Decimal("0.0500")]
            idx = s.get("entry_count", 0)
            if idx >= len(base_ratios):
                logger.warning(f"[{symbol}] SL-Rescue 단계 초과: {idx}")
                return
            cfg = SYMBOL_CONFIG.get(symbol, {})
            symbol_mult = Decimal(cfg.get("tp_mult", "1.0"))
            base_ratio = base_ratios[idx] * symbol_mult
            qty = pos["size"] * base_ratio * Decimal("1.5")
            qty = qty.quantize(cfg.get("qty_step", Decimal("1")), rounding=ROUND_DOWN)
            side = pos["side"]
            success = execute_market_order(symbol, qty, side)
            if success:
                s["entry_count"] = idx + 1
                s["sl_rescue_count"] += 1
                s["entry_time"] = time.time()
                logger.info(f"[SL-Rescue] 진입: {symbol} qty={qty} side={side}")
            else:
                logger.error(f"[SL-Rescue] 주문 실패: {symbol}")

def sl_rescue_monitor():
    while True:
        with position_lock:
            symbols = list(position_state.keys())
        for sym in symbols:
            try:
                run_sl_rescue(sym)
            except Exception as e:
                logger.error(f"SL-Rescue 실행 예외({sym}): {e}")
        time.sleep(0.2)

def worker(worker_id):
    logger.info(f"워커-{worker_id} 시작됨")
    while True:
        try:
            task = task_q.get(timeout=1)
            symbol, action, data = task
            logger.debug(f"워커-{worker_id} 작업 실행 중: {symbol} - {action} - {data}")
            if action == "entry":
                on_entry_signal_received(symbol, data)
            task_q.task_done()
        except queue.Empty:
            continue
        except Exception as ex:
            logger.error(f"워커-{worker_id} 작업 오류: {ex}")

def enqueue_task(symbol: str, action: str, data):
    try:
        task_q.put_nowait((symbol, action, data))
        logger.debug(f"작업 큐 추가됨: {symbol} {action}")
    except queue.Full:
        logger.error("작업 큐 가득참")

def update_position_state(symbol: str):
    with position_lock:
        pos = fetch_position(symbol)
        if pos:
            state = position_state.get(symbol, {})
            state.update({"side": pos.get("side"), "size": pos.get("size"), "price": pos.get("avg_entry_price")})
            position_state[symbol] = state
            logger.info(f"포지션 상태 업데이트: {symbol} {state}")
        else:
            logger.info(f"포지션 없음: {symbol}")

app = Flask(__name__)

@app.route("/", methods=["GET"])
def root_status():
    with position_lock:
        keys = list(position_state.keys())
        uptime_sec = int(time.time() - min([s.get("entry_time", time.time()) for s in position_state.values()] or [time.time()]))
    return jsonify({
        "status": "ok",
        "time_KST": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "position_state_keys": keys,
        "uptime_sec": uptime_sec
    }), 200

@app.route("/webhook", methods=["POST"])
def webhook_handler():
    try:
        data = request.json or {}
        action = data.get("action")
        symbol_raw = data.get("symbol")
        side_raw = data.get("side")
        if action == "entry" and symbol_raw and side_raw:
            if is_duplicate_signal(data):
                return jsonify({"result": "duplicate signal ignored"}), 200
            enqueue_task(normalize_symbol(symbol_raw), "entry", side_raw)
            return jsonify({"result": "entry signal queued"}), 200
        return jsonify({"error": "unsupported action or missing params"}), 400
    except Exception as e:
        logger.error(f"웹훅 에러: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug_status():
    symbol = request.args.get("symbol")
    with position_lock:
        if symbol:
            key = normalize_symbol(symbol)
            s = position_state.get(key)
            if not s:
                return jsonify({"error": "symbol not found"}), 404
            sanitized = {k: str(v) if isinstance(v, Decimal) else v for k, v in s.items()}
            return jsonify({key: sanitized}), 200
        sanitized = {k: {kk: str(vv) if isinstance(vv, Decimal) else vv for kk, vv in v.items()} for k, v in position_state.items()}
        return jsonify(sanitized), 200

if __name__ == "__main__":
    logger.info("[서버시작] Gate.io 자동매매 서버 (자산조회 최신 적용)")
    equity = get_total_collateral(force=True)
    logger.info(f"초기 자산: {equity} USDT")

    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)

    threading.Thread(target=sl_rescue_monitor, daemon=True).start()
    logger.info("SL-Rescue 감시 스레드 가동 중")

    for i in range(WORKER_COUNT):
        threading.Thread(target=worker, args=(i,), daemon=True).start()
        logger.info(f"워커-{i} 시작")

    port = int(os.environ.get("PORT", 5000))
    logger.info(f"웹 서버 실행 포트: {port}")
    app.run(host="0.0.0.0", port=port)
