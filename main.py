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
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi

# === 로깅 설정 ===
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Werkzeug 로깅 비활성화
logging.getLogger('werkzeug').setLevel(logging.ERROR)

def log_debug(tag, msg, exc_info=False):
    """간단한 로깅 함수"""
    logger.info(f"[{tag}] {msg}")
    if exc_info:
        logger.exception("")

# === Flask 앱 ===
app = Flask(__name__)

# === API 설정 ===
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

# === 심볼 매핑 ===
SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "BTCUSDT.P": "BTC_USDT", "BTCUSDTPERP": "BTC_USDT", "BTC_USDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", "ETHUSDT.P": "ETH_USDT", "ETHUSDTPERP": "ETH_USDT", "ETH_USDT": "ETH_USDT",
    "SOLUSDT": "SOL_USDT", "SOLUSDT.P": "SOL_USDT", "SOLUSDTPERP": "SOL_USDT", "SOL_USDT": "SOL_USDT",
    "ADAUSDT": "ADA_USDT", "ADAUSDT.P": "ADA_USDT", "ADAUSDTPERP": "ADA_USDT", "ADA_USDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "SUIUSDT.P": "SUI_USDT", "SUIUSDTPERP": "SUI_USDT", "SUI_USDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "LINKUSDT.P": "LINK_USDT", "LINKUSDTPERP": "LINK_USDT", "LINK_USDT": "LINK_USDT",
    "PEPEUSDT": "PEPE_USDT", "PEPEUSDT.P": "PEPE_USDT", "PEPEUSDTPERP": "PEPE_USDT", "PEPE_USDT": "PEPE_USDT",
}

# === 심볼별 설정 ===
SYMBOL_CONFIG = {
    "BTC_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.0001"), "min_notional": Decimal("5"), "tp_mult": 0.6},
    "ETH_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("0.01"), "min_notional": Decimal("5"), "tp_mult": 0.7},
    "SOL_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 0.9},
    "ADA_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10"), "min_notional": Decimal("5"), "tp_mult": 1.0},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0},
    "LINK_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("1"), "min_notional": Decimal("5"), "tp_mult": 1.0},
    "PEPE_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "contract_size": Decimal("10000000"), "min_notional": Decimal("5"), "tp_mult": 1.0},
}

# === Gate.io API 클라이언트 ===
config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# === 전역 변수 ===
position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
recent_signals = {}
signal_lock = threading.RLock()
tpsl_storage = {}
tpsl_lock = threading.RLock()

COOLDOWN_SECONDS = 14  # 14초 쿨다운

# === 핵심 함수들 ===
def normalize_symbol(raw_symbol):
    """심볼 정규화"""
    if not raw_symbol:
        return None
    symbol = str(raw_symbol).upper().strip()
    
    # 직접 매핑 확인
    if symbol in SYMBOL_MAPPING:
        return SYMBOL_MAPPING[symbol]
    
    # 접미사 제거 후 재시도
    for suffix in ['.P', 'PERP']:
        if symbol.endswith(suffix):
            base = symbol[:-len(suffix)]
            if base in SYMBOL_MAPPING:
                return SYMBOL_MAPPING[base]
    
    return None

def get_total_collateral(force=False):
    """총 자산 조회"""
    now = time.time()
    if not force and account_cache["time"] > now - 30 and account_cache["data"]:
        return account_cache["data"]
    
    try:
        # Unified API 시도
        try:
            unified = unified_api.list_unified_accounts()
            for attr in ['unified_account_total_equity', 'equity']:
                if hasattr(unified, attr):
                    equity = Decimal(str(getattr(unified, attr)))
                    account_cache.update({"time": now, "data": equity})
                    return equity
        except Exception:
            pass
        
        # Futures API로 폴백
        acc = api.list_futures_accounts(SETTLE)
        available = Decimal(str(getattr(acc, 'available', '0')))
        account_cache.update({"time": now, "data": available})
        return available
    except Exception as e:
        log_debug("❌ 자산 조회 실패", str(e))
        return Decimal("0")

def get_price(symbol):
    """현재 가격 조회"""
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if ticker and len(ticker) > 0:
            return Decimal(str(ticker[0].last))
        return Decimal("0")
    except Exception as e:
        log_debug(f"❌ 가격 조회 실패 ({symbol})", str(e))
        return Decimal("0")

