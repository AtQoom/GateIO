import os
import time
import threading
import queue
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
import pytz
import logging
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi
from gate_api import exceptions as gate_api_exceptions

# --- 로거 설정 ---
logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# --- 환경변수 API키 및 Gate.io 설정 ---
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

# --- KST 타임존 ---
KST = pytz.timezone('Asia/Seoul')

# --- 전역 상태 ---
position_state = {}
position_lock = threading.RLock()
recent_signals = {}
signal_lock = threading.RLock()
task_q = queue.Queue(maxsize=100)

# --- 주요 상수 ---
COOLDOWN_SECONDS = 14
SL_RESCUE_THRESHOLD = Decimal("0.0001")   # 0.01%
SL_RESCUE_MAX = 3

# --- 심볼매핑 (쉼표 누락 수정 포함) ---
SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "BTCUSDT.P": "BTC_USDT", "BTCUSDTPERP": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", "ETHUSDT.P": "ETH_USDT", "ETHUSDTPERP": "ETH_USDT",
    "SOLUSDT": "SOL_USDT", "SOLUSDT.P": "SOL_USDT", "SOLUSDTPERP": "SOL_USDT",
    "ADAUSDT": "ADA_USDT", "ADAUSDT.P": "ADA_USDT", "ADAUSDTPERP": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "SUIUSDT.P": "SUI_USDT", "SUIUSDTPERP": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "LINKUSDT.P": "LINK_USDT", "LINKUSDTPERP": "LINK_USDT",
    "PEPEUSDT": "PEPE_USDT", "PEPEUSDT.P": "PEPE_USDT", "PEPEUSDTPERP": "PEPE_USDT",  # 쉼표 있음!
    "XRPUSDT": "XRP_USDT", "XRPUSDT.P": "XRP_USDT", "XRPUSDTPERP": "XRP_USDT",
    "DOGEUSDT": "DOGE_USDT", "DOGEUSDT.P": "DOGE_USDT", "DOGEUSDTPERP": "DOGE_USDT",
    "ONDOUSDT": "ONDO_USDT", "ONDOUSDT.P": "ONDO_USDT", "ONDOUSDTPERP": "ONDO_USDT",
}

# --- 심볼별 계약 사양 및 TP/SL 가중치 (Decimal 타입 일관성) ---
SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"), "min_notional": Decimal("5"),
                 "tp_mult": Decimal("1.0"), "sl_mult": Decimal("0.55")},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"), "min_notional": Decimal("5"),
                 "tp_mult": Decimal("1.0"), "sl_mult": Decimal("0.65")},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"),
                 "tp_mult": Decimal("1.0"), "sl_mult": Decimal("0.8")},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"),
                 "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"),
                 "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"),
                 "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
    "PEPE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000000"), "min_notional": Decimal("5"),
                  "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.2")},
    "XRP_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"),
                 "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
    "DOGE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"),
                  "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.2")},
    "ONDO_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"),
                 "tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")},
}

# --- 유틸리티 & API 호출 ---

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

# --- 동적 SL 계산 ---

def calculate_dynamic_sl(entry_price: Decimal, elapsed_sec: int,
                         sl_base: Decimal, sl_decay_sec: int, sl_decay_amt: Decimal,
                         sl_multiplier: Decimal, sl_min: Decimal) -> Decimal:
    periods = elapsed_sec // sl_decay_sec
    decay_total = periods * sl_decay_amt * sl_multiplier
    return max(sl_min, sl_base - decay_total)

# --- SL-Rescue 조건 체크 및 디버그 로그 포함 ---

