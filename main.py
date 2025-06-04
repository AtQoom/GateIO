import os
import json
import time
import asyncio
import threading
import websockets
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

# 🔴 PEPEUSDT 추가
BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT",
    "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT"
}

# 🔴 모든 코인 레버리지 2배 통일 + PEPE 추가
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

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}

def log_debug(tag, msg, exc_info=False):
    if "포지션 없음" in msg: return
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {msg}")
    if exc_info:
        import traceback
        print(traceback.format_exc())

def get_account_info(force=False):
    """총 담보금(잔고+미실현손익) 반환 - 디버깅 강화"""
    now = time.time()
    if not force and account_cache["time"] > now - 5 and account_cache["data"]:
        return account_cache["data"]
    try:
        acc = api.list_futures_accounts(SETTLE)
        
        # 🔴 API 응답 전체 로깅
        log_debug("🔍 API 응답", f"전체 계정 정보: {acc}")
        
        # 🔴 각 필드별 상세 로깅
        total_str = str(acc.total) if hasattr(acc, 'total') else "0"
        available_str = str(acc.available) if hasattr(acc, 'available') else "0"
        unrealised_pnl_str = str(acc.unrealised_pnl) if hasattr(acc, 'unrealised_pnl') else "0"
        
        log_debug("💰 계정 필드별", f"total: {total_str}, available: {available_str}, unrealised_pnl: {unrealised_pnl_str}")
        
        # 과학적 표기법 처리
        total_equity = Decimal(total_str.upper().replace("E", "e"))
        available_equity = Decimal(available_str.upper().replace("E", "e"))
        
        # 🔴 total이 비정상적으로 작으면 available 사용
        if total_equity < Decimal("1"):
            log_debug("⚠️ 담보금 부족", f"total:{total_equity} < 1, available:{available_equity} 사용")
            final_equity = available_equity
        else:
            final_equity = total_equity
        
        account_cache.update({"time": now, "data": final_equity})
        log_debug("💰 최종 선택", f"선택된 담보금: {final_equity}")
        return final_equity
        
    except Exception as e:
        log_debug("❌ 계정 조회 실패", str(e), exc_info=True)
        return Decimal("0")