def calculate_dynamic_tp(symbol, atr_15s, signal_type):
    """동적 TP 계산 (15초봉 ATR 기반)"""
    try:
        cfg = SYMBOL_CONFIG[symbol]
        price = get_price(symbol)
        if price <= 0:
            price = Decimal("1")
        
        # ATR 변동성 계수 (0.8~1.5)
        atr_ratio = Decimal(str(atr_15s)) / price
        if atr_ratio < Decimal("0.0005"):
            vol_factor = Decimal("0.8")
        elif atr_ratio > Decimal("0.002"):
            vol_factor = Decimal("1.5")
        else:
            vol_factor = Decimal("0.8") + (atr_ratio - Decimal("0.0005")) / Decimal("0.0015") * Decimal("0.7")
        
        # 기본값 (파인스크립트 v6.7)
        base_tp = Decimal("0.004")  # 0.4%
        
        # 신호별 배수
        if signal_type == "backup_enhanced":
            tp_mult = Decimal("0.8")
        else:
            tp_mult = Decimal("1.2")
        
        # 최종 계산
        tp = base_tp * tp_mult * vol_factor * Decimal(str(cfg["tp_mult"]))
        
        # 범위 제한
        if signal_type == "backup_enhanced":
            tp = min(max(tp, Decimal("0.002")), Decimal("0.005"))
        else:
            tp = min(max(tp, Decimal("0.003")), Decimal("0.006"))
        
        return tp
    except Exception:
        # 실패시 기본값
        return Decimal("0.004")

def store_tpsl(symbol, tp):
    """TP 저장"""
    with tpsl_lock:
        tpsl_storage[symbol] = {"tp": tp, "time": time.time()}

def get_tp(symbol):
    """저장된 TP 조회"""
    with tpsl_lock:
        if symbol in tpsl_storage:
            return tpsl_storage[symbol]["tp"]
    # 기본값
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0})
    return Decimal("0.004") * Decimal(str(cfg["tp_mult"]))

def is_duplicate(data):
    """중복 신호 체크 (14초 쿨다운)"""
    with signal_lock:
        now = time.time()
        symbol = data.get("symbol", "")
        side = data.get("side", "")
        action = data.get("action", "")
        
        if action == "entry":
            key = f"{symbol}_{side}"
            
            # 같은 방향 14초 체크
            if key in recent_signals:
                if now - recent_signals[key]["time"] < COOLDOWN_SECONDS:
                    return True
            
            # 기록
            recent_signals[key] = {"time": now}
            
            # 반대 방향 제거
            opposite = f"{symbol}_{'short' if side == 'long' else 'long'}"
            recent_signals.pop(opposite, None)
        
        # 오래된 기록 정리
        recent_signals.update({k: v for k, v in recent_signals.items() if now - v["time"] < 300})
        
        return False

