import os
import time
import threading
import logging
from decimal import Decimal, ROUND_DOWN
from flask import Flask
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi

# ----------- 로그 설정 개선 -----------
logging.basicConfig(
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ----------- 서버 설정 -----------
app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"),
        "sl_pct": Decimal("0.0035"), "tp_pct": Decimal("0.006"), "min_notional": Decimal("10")
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.001"),
        "sl_pct": Decimal("0.0035"), "tp_pct": Decimal("0.006"), "min_notional": Decimal("10")
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"),
        "sl_pct": Decimal("0.0035"), "tp_pct": Decimal("0.006"), "min_notional": Decimal("10")
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"), "tp_pct": Decimal("0.006"), "min_notional": Decimal("10")
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"), "tp_pct": Decimal("0.006"), "min_notional": Decimal("10")
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"),
        "sl_pct": Decimal("0.0035"), "tp_pct": Decimal("0.006"), "min_notional": Decimal("10")
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000"),
        "sl_pct": Decimal("0.0035"), "tp_pct": Decimal("0.006"), "min_notional": Decimal("10")
    }
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

position_state = {}
position_lock = threading.RLock()

def log_initial_status():
    logger.info("=== Gate.io 자동매매 서버 시작 ===")
    try:
        acc = api.list_futures_accounts(SETTLE)
        logger.info(f"잔고: {getattr(acc, 'available', 'N/A')} USDT")
        for symbol in SYMBOL_CONFIG.keys():
            try:
                pos = api.get_position(SETTLE, symbol)
                size = Decimal(str(pos.size))
                if size != 0:
                    side = 'Long' if size > 0 else 'Short'
                    logger.info(f"{symbol}: {side} {abs(size)} 계약 @ {pos.entry_price}")
                else:
                    logger.info(f"{symbol}: 포지션 없음")
            except Exception:
                logger.info(f"{symbol}: 포지션 없음")
    except Exception as e:
        logger.error(f"초기 상태 로깅 실패: {e}")

def get_total_collateral():
    try:
        acc = api.list_futures_accounts(SETTLE)
        available = Decimal(str(getattr(acc, 'available', '0')))
        return available
    except Exception as e:
        logger.error(f"잔고 조회 실패: {e}")
        return Decimal("0")

def get_price(symbol):
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if not ticker or len(ticker) == 0:
            return Decimal("0")
        price_str = str(ticker[0].last).upper().replace("E", "e")
        price = Decimal(price_str).normalize()
        return price
    except Exception as e:
        logger.error(f"가격 조회 실패 {symbol}: {e}")
        return Decimal("0")

def calculate_position_size(symbol):
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    if price <= 0 or equity <= 0:
        return Decimal("0")
    try:
        raw_qty = equity / (price * cfg["contract_size"])
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        final_qty = max(qty, cfg["min_qty"])
        order_value = final_qty * price * cfg["contract_size"]
        if order_value < cfg["min_notional"]:
            logger.warning(f"{symbol}: 최소 주문 금액 미달 ({order_value} < {cfg['min_notional']})")
            return Decimal("0")
        return final_qty
    except Exception as e:
        logger.error(f"수량 계산 오류 {symbol}: {e}")
        return Decimal("0")

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        logger.warning(f"주문 락 실패 {symbol}")
        return False
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        min_qty = cfg["min_qty"]
        qty_dec = Decimal(str(qty)).quantize(step, ROUND_DOWN)
        if qty_dec < min_qty:
            logger.warning(f"잘못된 수량 {symbol}: {qty_dec} < {min_qty}")
            return False
        price = get_price(symbol)
        order_value = qty_dec * price * cfg["contract_size"]
        if order_value < cfg["min_notional"]:
            logger.warning(f"최소 주문 금액 미달 {symbol}: {order_value}")
            return False
        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        api.create_futures_order(SETTLE, order)
        logger.info(f"✅ 주문 성공 {symbol} {side.upper()} {float(qty_dec)} 계약")
        time.sleep(2)
        update_position_state(symbol)
        return True
    except Exception as e:
        logger.error(f"주문 실패 {symbol}: {e}")
        if retry > 0:
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // step) * step
            retry_qty = max(retry_qty, min_qty)
            logger.info(f"재시도 {symbol}: {qty} → {retry_qty}")
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False
    finally:
        position_lock.release()

def update_position_state(symbol, timeout=5):
    acquired = position_lock.acquire(timeout=timeout)
    if not acquired:
        return False
    try:
        try:
            pos = api.get_position(SETTLE, symbol)
        except Exception:
            position_state[symbol] = {
                "price": None, "side": None, "size": Decimal("0"), 
                "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
            }
            return True
        size = Decimal(str(pos.size))
        if size != 0:
            entry_price = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            position_state[symbol] = {
                "price": entry_price, "side": "buy" if size > 0 else "sell",
                "size": abs(size), "value": value, "margin": value, "mode": "cross"
            }
        else:
            position_state[symbol] = {
                "price": None, "side": None, "size": Decimal("0"), 
                "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
            }
        return True
    except Exception as e:
        logger.error(f"포지션 조회 실패 {symbol}: {e}")
        return False
    finally:
        position_lock.release()

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return Decimal(50)
    deltas = [float(closes[i] - closes[i-1]) for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
    if avg_loss == 0:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100 - (100 / (1 + rs)))

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return Decimal("1.0")
    tr_values = []
    for i in range(1, len(closes)):
        tr = max(
            float(highs[i] - lows[i]),
            abs(float(highs[i] - closes[i-1])),
            abs(float(lows[i] - closes[i-1]))
        )
        tr_values.append(tr)
    return Decimal(sum(tr_values[-period:]) / period) if len(tr_values) >= period else Decimal("1.0")

