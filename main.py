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
class LogFilter(logging.Filter):
    def filter(self, record):
        blocked_phrases = [
            "티커 수신", "가격 (", "Position update failed",
            "계약:", "마크가:", "사이즈:", "진입가:", "총 담보금:"
        ]
        return not any(phrase in record.msg for phrase in blocked_phrases)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s'))
handler.addFilter(LogFilter())
logger.addHandler(handler)

# ---------------------------- 게이트 API 패치 ----------------------------
class CustomApiClient(ApiClient):
    def __call_api(self, *args, **kwargs):
        # Timestamp 헤더 수동 추가
        kwargs['headers']['Timestamp'] = str(int(time.time()))
        return super().__call_api(*args, **kwargs)

# ---------------------------- 서버 설정 ----------------------------
app = Flask(__name__)
API_KEY = os.environ.get("GATE_API_KEY")
API_SECRET = os.environ.get("GATE_API_SECRET")
SETTLE = "usdt"

if not API_KEY or not API_SECRET:
    raise ValueError("GATE_API_KEY와 GATE_API_SECRET 환경변수를 설정해야 합니다.")

config = Configuration(key=API_KEY, secret=API_SECRET)
client = CustomApiClient(config)  # 패치된 클라이언트 사용
api = FuturesApi(client)

# ---------------------------- 거래 심볼 설정 ----------------------------
SYMBOL_MAP = {
    "BTCUSDT": "BTC_USDT", "ETHUSDT": "ETH_USDT", 
    "ADAUSDT": "ADA_USDT", "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT"
}

SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": 1, "qty_step": 1, "contract_size": Decimal("0.0001"), "leverage": 2},
    "ETH_USDT": {"min_qty": 1, "qty_step": 1, "contract_size": Decimal("0.001"), "leverage": 2},
    "ADA_USDT": {"min_qty": 1, "qty_step": 1, "contract_size": Decimal("10"), "leverage": 2},
    "SUI_USDT": {"min_qty": 1, "qty_step": 1, "contract_size": Decimal("1"), "leverage": 2},
    "LINK_USDT": {"min_qty": 1, "qty_step": 1, "contract_size": Decimal("1"), "leverage": 2},
    "SOL_USDT": {"min_qty": 1, "qty_step": 1, "contract_size": Decimal("0.1"), "leverage": 2},
    "PEPE_USDT": {"min_qty": 1, "qty_step": 1, "contract_size": Decimal("10000"), "leverage": 2}
}
for k in SYMBOL_CONFIG:  # Decimal 변환
    SYMBOL_CONFIG[k] = {ik: Decimal(iv) if isinstance(iv, str) else iv 
                       for ik, iv in SYMBOL_CONFIG[k].items()}

# ---------------------------- 글로벌 상태 ----------------------------
position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
actual_entry_prices = {}

# ---------------------------- 코어 함수 ----------------------------
def get_account(force=False):
    now = time.time()
    if not force and (now - account_cache["time"] < 5) and account_cache["data"]:
        return account_cache["data"]
    
    try:
        acc = api.list_futures_accounts(SETTLE)
        equity = Decimal(str(acc.total)).normalize()
        account_cache.update({"time": now, "data": equity})
        return equity
    except Exception as e:
        logger.error(f"계정 조회 실패: {str(e)}")
        return Decimal("0")

def fetch_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        return Decimal(str(ticker[0].last)).normalize() if ticker else Decimal("0")
    except Exception as e:
        logger.error(f"가격 조회 실패 [{symbol}]: {str(e)}")
        return Decimal("0")

def calculate_position_size(symbol, equity):
    cfg = SYMBOL_CONFIG[symbol]
    price = fetch_price(symbol)
    if price <= 0 or equity <= 0:
        return cfg["min_qty"]
    
    position_value = equity * 2  # 레버리지 2배
    raw_qty = (position_value / (price * cfg["contract_size"])).quantize(Decimal('1e-8'), ROUND_DOWN)
    qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
    return max(qty, cfg["min_qty"])