def is_sl_rescue_condition(symbol: str) -> bool:
    with position_lock:
        state = position_state.get(symbol)
        if not state or state.get("sl_rescue_count", 0) >= SL_RESCUE_MAX:
            logger.debug(f"[{symbol}] SL-Rescue 조건 미충족: 상태없음 또는 최대 진입 횟수 초과")
            return False
        pos_info = fetch_position(symbol)
        if not pos_info or pos_info["size"] == 0:
            logger.debug(f"[{symbol}] SL-Rescue 조건 미충족: 포지션 정보 없음")
            return False
        elapsed = int(time.time() - state["entry_time"])
        cfg = SYMBOL_CONFIG.get(symbol, {"sl_mult": Decimal("1.0")})
        sl_mult = cfg.get("sl_mult", Decimal("1.0"))
        sl_mult = Decimal(sl_mult) if not isinstance(sl_mult, Decimal) else sl_mult

        sl_base = state.get("stored_sl_pct", Decimal("0.04"))
        sl_min = state.get("sl_min_pct", Decimal("0.0009"))
        sl_decay_sec = state.get("sl_decay_sec", 15)
        sl_decay_amt = state.get("sl_decay_amt", Decimal("0.00004"))
        entry_price = pos_info["avg_entry_price"]
        side = pos_info["side"]
        current_price = fetch_market_price(symbol)
        if current_price <= 0:
            logger.debug(f"[{symbol}] SL-Rescue 조건 미충족: 현재가 없음 또는 0")
            return False

        dynamic_sl_pct = calculate_dynamic_sl(entry_price, elapsed, sl_base, sl_decay_sec, sl_decay_amt, sl_mult, sl_min)
        sl_price = (entry_price * (Decimal("1.0") - dynamic_sl_pct)) if side == "buy" else (entry_price * (Decimal("1.0") + dynamic_sl_pct))
        near_sl = abs(current_price - sl_price) / sl_price <= SL_RESCUE_THRESHOLD
        is_loss = (side == "buy" and current_price < entry_price) or (side == "sell" and current_price > entry_price)

        logger.debug(f"[{symbol}] SL-Rescue 체크: 평단={entry_price}, 현재가={current_price}, dynamic_sl_pct={dynamic_sl_pct}, sl_price={sl_price}, near_sl={near_sl}, is_loss={is_loss}")

        if near_sl and is_loss:
            logger.info(f"[SL-Rescue 트리거] {symbol}: SL 근접 및 손실 상태, 현재가={current_price}, SL={sl_price}")

        return near_sl and is_loss

# --- SL-Rescue 실행 ---

def run_sl_rescue(symbol: str):
    with position_lock:
        state = position_state.get(symbol)
        if not state:
            logger.debug(f"[{symbol}] SL-Rescue 실행 실패: 상태 없음")
            return
        if state.get("sl_rescue_count", 0) >= SL_RESCUE_MAX:
            logger.debug(f"[{symbol}] SL-Rescue 실행 실패: 최대 추가진입 초과")
            return
        pos_info = fetch_position(symbol)
        if not pos_info or pos_info["size"] == 0:
            logger.debug(f"[{symbol}] SL-Rescue 실행 실패: 포지션 없음")
            return
        if is_sl_rescue_condition(symbol):
            base_ratios = [Decimal("0.0020"), Decimal("0.0030"), Decimal("0.0070"), Decimal("0.0160"), Decimal("0.0500")]
            entry_idx = state.get("entry_count", 0)
            if entry_idx >= len(base_ratios):
                logger.warning(f"[{symbol}] SL-Rescue 단계 인덱스 초과: {entry_idx}")
                return
            cfg = SYMBOL_CONFIG.get(symbol, {})
            symbol_mult = cfg.get("tp_mult", Decimal("1.0"))
            symbol_mult = Decimal(symbol_mult) if not isinstance(symbol_mult, Decimal) else symbol_mult
            base_ratio = base_ratios[entry_idx] * symbol_mult
            qty = pos_info["size"] * base_ratio * Decimal("1.5")
            qty = qty.quantize(cfg.get("qty_step", Decimal("1")), rounding=ROUND_DOWN)
            side = pos_info["side"]

            logger.info(f"[SL-Rescue] 시도: 심볼={symbol}, qty={qty}, side={side}")

            # TODO: 실제 시장가 주문 API 호출 구현
            # success = execute_market_order(symbol, qty, side)
            # if success:
            #     state["entry_count"] = entry_idx + 1
            #     state["sl_rescue_count"] = state.get("sl_rescue_count", 0) + 1
            #     state["entry_time"] = time.time()
            #     logger.info(f"[SL-Rescue] 추가진입 완료: {symbol}, qty={qty}, side={side}")
            # else:
            #     logger.error(f"[SL-Rescue] 주문 실패: {symbol}")

