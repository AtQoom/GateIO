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
class InfoFilter(logging.Filter):
    def filter(self, record):
        return record.levelno in (logging.INFO, logging.WARNING, logging.ERROR)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
console_handler.addFilter(InfoFilter())
logger.addHandler(console_handler)

file_handler = logging.FileHandler('trading.log')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# ---------------------------- API 클라이언트 패치 ----------------------------
class PatchedApiClient(ApiClient):
    def __call_api(self, *args, **kwargs):
        kwargs['headers']['Timestamp'] = str(int(time.time()))
        logger.debug(f"API 요청: {args[1]} {kwargs.get('query_params')}")
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
SYMBOL_MAP = {
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
    # ... 다른 코인 설정 ...
}

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "equity": Decimal("0")}
actual_entry_prices = {}

# ---------------------------- 코어 함수 ----------------------------
def get_account(force=False):
    try:
        now = time.time()
        if not force and (now - account_cache["time"] < 5):
            return account_cache["equity"]
        acc = api.list_futures_accounts(SETTLE)
        equity = Decimal(str(acc.total)).quantize(Decimal('1e-8'))
        account_cache.update({"time": now, "equity": equity})
        logger.info(f"[계정] 총 자산: {equity} USDT")
        return equity
    except Exception as e:
        logger.error(f"[계정] 조회 실패: {str(e)}", exc_info=True)
        return Decimal("0")

def fetch_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        price = Decimal(str(ticker[0].last)).quantize(Decimal('1e-8'))
        logger.debug(f"[가격] {symbol} 현재가: {price}")
        return price
    except Exception as e:
        logger.error(f"[가격] 조회 실패 [{symbol}]: {str(e)}", exc_info=True)
        return Decimal("0")

def calculate_position_size(symbol, equity, leverage=2):
    cfg = SYMBOL_CONFIG[symbol]
    price = fetch_price(symbol)
    if price <= 0 or equity <= 0:
        logger.warning(f"[계산] 무효 값 [{symbol}] 가격:{price} 잔고:{equity}")
        return Decimal("0")
    try:
        position_value = equity * leverage
        raw_qty = position_value / (price * cfg["contract_size"])
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        logger.info(f"[계산] 최종 수량 [{symbol}]: {final_qty}")
        return final_qty
    except Exception as e:
        logger.error(f"[계산] 오류 [{symbol}]: {str(e)}", exc_info=True)
        return Decimal("0")

def sync_position(symbol):
    with position_lock:
        try:
            pos = api.get_position(SETTLE, symbol)
            if pos.size == 0:
                # 기존 포지션이 있었을 때만 청산 메시지 출력
                if symbol in position_state:
                    logger.info(f"[포지션] 청산됨 [{symbol}]")
                    position_state.pop(symbol, None)
                    actual_entry_prices.pop(symbol, None)
                else:
                    logger.debug(f"[포지션] 없음 [{symbol}]")
                return True
            
            entry_price = actual_entry_prices.get(symbol, Decimal(str(pos.entry_price)))
            position_state[symbol] = {
                "size": abs(pos.size),
                "side": "long" if pos.size > 0 else "short",
                "entry": entry_price
            }
            logger.info(f"[포지션] 갱신 [{symbol}]: {position_state[symbol]}")
            return True
        except Exception as e:
            logger.error(f"[포지션] 동기화 실패 [{symbol}]: {str(e)}", exc_info=True)
            return False

def execute_order(symbol, side, qty, retry=3):
    for attempt in range(1, retry+1):
        try:
            cfg = SYMBOL_CONFIG[symbol]
            qty_dec = qty.quantize(cfg["qty_step"], rounding=ROUND_DOWN)
            size = float(qty_dec) if side == "buy" else -float(qty_dec)
            logger.info(f"[주문] 시도 [{symbol} {side} {qty_dec}] ({attempt}/{retry})")
            order = FuturesOrder(
                contract=symbol,
                size=size,
                price="0",
                tif="ioc"
            )
            response = api.create_futures_order(SETTLE, order)
            logger.info(f"[주문] 성공 | ID:{response.id} | {symbol} {side} {qty_dec}")
            sync_position(symbol)
            return True
        except Exception as e:
            error = str(e)
            logger.error(f"[주문] 실패 [{symbol} {side} {qty_dec}]: {error}")
            if "INSUFFICIENT_AVAILABLE" in error:
                logger.warning("[주문] 잔고 부족으로 재시도 중단")
                break
            time.sleep(0.5)
    logger.critical(f"[주문] 최종 실패 [{symbol} {side} {qty_dec}]")
    return False