def check_engulfing(current, prev):
    bullish = current['close'] > prev['open'] and prev['close'] < prev['open']
    bearish = current['close'] < prev['open'] and prev['close'] > prev['open']
    return bullish or bearish

def generate_signal(symbol):
    try:
        # 3분봉 데이터
        candles_3m = api.list_futures_candlesticks(SETTLE, symbol, interval="3m", limit=5)
        if len(candles_3m) < 5: 
            return False, False
        tf_3m = [{
            'open': Decimal(str(c.o)), 'high': Decimal(str(c.h)),
            'low': Decimal(str(c.l)), 'close': Decimal(str(c.c)),
        } for c in candles_3m]
        closes_3m = [c['close'] for c in tf_3m]
        highs_3m = [c['high'] for c in tf_3m]
        lows_3m = [c['low'] for c in tf_3m]
        rsi_3m = calculate_rsi(closes_3m, 14)
        atr_3m = calculate_atr(highs_3m, lows_3m, closes_3m, 14)
        engulf_3m = check_engulfing(tf_3m[-1], tf_3m[-2])

        # 10초봉 데이터 (지원됨)
        candles_10s = api.list_futures_candlesticks(SETTLE, symbol, interval="10s", limit=5)
        if len(candles_10s) < 5: 
            return False, False
        tf_10s = [{
            'open': Decimal(str(c.o)), 'high': Decimal(str(c.h)),
            'low': Decimal(str(c.l)), 'close': Decimal(str(c.c)),
        } for c in candles_10s]
        closes_10s = [c['close'] for c in tf_10s]
        rsi_10s = calculate_rsi(closes_10s, 14)
        engulf_10s = check_engulfing(tf_10s[-1], tf_10s[-2])

        # 전략 신호 (3분 + 10초)
        long_signal = (
            rsi_3m <= 44 and engulf_3m and
            abs(tf_3m[-1]['close'] - tf_3m[-1]['open']) > atr_3m * Decimal('1.05') and
            tf_10s[-1]['close'] > tf_10s[-2]['open'] and rsi_10s <= 40
        )
        short_signal = (
            rsi_3m >= 56 and engulf_3m and
            abs(tf_3m[-1]['close'] - tf_3m[-1]['open']) > atr_3m * Decimal('1.05') and
            tf_10s[-1]['close'] < tf_10s[-2]['open'] and rsi_10s >= 60
        )
        
        if long_signal or short_signal:
            logger.info(f"📊 신호 {symbol}: Long={long_signal}, Short={short_signal} (RSI_3m={rsi_3m:.1f}, RSI_10s={rsi_10s:.1f})")
        
        return long_signal, short_signal
    except Exception as e:
        logger.error(f"신호 생성 실패 {symbol}: {e}")
        return False, False

def main_trading_loop():
    logger.info("🤖 자동매매 루프 시작")
    while True:
        try:
            for symbol in SYMBOL_CONFIG.keys():
                long, short = generate_signal(symbol)
                update_position_state(symbol)
                pos = position_state.get(symbol, {})
                current_side = pos.get("side")
                
                if long and current_side != "buy":
                    if current_side == "sell":
                        logger.info(f"🔄 {symbol}: 숏 청산 후 롱 진입")
                        place_order(symbol, "buy", pos.get("size", 0), reduce_only=True)
                    qty = calculate_position_size(symbol)
                    if qty > 0:
                        place_order(symbol, "buy", qty)
                elif short and current_side != "sell":
                    if current_side == "buy":
                        logger.info(f"🔄 {symbol}: 롱 청산 후 숏 진입")
                        place_order(symbol, "sell", pos.get("size", 0), reduce_only=True)
                    qty = calculate_position_size(symbol)
                    if qty > 0:
                        place_order(symbol, "sell", qty)
            
            time.sleep(10)  # 10초 간격으로 신호 체크
        except Exception as e:
            logger.error(f"트레이딩 루프 오류: {e}")
            time.sleep(60)

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = get_total_collateral()
        positions = {}
        for symbol in SYMBOL_CONFIG.keys():
            update_position_state(symbol)
            pos = position_state.get(symbol, {})
            if pos.get("side"):
                positions[symbol] = {
                    "side": pos["side"],
                    "size": float(pos["size"]),
                    "price": float(pos["price"]) if pos["price"] else None,
                    "value": float(pos["value"])
                }
        return {
            "status": "running",
            "equity": float(equity),
            "positions": positions
        }
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/", methods=["POST"])
def webhook():
    return {"status": "ok"}

if __name__ == "__main__":
    log_initial_status()
    trading_thread = threading.Thread(target=main_trading_loop, daemon=True)
    trading_thread.start()
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 서버 시작: 포트 {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
