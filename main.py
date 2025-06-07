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
server_start_time = time.time()

# ---------------------------- 업타임/헬스 엔드포인트 ----------------------------
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/health", methods=["GET"])
def health_check():
    try:
        api.list_futures_accounts(SETTLE)
        api_status = "healthy"
    except Exception as e:
        api_status = f"error: {str(e)}"
    uptime = int(time.time() - server_start_time)
    return jsonify({
        "status": "healthy" if api_status == "healthy" else "degraded",
        "timestamp": datetime.now().isoformat(),
        "uptime_seconds": uptime,
        "api_connection": api_status,
        "active_positions": len([k for k, v in position_state.items() if v.get("size", 0) > 0]),
        "total_symbols": len(SYMBOL_CONFIG),
        "version": "1.0.0"
    }), 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral()
        active_positions = {}
        for symbol, pos in position_state.items():
            if pos.get("size", 0) > 0:
                active_positions[symbol] = {
                    "size": float(pos["size"]),
                    "side": pos["side"],
                    "entry_price": float(pos["entry"]),
                    "count": position_counts.get(symbol, 0)
                }
        return jsonify({
            "total_collateral": float(equity),
            "active_positions": active_positions,
            "position_counts": dict(position_counts),
            "last_signals": {k: datetime.fromtimestamp(v).isoformat() for k, v in last_signals.items()},
            "server_uptime": int(time.time() - server_start_time)
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ✅ /debug 엔드포인트 추가 위치
@app.route("/debug", methods=["GET"])
def debug_account():
    try:
        acc = api.list_futures_accounts(SETTLE)
        return jsonify({
            "type": str(type(acc)),
            "total": str(acc.total),
            "available": str(getattr(acc, 'available', 'N/A')),
            "position_margin": str(getattr(acc, 'position_margin', 'N/A')),
            "order_margin": str(getattr(acc, 'order_margin', 'N/A')),
            "raw_data": str(acc)
        })
    except Exception as e:
        return jsonify({"error": str(e)})
        
# ---------------------------- 총 담보금 조회 ----------------------------
def get_total_collateral(force=False):
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)  # Gate.io 공식 문서: 단일 객체 반환
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
    reconnect_count = 0
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=10) as ws:
                reconnect_count = 0
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": list(SYMBOL_CONFIG.keys())
                }))
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        if "result" in data:
                            process_ticker(data["result"])
                    except asyncio.TimeoutError:
                        log_debug("⚠️ 웹소켓", "수신 타임아웃, 재연결")
                        break
        except Exception as e:
            reconnect_count += 1
            log_debug("❌ 웹소켓 오류", f"{str(e)} (재시도: {reconnect_count})")
            await asyncio.sleep(min(reconnect_count * 2, 30))

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
            if (pos["side"] == "long" and (price <= entry*(1-cfg["sl_pct"]) or price >= entry*(1+cfg["tp_pct"]))) or \
               (pos["side"] == "short" and (price >= entry*(1+cfg["sl_pct"]) or price <= entry*(1-cfg["tp_pct"]))):
                log_debug(f"⚡ SL/TP 트리거 ({contract})", f"현재가: {price}, 진입가: {entry}")
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
        final_qty = max(qty, cfg["min_qty"])
        log_debug(f"📊 수량 계산 ({symbol})", f"담보금: {equity}, 가격: {price}, 수량: {final_qty}")
        return final_qty
    except Exception as e:
        log_debug(f"❌ 수량 계산 오류 ({symbol})", str(e), exc_info=True)
        return Decimal("0")

# ---------------------------- 주문 실행 (피라미딩 2회 제한, 주문 취소 API 수정) ----------------------------
def execute_order(symbol, side, qty):
    for attempt in range(3):
        try:
            cfg = SYMBOL_CONFIG[symbol]
            qty_dec = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN)
            if qty_dec < cfg["min_qty"]:
                log_debug(f"⛔ 최소 수량 미달 ({symbol})", f"{qty_dec} < {cfg['min_qty']}")
                return False
            current_count = position_counts.get(symbol, 0)
            if current_count >= 2:
                log_debug(f"🚫 피라미딩 제한 ({symbol})", "최대 2회")
                return False
            size = float(qty_dec) if side == "buy" else -float(qty_dec)
            # ✅ 올바른 주문 취소 메서드 사용
            try:
                api.cancel_futures_orders(SETTLE, symbol)
            except Exception as e:
                log_debug("주문 취소 무시", str(e))
            order = FuturesOrder(
                contract=symbol,
                size=size,
                price="0",
                tif="ioc"
            )
            created_order = api.create_futures_order(SETTLE, order)
            log_debug(f"✅ 주문 완료 ({symbol})", f"{side} {qty_dec} 계약")
            sync_position(symbol)
            return True
        except Exception as e:
            log_debug(f"❌ 주문 실패 ({symbol})", str(e))
            time.sleep(1)
    return False

# ---------------------------- 청산 로직 (주문 취소 API 수정) ----------------------------
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
                # ✅ 올바른 주문 취소 메서드 사용
                try:
                    api.cancel_futures_orders(SETTLE, symbol)
                except Exception as e:
                    log_debug("주문 취소 무시", str(e))
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
        position_counts[symbol] = 0
        return False
    finally:
        position_lock.release()

# ---------------------------- 웹훅 처리 ----------------------------
@app.route("/", methods=["POST"])
def webhook():
    symbol = None
    try:
        data = request.get_json()
        log_debug("📥 웹훅", f"수신: {json.dumps(data)}")
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400
        action = data.get("action", "").lower()
        reason = data.get("reason", "")
        side = data.get("side", "").lower()
        signal_key = f"{symbol}_{action}_{side}"
        now = time.time()
        if signal_key in last_signals and now - last_signals[signal_key] < 3:
            log_debug("🚫 중복 신호 차단", signal_key)
            return jsonify({"status": "duplicate_blocked"}), 200
        last_signals[signal_key] = now
        with position_lock:
            if action == "exit":
                success = close_position(symbol)
                return jsonify({"status": "success" if success else "error"})
            if action == "entry":
                if position_counts.get(symbol, 0) >= 2:
                    log_debug(f"🚫 피라미딩 제한 ({symbol})", "최대 2회")
                    return jsonify({"status": "pyramiding_limit"}), 200
                current_side = position_state.get(symbol, {}).get("side")
                desired_side = "buy" if side == "long" else "sell"
                if current_side and current_side != desired_side:
                    if not close_position(symbol):
                        return jsonify({"status": "error", "message": "역포지션 청산 실패"}), 500
                    time.sleep(3)
                qty = calculate_position_size(symbol)
                if qty <= 0:
                    return jsonify({"status": "error", "message": "수량 계산 오류"}), 400
                success = execute_order(symbol, desired_side, qty)
                if success:
                    position_counts[symbol] = position_counts.get(symbol, 0) + 1
                return jsonify({
                    "status": "success" if success else "error", 
                    "qty": float(qty),
                    "symbol": symbol,
                    "side": side
                })
        return jsonify({"error": "Invalid action"}), 400
    except Exception as e:
        log_debug(f"❌ 웹훅 처리 실패 ({symbol or 'unknown'})", str(e), exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ---------------------------- 서버 실행 ----------------------------
if __name__ == "__main__":
    threading.Thread(target=backup_position_loop, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 서버 시작 (포트: {port})")
    logger.info(f"📍 헬스체크: http://localhost:{port}/ping")
    logger.info(f"📊 상태조회: http://localhost:{port}/status")
    app.run(host="0.0.0.0", port=port, debug=False)