# ---------------------------- 웹훅 처리 ----------------------------
@app.route("/", methods=["POST"])
def handle_webhook():
    start_time = time.time()
    try:
        data = request.get_json()
        logger.debug(f"[웹훅] 수신: {json.dumps(data, indent=2)}")
        raw_symbol = data.get("symbol", "").upper().replace(".P", "")
        symbol = SYMBOL_MAP.get(raw_symbol)
        if not symbol:
            logger.error(f"[웹훅] 잘못된 심볼: {raw_symbol}")
            return jsonify({"error": "Invalid symbol"}), 400
        if data.get("action") == "exit" and data.get("reason") == "reverse_signal":
            logger.warning(f"[청산] 반대 신호 감지 [{symbol}]")
            success = execute_order(symbol, "close", Decimal("0"))
            return jsonify({"status": "success" if success else "error"})
        action = data.get("action", "").lower()
        if action not in ["entry", "exit"]:
            logger.error(f"[웹훅] 잘못된 액션: {action}")
            return jsonify({"error": "Invalid action"}), 400
        if action == "exit":
            logger.info(f"[청산] 요청 [{symbol}]")
            success = execute_order(symbol, "close", Decimal("0"))
            return jsonify({"status": "success" if success else "error"})
        leverage = int(data.get("leverage", 2))
        equity = get_account(force=True)
        qty = calculate_position_size(symbol, equity, leverage)
        if qty <= 0:
            logger.error(f"[진입] 무효 수량 [{symbol}]: {qty}")
            return jsonify({"error": "Invalid quantity"}), 400
        desired_side = "buy" if data.get("side") == "long" else "sell"
        logger.info(f"[진입] 시도 [{symbol} {desired_side} {qty}]")
        success = execute_order(symbol, desired_side, qty)
        return jsonify({"status": "success" if success else "error", "qty": float(qty)})
    except Exception as e:
        logger.error(f"[웹훅] 처리 실패: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        logger.info(f"[성능] 처리 시간: {time.time() - start_time:.3f}초")

# ---------------------------- 실시간 모니터링 ----------------------------
async def price_monitor():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    while True:
        try:
            logger.info("[웹소켓] 연결 시도")
            async with websockets.connect(uri, ping_interval=30) as ws:
                logger.info("[웹소켓] 연결 성공")
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": list(SYMBOL_CONFIG.keys())
                }))
                logger.info("[웹소켓] 티커 구독 완료")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "result" in data:
                        process_ticker(data["result"])
        except Exception as e:
            logger.error(f"[웹소켓] 오류: {str(e)}")
            await asyncio.sleep(5)

def process_ticker(ticker_data):
    try:
        # 리스트면 여러 심볼, 딕셔너리면 단일 심볼
        if isinstance(ticker_data, list):
            for item in ticker_data:
                process_ticker(item)
            return
        
        # 필수 필드 체크
        if not isinstance(ticker_data, dict):
            return
        if "contract" not in ticker_data or "last" not in ticker_data:
            logger.debug(f"[티커] 무효 데이터: {ticker_data}")
            return
        
        symbol = ticker_data["contract"]
        price = Decimal(str(ticker_data["last"])).quantize(Decimal('1e-8'))
        logger.debug(f"[티커] 업데이트 [{symbol}]: {price}")
        
        with position_lock:
            pos = position_state.get(symbol)
            if not pos or pos["size"] == 0:
                return
            entry = actual_entry_prices.get(symbol, pos["entry"])
            cfg = SYMBOL_CONFIG[symbol]
            if pos["side"] == "long":
                sl = entry * (1 - cfg["sl_pct"])
                tp = entry * (1 + cfg["tp_pct"])
                if price <= sl:
                    logger.warning(f"[TP/SL] 롱 SL 트리거 [{symbol}] 가격:{price} 진입가:{entry}")
                    execute_order(symbol, "close", Decimal(pos["size"]))
                elif price >= tp:
                    logger.warning(f"[TP/SL] 롱 TP 트리거 [{symbol}] 가격:{price} 진입가:{entry}")
                    execute_order(symbol, "close", Decimal(pos["size"]))
            else:
                sl = entry * (1 + cfg["sl_pct"])
                tp = entry * (1 - cfg["tp_pct"])
                if price >= sl:
                    logger.warning(f"[TP/SL] 숏 SL 트리거 [{symbol}] 가격:{price} 진입가:{entry}")
                    execute_order(symbol, "close", Decimal(pos["size"]))
                elif price <= tp:
                    logger.warning(f"[TP/SL] 숏 TP 트리거 [{symbol}] 가격:{price} 진입가:{entry}")
                    execute_order(symbol, "close", Decimal(pos["size"]))
    except Exception as e:
        logger.error(f"[티커] 처리 실패: {str(e)}", exc_info=True)

# ---------------------------- 백그라운드 작업 ----------------------------
def background_sync():
    while True:
        try:
            logger.debug("[백그라운드] 동기화 시작")
            for symbol in SYMBOL_CONFIG:
                sync_position(symbol)
            time.sleep(60)
        except Exception as e:
            logger.error(f"[백그라운드] 오류: {str(e)}")
            time.sleep(30)

# ---------------------------- 서버 실행 ----------------------------
if __name__ == "__main__":
    threading.Thread(target=background_sync, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"[서버] 시작 (포트: {port})")
    app.run(host="0.0.0.0", port=port, use_reloader=False)