def get_max_qty(symbol, side):
    """전체 잔고 × 2배 계산 (모든 코인 동일)"""
    try:
        cfg = SYMBOL_CONFIG[symbol]
        total_equity = get_account_info(force=True)
        price = get_price(symbol)
        
        if total_equity <= 0 or price <= 0:
            log_debug(f"⚠️ 계산 불가 ({symbol})", f"담보금:{total_equity}, 가격:{price}")
            return float(cfg["min_qty"])

        # 모든 코인 2배 적용
        position_value = total_equity * 2
        contract_size = cfg["contract_size"]
        raw_qty = (position_value / (price * contract_size)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
        
        qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
        qty = max(qty, cfg["min_qty"])

        log_debug(f"📊 수량 계산 ({symbol})", 
            f"담보금:{total_equity}, 가격:{price}, 계약단위:{contract_size}, 최종수량:{qty}")
        return float(qty)
    except Exception as e:
        log_debug(f"❌ 수량 계산 실패 ({symbol})", str(e), exc_info=True)
        return float(cfg["min_qty"])

def update_position_state(symbol, timeout=5):
    """🔴 POSITION_NOT_FOUND 오류 수정"""
    acquired = position_lock.acquire(timeout=timeout)
    if not acquired:
        log_debug(f"⚠️ 락 획득 실패 ({symbol})", f"타임아웃 {timeout}초")
        return False
    try:
        try:
            pos = api.get_position(SETTLE, symbol)
        except Exception as e:
            # 🔴 POSITION_NOT_FOUND는 정상 처리 (포지션 없음)
            if "POSITION_NOT_FOUND" in str(e):
                position_state[symbol] = {
                    "price": None, "side": None,
                    "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
                }
                return True  # 정상 처리
            else:
                log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
                return False
        
        # 포지션이 존재하는 경우 처리
        if hasattr(pos, "margin_mode") and pos.margin_mode != "cross":
            api.update_position_margin_mode(SETTLE, symbol, "cross")
            log_debug(f"⚙️ 마진모드 변경 ({symbol})", f"{pos.margin_mode} → cross")
        
        size = Decimal(str(pos.size))
        if size != 0:
            entry = Decimal(str(pos.entry_price))
            mark = Decimal(str(pos.mark_price))
            value = abs(size) * mark * SYMBOL_CONFIG[symbol]["contract_size"]
            margin = value / SYMBOL_CONFIG[symbol]["leverage"]
            position_state[symbol] = {
                "price": entry, "side": "buy" if size > 0 else "sell",
                "size": abs(size), "value": value, "margin": margin,
                "mode": "cross"
            }
            log_debug(f"📊 포지션 상태 ({symbol})", f"진입가: {entry}, 사이즈: {abs(size)}, 방향: {position_state[symbol]['side']}")
        else:
            position_state[symbol] = {
                "price": None, "side": None,
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0"), "mode": "cross"
            }
        return True
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
        return False
    finally:
        position_lock.release()

def place_order(symbol, side, qty, reduce_only=False, retry=3):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 주문 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        cfg = SYMBOL_CONFIG[symbol]
        step = cfg["qty_step"]
        min_qty = cfg["min_qty"]

        qty_dec = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        if qty_dec < min_qty:
            log_debug(f"⛔ 잘못된 수량 ({symbol})", f"{qty_dec} < 최소 {min_qty}")
            return False

        size = float(qty_dec) if side == "buy" else -float(qty_dec)
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        log_debug(f"📤 주문 시도 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약, reduce_only={reduce_only}")
        api.create_futures_order(SETTLE, order)
        log_debug(f"✅ 주문 성공 ({symbol})", f"{side.upper()} {float(qty_dec)} 계약")
        time.sleep(0.5)
        update_position_state(symbol)
        return True
    except Exception as e:
        error_msg = str(e)
        log_debug(f"❌ 주문 실패 ({symbol})", f"{error_msg}")
        if retry > 0 and ("INVALID_PARAM" in error_msg or "POSITION_EMPTY" in error_msg):
            retry_qty = (Decimal(str(qty)) * Decimal("0.5") // step) * step
            retry_qty = max(retry_qty, min_qty)
            log_debug(f"🔄 재시도 ({symbol})", f"{qty} → {retry_qty}")
            return place_order(symbol, side, float(retry_qty), reduce_only, retry-1)
        return False
    finally:
        position_lock.release()

def close_position(symbol):
    acquired = position_lock.acquire(timeout=5)
    if not acquired:
        log_debug(f"⚠️ 청산 락 실패 ({symbol})", "타임아웃")
        return False
    try:
        log_debug(f"🔄 청산 시도 ({symbol})", "size=0 주문")
        api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
        log_debug(f"✅ 청산 완료 ({symbol})", "")
        time.sleep(0.5)
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
        log_debug("🔄 웹훅 시작", "요청 수신")
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
        data = request.get_json()
        log_debug("📥 웹훅 데이터", json.dumps(data))
        raw = data.get("symbol", "").upper().replace(".P", "")
        symbol = BINANCE_TO_GATE_SYMBOL.get(raw)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": "Invalid symbol"}), 400
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        reason = data.get("reason", "")

        # 🔴 반대 신호 청산 처리
        if action == "exit" and reason == "reverse_signal":
            success = close_position(symbol)
            log_debug(f"🔁 반대 신호 청산 ({symbol})", f"성공: {success}")
            return jsonify({"status": "success" if success else "error"})

        # 기존 청산/진입 로직
        if side not in ["long", "short"] or action not in ["entry", "exit"]:
            return jsonify({"error": "Invalid side/action"}), 400
        if not update_position_state(symbol, timeout=1):
            return jsonify({"status": "error", "message": "포지션 조회 실패"}), 500
        current_side = position_state.get(symbol, {}).get("side")
        desired_side = "buy" if side == "long" else "sell"
        if action == "exit":
            success = close_position(symbol)
            log_debug(f"🔁 일반 청산 ({symbol})", f"성공: {success}")
            return jsonify({"status": "success" if success else "error"})
        if current_side and current_side != desired_side:
            log_debug("🔄 역포지션 처리", f"현재: {current_side} → 목표: {desired_side}")
            if not close_position(symbol):
                log_debug("❌ 역포지션 청산 실패", "")
                return jsonify({"status": "error", "message": "역포지션 청산 실패"})
            time.sleep(1)
            if not update_position_state(symbol):
                log_debug("❌ 역포지션 후 상태 갱신 실패", "")
        qty = get_max_qty(symbol, desired_side)
        log_debug(f"🧮 수량 계산 완료 ({symbol})", f"{qty} 계약")
        if qty <= 0:
            log_debug("❌ 수량 오류", f"계산된 수량: {qty}")
            return jsonify({"status": "error", "message": "수량 계산 오류"})
        success = place_order(symbol, desired_side, qty)
        log_debug(f"📨 최종 결과 ({symbol})", f"주문 성공: {success}")
        return jsonify({"status": "success" if success else "error", "qty": qty})
    except Exception as e:
        log_debug(f"❌ 웹훅 전체 실패 ({symbol or 'unknown'})", str(e))
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
            "positions": positions
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug_account():
    try:
        acc = api.list_futures_accounts(SETTLE)
        return jsonify({
            "raw_response": str(acc),
            "total": str(acc.total) if hasattr(acc, 'total') else "없음",
            "available": str(acc.available) if hasattr(acc, 'available') else "없음"
        })
    except Exception as e:
        return jsonify({"error": str(e)})

async def send_ping(ws):
    while True:
        try:
            await ws.ping()
        except Exception as e:
            log_debug("❌ 핑 실패", str(e))
            break
        await asyncio.sleep(10)

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
                asyncio.create_task(send_ping(ws))
                reconnect_delay = 5
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("event") == "subscribe":
                            log_debug("✅ 웹소켓 구독", f"채널: {data.get('channel')}")
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
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        log_debug("⚠️ 웹소켓", "연결 끊김, 재연결 시도")
                        break
                    except Exception as e:
                        log_debug("❌ 웹소켓 메시지 처리 실패", str(e))
                        continue
        except Exception as e:
            log_debug("❌ 웹소켓 연결 실패", str(e))
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

def process_ticker_data(ticker):
    try:
        contract = ticker.get("contract")
        last = ticker.get("last")
        if not contract or not last or contract not in SYMBOL_CONFIG:
            return
        try:
            price = Decimal(str(last).replace("E", "e")).normalize()
        except (InvalidOperation, ValueError):
            return
        if not update_position_state(contract, timeout=1):
            return
        pos = position_state.get(contract, {})
        entry = pos.get("price")
        if entry and pos.get("side"):
            cfg = SYMBOL_CONFIG[contract]
            side = pos["side"]
            # TP/SL 계산
            if side == "buy":
                sl = entry * (1 - cfg["sl_pct"])
                tp = entry * (1 + cfg["tp_pct"])
                if price <= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가: {price} <= SL: {sl}")
                    close_position(contract)
                elif price >= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가: {price} >= TP: {tp}")
                    close_position(contract)
            else:
                sl = entry * (1 + cfg["sl_pct"])
                tp = entry * (1 - cfg["tp_pct"])
                if price >= sl:
                    log_debug(f"🛑 SL 트리거 ({contract})", f"현재가: {price} >= SL: {sl}")
                    close_position(contract)
                elif price <= tp:
                    log_debug(f"🎯 TP 트리거 ({contract})", f"현재가: {price} <= TP: {tp}")
                    close_position(contract)
    except Exception as e:
        log_debug("❌ 티커 처리 실패", str(e))

def backup_position_loop():
    while True:
        try:
            for sym in SYMBOL_CONFIG:
                update_position_state(sym, timeout=1)
            time.sleep(300)
        except Exception as e:
            log_debug("❌ 백업 루프 오류", str(e))
            time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=lambda: asyncio.run(price_listener()), daemon=True).start()
    threading.Thread(target=backup_position_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    log_debug("🚀 서버 시작", f"Railway에서 {port} 포트로 시작")
    app.run(host="0.0.0.0", port=port)