# --- SL-Rescue 모니터링 스레드 ---

def sl_rescue_monitor():
    while True:
        with position_lock:
            symbols = list(position_state.keys())
        for sym in symbols:
            try:
                run_sl_rescue(sym)
            except Exception as ex:
                logger.error(f"SL-Rescue 실행 예외({sym}): {ex}")
        time.sleep(0.2)

# --- 진입 신호 수신 및 상태 초기화 ---

def on_entry_signal_received(symbol_raw: str, side_raw: str):
    symbol = normalize_symbol(symbol_raw)
    side = "buy" if side_raw.lower() == "long" else "sell"
    with position_lock:
        logger.info(f"[EntrySignal] 신호 수신: {symbol} {side} @ {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")
        position_state[symbol] = {
            "entry_count": 1,
            "sl_rescue_count": 0,
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

# --- 중복신호 쿨다운 검사 ---

def is_duplicate_signal(data):
    with signal_lock:
        now = time.time()
        signal_id = data.get("id", "")
        symbol = data.get("symbol", "")
        side = data.get("side", "")
        key = f"{symbol}_{side}"
        if signal_id and signal_id in recent_signals and now - recent_signals[signal_id] < COOLDOWN_SECONDS:
            logger.info(f"중복신호 무시: {signal_id}")
            return True
        if key in recent_signals and now - recent_signals[key] < COOLDOWN_SECONDS:
            logger.info(f"중복신호 무시: {key}")
            return True
        if signal_id:
            recent_signals[signal_id] = now
        recent_signals[key] = now
        return False

# --- Flask 앱 및 라우팅 ---

app = Flask(__name__)

@app.route("/", methods=["GET"])
def root_status():
    with position_lock:
        uptime_sec = int(time.time() - min([state["entry_time"] for state in position_state.values()] or [time.time()]))
        keys = list(position_state.keys())
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
        action = data.get("action", "").lower()
        symbol_raw = data.get("symbol", "")
        side_raw = data.get("side", "")
        if action == "entry" and symbol_raw and side_raw:
            if is_duplicate_signal(data):
                return jsonify({"result": "duplicate signal ignored"}), 200
            on_entry_signal_received(symbol_raw, side_raw)
            return jsonify({"result": "entry signal processed"}), 200
        return jsonify({"error": "unsupported action or missing params"}), 400
    except Exception as e:
        logger.error(f"[Webhook] 예외: {e}", exc_info=True)
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

# --- 메인 프로그램 ---

if __name__ == "__main__":
    logger.info("===========================================")
    logger.info("🚀 서버 시작: Gate.io 자동매매 서버 v6.12 (디버깅 포함)")
    logger.info(f"📊 감시 심볼 수: {len(SYMBOL_CONFIG)}개")
    logger.info(f"⏳ 쿨다운 설정: {COOLDOWN_SECONDS}초, SL-Rescue 최대진입: {SL_RESCUE_MAX}회")
    logger.info(f"🔧 SL-Rescue 임계값: {float(SL_RESCUE_THRESHOLD)*100:.4f}%")
    logger.info("===========================================")

    # SL-Rescue 감시 스레드 시작
    threading.Thread(target=sl_rescue_monitor, daemon=True).start()
    logger.info("🛠️ SL-Rescue 감시 스레드 시작됨")

    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🌐 Flask 웹 서버 시작, 포트: {port}")
    app.run(host="0.0.0.0", port=port)