def sync_position(symbol):
    with position_lock:
        try:
            pos = api.get_position(SETTLE, symbol)
            if pos.size == 0:
                position_state[symbol] = None
                actual_entry_prices.pop(symbol, None)
                return True
            
            entry_price = Decimal(str(pos.entry_price))
            position_state[symbol] = {
                "size": abs(pos.size),
                "side": "long" if pos.size > 0 else "short",
                "price": actual_entry_prices.get(symbol, entry_price)
            }
            return True
        except Exception as e:
            logger.error(f"포지션 동기화 실패 [{symbol}]: {str(e)}")
            return False

def execute_order(symbol, side, qty, retry=3):
    for attempt in range(retry):
        try:
            cfg = SYMBOL_CONFIG[symbol]
            size = float(Decimal(qty).quantize(cfg["qty_step"]))
            order = FuturesOrder(
                contract=symbol,
                size=size if side == "buy" else -size,
                price="0",
                tif="ioc"
            )
            api.create_futures_order(SETTLE, order)
            
            # 체결가 추적
            trades = api.list_my_trades(SETTLE, contract=symbol, limit=1)
            if trades:
                actual_price = Decimal(str(trades[0].price))
                actual_entry_prices[symbol] = actual_price
                logger.info(f"체결가 업데이트 [{symbol}]: {actual_price}")
            
            sync_position(symbol)
            return True
        except Exception as e:
            logger.error(f"주문 실패 [{symbol} {side} {qty}] ({attempt+1}/{retry}): {str(e)}")
            time.sleep(0.5)
    return False

# ---------------------------- 웹훅 처리 ----------------------------
@app.route("/", methods=["POST"])
def handle_webhook():
    try:
        data = request.get_json()
        raw_symbol = data.get("symbol", "").upper()
        symbol = SYMBOL_MAP.get(raw_symbol)
        
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400
        
        # 반대 신호 청산
        if data.get("action") == "exit" and data.get("reason") == "reverse_signal":
            success = execute_order(symbol, "close", 0)
            return jsonify({"status": "success" if success else "error"})
        
        # 진입/청산 처리
        action = data.get("action", "").lower()
        if action == "exit":
            success = execute_order(symbol, "close", 0)
            return jsonify({"status": "success" if success else "error"})
        
        equity = get_account(force=True)
        qty = calculate_position_size(symbol, equity)
        if qty <= 0:
            return jsonify({"error": "Invalid quantity"}), 400
        
        side = "buy" if data.get("side") == "long" else "sell"
        success = execute_order(symbol, side, qty)
        return jsonify({"status": "success" if success else "error", "qty": float(qty)})
    
    except Exception as e:
        logger.error(f"웹훅 처리 오류: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# ---------------------------- 실시간 모니터링 ----------------------------
async def price_monitor():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "result" not in data:
                        continue
                    
                    for ticker in data["result"] if isinstance(data["result"], list) else [data["result"]]:
                        process_ticker(ticker)
        
        except Exception as e:
            logger.error(f"웹소켓 연결 오류: {str(e)}")
            await asyncio.sleep(5)

def process_ticker(ticker):
    symbol = ticker.get("contract")
    price = Decimal(str(ticker["last"])).normalize()
    
    with position_lock:
        if symbol not in position_state or not position_state[symbol]:
            return
        
        pos = position_state[symbol]
        entry = pos["price"]
        cfg = SYMBOL_CONFIG[symbol]
        
        # TP/SL 계산
        if pos["side"] == "long":
            sl = entry * (1 - cfg["sl_pct"])
            tp = entry * (1 + cfg["tp_pct"])
            if price <= sl or price >= tp:
                execute_order(symbol, "close", pos["size"])
        else:
            sl = entry * (1 + cfg["sl_pct"])
            tp = entry * (1 - cfg["tp_pct"])
            if price >= sl or price <= tp:
                execute_order(symbol, "close", pos["size"])

# ---------------------------- 백그라운드 작업 ----------------------------
def background_updater():
    while True:
        try:
            for symbol in SYMBOL_CONFIG:
                sync_position(symbol)
            time.sleep(60)
        except Exception as e:
            logger.error(f"백그라운드 업데이트 실패: {str(e)}")
            time.sleep(30)

# ---------------------------- 서버 실행 ----------------------------
if __name__ == "__main__":
    threading.Thread(target=background_updater, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), use_reloader=False)