def calculate_position_size(symbol, signal_type):
    """포지션 크기 계산 (진입 횟수별 차등)"""
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    
    if equity <= 0 or price <= 0:
        return Decimal("0")
    
    # 현재 포지션 횟수 확인
    entry_count = position_state.get(symbol, {}).get("entry_count", 0)
    
    # 진입 횟수별 비율 (20% → 30% → 70% → 200% 이후 계속 200%)
    if entry_count == 0:
        ratio = Decimal("0.2")  # 첫 진입: 20%
    elif entry_count == 1:
        ratio = Decimal("0.3")  # 두번째: 30%
    elif entry_count == 2:
        ratio = Decimal("0.7")  # 세번째: 70%
    else:  # 3회 이상
        ratio = Decimal("2.0")  # 네번째 이후: 200%
    
    log_debug(f"📊 수량 계산 ({symbol})", f"진입 횟수: {entry_count + 1}, 비율: {ratio * 100}%")
    
    # 수량 계산
    adjusted = equity * ratio
    raw_qty = adjusted / (price * cfg["contract_size"])
    qty = (raw_qty // cfg["qty_step"]) * cfg["qty_step"]
    final_qty = max(qty, cfg["min_qty"])
    
    # 최소 주문금액 체크
    value = final_qty * price * cfg["contract_size"]
    if value < cfg["min_notional"]:
        return Decimal("0")
    
    return final_qty

def update_position_state(symbol):
    """포지션 상태 업데이트"""
    with position_lock:
        try:
            pos = api.get_position(SETTLE, symbol)
            size = Decimal(str(pos.size))
            
            if size != 0:
                # 기존 진입 횟수 및 시간 유지
                existing_count = position_state.get(symbol, {}).get("entry_count", 0)
                existing_time = position_state.get(symbol, {}).get("entry_time", time.time())
                
                position_state[symbol] = {
                    "price": Decimal(str(pos.entry_price)),
                    "side": "buy" if size > 0 else "sell",
                    "size": abs(size),
                    "value": abs(size) * Decimal(str(pos.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"],
                    "entry_count": existing_count,
                    "entry_time": existing_time
                }
            else:
                position_state[symbol] = {
                    "price": None, 
                    "side": None, 
                    "size": Decimal("0"), 
                    "value": Decimal("0"),
                    "entry_count": 0,
                    "entry_time": None
                }
            return True
        except Exception as e:
            if "POSITION_NOT_FOUND" in str(e):
                position_state[symbol] = {
                    "price": None, 
                    "side": None, 
                    "size": Decimal("0"), 
                    "value": Decimal("0"),
                    "entry_count": 0,
                    "entry_time": None
                }
                return True
            return False

def place_order(symbol, side, qty):
    """주문 실행"""
    with position_lock:
        try:
            cfg = SYMBOL_CONFIG[symbol]
            qty_dec = Decimal(str(qty)).quantize(cfg["qty_step"], rounding=ROUND_DOWN)
            
            if qty_dec < cfg["min_qty"]:
                return False
            
            size = float(qty_dec) if side == "buy" else -float(qty_dec)
            order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=False)
            
            api.create_futures_order(SETTLE, order)
            
            # 진입 횟수 증가 및 시간 기록
            current_count = position_state.get(symbol, {}).get("entry_count", 0)
            if current_count == 0:  # 첫 진입시만 시간 기록
                position_state.setdefault(symbol, {})["entry_time"] = time.time()
            position_state.setdefault(symbol, {})["entry_count"] = current_count + 1
            
            log_debug(f"✅ 주문 성공 ({symbol})", 
                     f"{side.upper()} {float(qty_dec)} 계약 (진입 #{current_count + 1})")
            
            time.sleep(2)
            update_position_state(symbol)
            return True
            
        except Exception as e:
            log_debug(f"❌ 주문 실패 ({symbol})", str(e))
            return False

def close_position(symbol, reason="manual"):
    """포지션 청산"""
    with position_lock:
        try:
            api.create_futures_order(SETTLE, FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True))
            log_debug(f"✅ 청산 완료 ({symbol})", f"이유: {reason}")
            
            # 진입 횟수 및 시간 초기화
            if symbol in position_state:
                position_state[symbol]["entry_count"] = 0
                position_state[symbol]["entry_time"] = None
            
            # 데이터 정리
            with signal_lock:
                keys = [k for k in recent_signals.keys() if k.startswith(symbol + "_")]
                for k in keys:
                    recent_signals.pop(k, None)
            
            with tpsl_lock:
                tpsl_storage.pop(symbol, None)
            
            time.sleep(1)
            update_position_state(symbol)
            return True
        except Exception as e:
            log_debug(f"❌ 청산 실패 ({symbol})", str(e))
            return False

# === Flask 라우트 ===
@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    """캐시 초기화"""
    with signal_lock:
        recent_signals.clear()
    with tpsl_lock:
        tpsl_storage.clear()
    return jsonify({"status": "cache_cleared"})

@app.before_request
def log_request():
    """요청 로깅"""
    if request.path != "/ping":
        log_debug("🌐 요청", f"{request.method} {request.path}")

@app.route("/", methods=["POST"])
def webhook():
    """파인스크립트 웹훅 처리"""
    try:
        # 데이터 파싱
        raw_data = request.get_data(as_text=True)
        if not raw_data:
            return jsonify({"error": "Empty data"}), 400
        
        data = None
        
        # JSON 파싱 시도
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            # Form 데이터 시도
            if request.form:
                data = request.form.to_dict()
            # URL 디코딩 시도
            elif "&" not in raw_data and "=" not in raw_data:
                try:
                    import urllib.parse
                    decoded = urllib.parse.unquote(raw_data)
                    data = json.loads(decoded)
                except Exception:
                    pass
        
        if not data:
            # 트레이딩뷰 플레이스홀더 체크
            if "{{" in raw_data and "}}" in raw_data:
                return jsonify({
                    "error": "TradingView placeholder detected",
                    "solution": "Use {{strategy.order.alert_message}} in TradingView alert message field"
                }), 400
            return jsonify({"error": "Failed to parse data"}), 400
        
        # 필드 추출
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        signal_type = data.get("signal_type", "none")
        atr_15s = data.get("atr_15s", 0)
        
        # 심볼 정규화
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": f"Invalid symbol: {raw_symbol}"}), 400
        
        # 중복 체크
        if is_duplicate(data):
            return jsonify({"status": "duplicate_ignored"}), 200
        
        # 청산 처리
        if action == "exit":
            update_position_state(symbol)
            if position_state.get(symbol, {}).get("side"):
                close_position(symbol, data.get("reason", "signal"))
            return jsonify({"status": "success", "action": "exit"})
        
        # 진입 처리
        if action == "entry" and side in ["long", "short"]:
            # 동적 TP 계산
            tp = calculate_dynamic_tp(symbol, atr_15s, signal_type)
            store_tpsl(symbol, tp)
            
            # 포지션 확인
            update_position_state(symbol)
            current = position_state.get(symbol, {}).get("side")
            desired = "buy" if side == "long" else "sell"
            
            # 반대 포지션 청산
            if current and current != desired:
                if not close_position(symbol, "reverse"):
                    return jsonify({"status": "error", "message": "Failed to close opposite position"})
                time.sleep(3)
            
            # 수량 계산 및 주문
            qty = calculate_position_size(symbol, signal_type)
            if qty <= 0:
                return jsonify({"status": "error", "message": "Invalid quantity"})
            
            success = place_order(symbol, desired, qty)
            
            # 상세 응답
            return jsonify({
                "status": "success" if success else "error",
                "action": "entry",
                "symbol": symbol,
                "side": side,
                "qty": float(qty),
                "signal_type": signal_type,
                "dynamic_tp": {
                    "tp_pct": float(tp) * 100,
                    "atr_15s": float(atr_15s)
                }
            })
        
        return jsonify({"error": "Invalid action"}), 400
        
    except Exception as e:
        log_debug("❌ 웹훅 오류", str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    """서버 상태"""
    try:
        equity = get_total_collateral(force=True)
        positions = {}
        
        for sym in SYMBOL_CONFIG:
            update_position_state(sym)
            pos = position_state.get(sym, {})
            if pos.get("side"):
                tp = get_tp(sym)
                positions[sym] = {
                    "side": pos["side"],
                    "size": float(pos["size"]),
                    "price": float(pos["price"]),
                    "value": float(pos["value"]),
                    "tp_pct": float(tp) * 100,
                    "entry_count": pos.get("entry_count", 0)
                }
        
        return jsonify({
            "status": "running",
            "version": "v6.7",
            "balance": float(equity),
            "positions": positions,
            "cooldown": COOLDOWN_SECONDS
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === WebSocket 모니터링 ===
async def price_monitor():
    """실시간 가격 모니터링"""
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    symbols = list(SYMBOL_CONFIG.keys())
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                # 구독
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": symbols
                }))
                
                log_debug("📡 웹소켓", f"구독 완료: {symbols}")
                
                # 메시지 처리
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    data = json.loads(msg)
                    
                    # 에러 처리
                    if data.get("event") == "error":
                        log_debug("❌ 웹소켓 에러", data.get("message", "Unknown error"))
                        continue
                    
                    # 구독 확인
                    if data.get("event") == "subscribe":
                        log_debug("✅ 구독 확인", data.get("channel", ""))
                        continue
                    
                    # 가격 데이터 처리
                    result = data.get("result")
                    if not result:
                        continue
                    
                    if isinstance(result, list):
                        for item in result:
                            check_tp(item)
                    elif isinstance(result, dict):
                        check_tp(result)
                        
        except asyncio.TimeoutError:
            log_debug("⏱️ 웹소켓 타임아웃", "재연결 시도...")
        except websockets.exceptions.ConnectionClosed:
            log_debug("🔌 웹소켓 연결 종료", "재연결 시도...")
        except Exception as e:
            log_debug("❌ 웹소켓 오류", str(e))
        
        await asyncio.sleep(5)

def check_tp(ticker):
    """TP 체크 (30분마다 10% 감소, 최소 0.12%)"""
    try:
        symbol = ticker.get("contract")
        price = Decimal(str(ticker.get("last", "0")))
        
        if not symbol or symbol not in SYMBOL_CONFIG or price <= 0:
            return
        
        with position_lock:
            pos = position_state.get(symbol, {})
            entry = pos.get("price")
            side = pos.get("side")
            entry_time = pos.get("entry_time")
            
            if not entry or not side or not entry_time:
                return
            
            # 원본 TP 가져오기
            original_tp = get_tp(symbol)
            
            # 30분마다 10% 감소 계산
            time_elapsed = time.time() - entry_time
            periods_30min = int(time_elapsed / 1800)  # 30분 = 1800초
            
            # 감소 계산
            decay_factor = max(0.3, 1 - (periods_30min * 0.1))  # 최소 30%
            adjusted_tp = original_tp * Decimal(str(decay_factor))
            
            # 최소 0.12% 보장
            min_tp = Decimal("0.0012")  # 0.12%
            adjusted_tp = max(adjusted_tp, min_tp)
            
            # 디버깅용 로그 (30분마다 한번만)
            if periods_30min > 0 and int(time_elapsed) % 1800 < 10:
                log_debug(f"📉 TP 감소 ({symbol})", 
                         f"경과: {periods_30min*30}분, 원본TP: {original_tp*100:.2f}%, "
                         f"조정TP: {adjusted_tp*100:.2f}%")
            
            if side == "buy":
                if price >= entry * (1 + adjusted_tp):
                    log_debug(f"🎯 TP 트리거 ({symbol})", 
                             f"가격: {price}, 조정TP: {entry * (1 + adjusted_tp)} "
                             f"({adjusted_tp*100:.2f}%, {periods_30min*30}분 경과)")
                    close_position(symbol, "take_profit")
            else:
                if price <= entry * (1 - adjusted_tp):
                    log_debug(f"🎯 TP 트리거 ({symbol})", 
                             f"가격: {price}, 조정TP: {entry * (1 - adjusted_tp)} "
                             f"({adjusted_tp*100:.2f}%, {periods_30min*30}분 경과)")
                    close_position(symbol, "take_profit")
    except:
        pass

# === 메인 실행 ===
if __name__ == "__main__":
    log_debug("🚀 서버 시작", "v6.7 - 진입별 수량: 20%→30%→70%→200%")
    log_debug("📊 설정", f"심볼: {len(SYMBOL_CONFIG)}개")
    log_debug("✅ TP만 사용", "SL 비활성화, 파인스크립트 RSI 청산 활성")
    log_debug("🎯 가중치", "BTC 60%, ETH 70%, SOL 90%, 기타 100%")
    log_debug("📈 진입 전략", "최대 4회 진입, 단계별 수량 증가")
    
    # 초기 상태
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산", f"{equity} USDT")
    
    # 초기 포지션 확인
    for symbol in SYMBOL_CONFIG:
        if update_position_state(symbol):
            pos = position_state.get(symbol, {})
            if pos.get("side"):
                log_debug(f"📊 포지션 ({symbol})", 
                         f"{pos['side']} {pos['size']} @ {pos['price']} (진입 #{pos.get('entry_count', 0)})")
    
    # 백업 루프 (5분마다 포지션 갱신)
    def backup_loop():
        while True:
            try:
                time.sleep(300)  # 5분
                for symbol in SYMBOL_CONFIG:
                    update_position_state(symbol)
            except:
                pass
    
    # 스레드 시작
    threading.Thread(target=backup_loop, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(price_monitor()), daemon=True).start()
    
    # Flask 실행
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹서버", f"포트 {port}에서 실행")
    app.run(host="0.0.0.0", port=port, debug=False)
