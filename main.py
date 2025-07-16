"""
Gate.io 자동매매 서버 v6.10 - 파인스크립트 완전 대응

실행 순서:
1. 서버 초기화
   - API 키 로드
   - Flask 앱 생성
   - 전역 변수 초기화
   
2. 웹훅 수신 (/webhook)
   - 파인스크립트 알림 수신
   - JSON 파싱 및 검증
   - 심볼 정규화
   
3. 신호 처리
   - 중복 신호 체크 (14초 쿨다운)
   - 진입/청산 구분
   
4. 진입 처리
   - 반대 포지션 청산
   - TP/SL 계산 및 저장
   - 포지션 크기 계산
   - 주문 실행
   
5. 백그라운드 모니터링
   - WebSocket 가격 모니터링
   - TP/SL 체크 (15초마다 감소)
   - 포지션 상태 업데이트
"""

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

# ========================================
# 1. 로깅 설정
# ========================================

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

# ========================================
# 2. Flask 앱 및 API 설정
# ========================================

app = Flask(__name__)

# API 설정
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

# Gate.io API 클라이언트
config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)
unified_api = UnifiedApi(client)

# ========================================
# 3. 상수 및 설정
# ========================================

# 쿨다운 설정 (14초)
COOLDOWN_SECONDS = 14

# 심볼 매핑
SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT", "BTCUSDT.P": "BTC_USDT", "BTCUSDTPERP": "BTC_USDT", "BTC_USDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", "ETHUSDT.P": "ETH_USDT", "ETHUSDTPERP": "ETH_USDT", "ETH_USDT": "ETH_USDT",
    "SOLUSDT": "SOL_USDT", "SOLUSDT.P": "SOL_USDT", "SOLUSDTPERP": "SOL_USDT", "SOL_USDT": "SOL_USDT",
    "ADAUSDT": "ADA_USDT", "ADAUSDT.P": "ADA_USDT", "ADAUSDTPERP": "ADA_USDT", "ADA_USDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT", "SUIUSDT.P": "SUI_USDT", "SUIUSDTPERP": "SUI_USDT", "SUI_USDT": "SUI_USDT",
    "LINKUSDT": "LINK_USDT", "LINKUSDT.P": "LINK_USDT", "LINKUSDTPERP": "LINK_USDT", "LINK_USDT": "LINK_USDT",
    "PEPEUSDT": "PEPE_USDT", "PEPEUSDT.P": "PEPE_USDT", "PEPEUSDTPERP": "PEPE_USDT", "PEPE_USDT": "PEPE_USDT",
    "XRPUSDT": "XRP_USDT", "XRPUSDT.P": "XRP_USDT", "XRPUSDTPERP": "XRP_USDT", "XRP_USDT": "XRP_USDT",
    "DOGEUSDT": "DOGE_USDT", "DOGEUSDT.P": "DOGE_USDT", "DOGEUSDTPERP": "DOGE_USDT", "DOGE_USDT": "DOGE_USDT",
}

# 심볼별 설정 (파인스크립트 v6.10 가중치 완전 반영)
SYMBOL_CONFIG = {
    "BTC_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("0.0001"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.6,  # 파인스크립트 가중치
        "sl_mult": 0.6   # 파인스크립트 가중치
    },
    "ETH_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("0.01"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.7,
        "sl_mult": 0.7
    },
    "SOL_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("1"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 0.9,
        "sl_mult": 0.9
    },
    "ADA_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("10"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
    "SUI_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("1"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
    "LINK_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("1"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
    "PEPE_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("10000000"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.2,
        "sl_mult": 1.2
    },
    "XRP_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("10"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
       "DOGE_USDT": {
        "min_qty": Decimal("1"), 
        "qty_step": Decimal("1"), 
        "contract_size": Decimal("10"), 
        "min_notional": Decimal("5"), 
        "tp_mult": 1.0,
        "sl_mult": 1.0
    },
}

# ========================================
# 4. 전역 변수
# ========================================

position_state = {}
position_lock = threading.RLock()
account_cache = {"time": 0, "data": None}
recent_signals = {}
signal_lock = threading.RLock()
tpsl_storage = {}
tpsl_lock = threading.RLock()

# ========================================
# 5. 핵심 유틸리티 함수
# ========================================

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

# ========================================
# 6. TP/SL 저장 및 관리
# ========================================

def store_tp_sl(symbol, tp, sl):
    """TP/SL 저장"""
    with tpsl_lock:
        tpsl_storage[symbol] = {
            "tp": tp, 
            "sl": sl, 
            "time": time.time()
        }

def get_tp_sl(symbol):
    """저장된 TP/SL 조회"""
    with tpsl_lock:
        if symbol in tpsl_storage:
            data = tpsl_storage[symbol]
            return data["tp"], data["sl"]
    
    # 기본값 반환 (파인스크립트와 동일)
    cfg = SYMBOL_CONFIG.get(symbol, {"tp_mult": 1.0, "sl_mult": 1.0})
    default_tp = Decimal("0.0049") * Decimal(str(cfg["tp_mult"]))  # 0.5% * 가중치
    default_sl = Decimal("0.019") * Decimal(str(cfg["sl_mult"]))   # 2% * 가중치
    return default_tp, default_sl

# ========================================
# 7. 중복 신호 체크
# ========================================

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
        
        # 오래된 기록 정리 (5분)
        recent_signals.update({
            k: v for k, v in recent_signals.items() 
            if now - v["time"] < 300
        })
        
        return False

# ========================================
# 8. 포지션 크기 계산
# ========================================

def calculate_position_size(symbol, signal_type, data=None):
    """포지션 크기 계산 (파인스크립트 수량 비율 완전 반영)"""
    cfg = SYMBOL_CONFIG[symbol]
    equity = get_total_collateral()
    price = get_price(symbol)
    
    if equity <= 0 or price <= 0:
        return Decimal("0")
    
    # 현재 진입 횟수 판단
    entry_count = 0
    if symbol in position_state:
        entry_count = position_state[symbol].get("entry_count", 0)
    
    # 파인스크립트 전략명으로 판단
    if data and "strategy" in data:
        strategy = data.get("strategy", "")
        if "Pyramid" in strategy and entry_count == 0:
            entry_count = 1  # Pyramid은 최소 2차 진입
    
    # 진입 횟수별 비율 (파인스크립트와 동일)
    # 파인스크립트: 2% → 4% → 12% → 50%
    # 서버: 실제 20% → 40% → 120% → 500%
    if entry_count == 0:
        ratio = Decimal("0.2")   # 첫 진입: 20%
    elif entry_count == 1:
        ratio = Decimal("0.4")   # 두번째: 40%
    elif entry_count == 2:
        ratio = Decimal("0.12")   # 세번째: 120%
    elif entry_count >= 3:
        ratio = Decimal("5.0")   # 네번째: 500%
    else:
        ratio = Decimal("0.15")
    
    next_entry_number = entry_count + 1
    
    log_debug(f"📊 수량 계산 ({symbol})", 
             f"진입 횟수: {next_entry_number}, 비율: {ratio * 100}%")
    
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

# ========================================
# 9. 포지션 상태 관리
# ========================================

def update_position_state(symbol):
    """포지션 상태 업데이트"""
    with position_lock:
        try:
            pos = api.get_position(SETTLE, symbol)
            size = Decimal(str(pos.size))
            
            if size != 0:
                # 기존 데이터 유지
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

# ========================================
# 10. 주문 실행
# ========================================

def place_order(symbol, side, qty):
    """
    주문 실행
    
    처리 순서:
    1. 수량 검증 및 정규화
    2. 주문 생성 및 실행
    3. 진입 횟수 및 시간 기록
    4. 포지션 상태 업데이트
    """
    with position_lock:
        try:
            cfg = SYMBOL_CONFIG[symbol]
            qty_dec = Decimal(str(qty)).quantize(cfg["qty_step"], rounding=ROUND_DOWN)
            
            if qty_dec < cfg["min_qty"]:
                return False
            
            size = float(qty_dec) if side == "buy" else -float(qty_dec)
            order = FuturesOrder(
                contract=symbol, 
                size=size, 
                price="0", 
                tif="ioc", 
                reduce_only=False
            )
            
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
    """
    포지션 청산
    
    처리 순서:
    1. 청산 주문 실행
    2. 진입 정보 초기화
    3. 관련 데이터 정리
    4. 포지션 상태 업데이트
    """
    with position_lock:
        try:
            api.create_futures_order(
                SETTLE, 
                FuturesOrder(
                    contract=symbol, 
                    size=0, 
                    price="0", 
                    tif="ioc", 
                    close=True
                )
            )
            
            log_debug(f"✅ 청산 완료 ({symbol})", f"이유: {reason}")
            
            # 진입 횟수 및 시간 초기화
            if symbol in position_state:
                position_state[symbol]["entry_count"] = 0
                position_state[symbol]["entry_time"] = None
            
            # 관련 데이터 정리
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

# ========================================
# 11. Flask 라우트
# ========================================

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
    """
    파인스크립트 웹훅 처리
    
    처리 순서:
    1. 데이터 파싱 (JSON/Form/URL-encoded)
    2. 필드 추출 및 검증
    3. 심볼 정규화
    4. 중복 신호 체크
    5. 액션별 처리:
       - exit: 포지션 청산
       - entry: 새 포지션 진입
    """
    try:
        # ========== 1. 데이터 파싱 ==========
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
        
        # ========== 2. 필드 추출 ==========
        raw_symbol = data.get("symbol", "")
        side = data.get("side", "").lower()
        action = data.get("action", "").lower()
        signal_type = data.get("signal_type", "none")
        atr_15s = data.get("atr_15s", 0)
        entry_number = data.get("entry_number", 1)
        tightening_mult = data.get("tightening_mult", 1.0)
        sl_pct = data.get("sl_pct", 0)  # 파인스크립트에서 전달받은 SL%
        
        # ========== 3. 심볼 정규화 ==========
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in SYMBOL_CONFIG:
            return jsonify({"error": f"Invalid symbol: {raw_symbol}"}), 400
        
        # ========== 4. 중복 체크 ==========
        if is_duplicate(data):
            return jsonify({"status": "duplicate_ignored"}), 200
        
        # ========== 5. 청산 처리 ==========
        if action == "exit":
            # TP/SL 청산은 서버에서 처리하므로 무시
            if data.get("reason") in ["take_profit", "stop_loss"]:
                return jsonify({"status": "ignored", "reason": "tp_sl_handled_by_server"}), 200
            
            update_position_state(symbol)
            if position_state.get(symbol, {}).get("side"):
                close_position(symbol, data.get("reason", "signal"))
            return jsonify({"status": "success", "action": "exit"})
        
        # ========== 6. 진입 처리 ==========
        if action == "entry" and side in ["long", "short"]:
            # TP 계산 (기본 0.5% * 심볼 가중치)
            tp = Decimal("0.005") * Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
            
            # SL 계산 (기본 2% * 심볼 가중치 또는 파인스크립트 전달값)
            if sl_pct > 0:  # 파인스크립트에서 전달된 값이 있으면
                sl = Decimal(str(sl_pct)) / 100
            else:  # 없으면 기본값 사용
                sl = Decimal("0.02") * Decimal(str(SYMBOL_CONFIG[symbol]["sl_mult"]))
            
            store_tp_sl(symbol, tp, sl)
            
            # 포지션 확인
            update_position_state(symbol)
            current = position_state.get(symbol, {}).get("side")
            desired = "buy" if side == "long" else "sell"
            
            # 반대 포지션 청산
            if current and current != desired:
                if not close_position(symbol, "reverse"):
                    return jsonify({
                        "status": "error", 
                        "message": "Failed to close opposite position"
                    })
                time.sleep(1)
                update_position_state(symbol)
            
            # 최대 4회 진입 체크 (파인스크립트 pyramiding=4)
            entry_count = position_state.get(symbol, {}).get("entry_count", 0)
            if entry_count >= 4:
                return jsonify({
                    "status": "max_entries", 
                    "message": "Maximum 4 entries reached"
                })
            
            # 수량 계산 및 주문
            qty = calculate_position_size(symbol, signal_type, data)
            if qty <= 0:
                return jsonify({
                    "status": "error", 
                    "message": "Invalid quantity"
                })
            
            success = place_order(symbol, desired, qty)
            
            # 상세 응답
            return jsonify({
                "status": "success" if success else "error",
                "action": "entry",
                "symbol": symbol,
                "side": side,
                "qty": float(qty),
                "entry_count": entry_count + 1,
                "entry_number": entry_number,
                "tightening_mult": tightening_mult,
                "signal_type": signal_type,
                "tp_pct": float(tp) * 100,
                "sl_pct": float(sl) * 100
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
                tp, sl = get_tp_sl(sym)
                entry_count = pos.get("entry_count", 0)
                
                positions[sym] = {
                    "side": pos["side"],
                    "size": float(pos["size"]),
                    "price": float(pos["price"]),
                    "value": float(pos["value"]),
                    "tp_pct": float(tp) * 100,
                    "sl_pct": float(sl) * 100,
                    "entry_count": entry_count,
                    "symbol_multiplier": SYMBOL_CONFIG[sym]["tp_mult"]
                }
        
        return jsonify({
            "status": "running",
            "version": "v6.10",
            "balance": float(equity),
            "positions": positions,
            "cooldown": COOLDOWN_SECONDS,
            "symbol_weights": {sym: cfg["tp_mult"] for sym, cfg in SYMBOL_CONFIG.items()}
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========================================
# 12. WebSocket 모니터링
# ========================================

async def price_monitor():
    """실시간 가격 모니터링 및 TP/SL 체크"""
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
                            check_tp_sl(item)
                    elif isinstance(result, dict):
                        check_tp_sl(result)
                        
        except asyncio.TimeoutError:
            log_debug("⏱️ 웹소켓 타임아웃", "재연결 시도...")
        except websockets.exceptions.ConnectionClosed:
            log_debug("🔌 웹소켓 연결 종료", "재연결 시도...")
        except Exception as e:
            log_debug("❌ 웹소켓 오류", str(e))
        
        await asyncio.sleep(5)

def check_tp_sl(ticker):
    """
    TP/SL 체크 (15초마다 감소 로직)
    
    처리 순서:
    1. 심볼/가격 검증
    2. 포지션 정보 확인
    3. 시간 경과 계산
    4. TP/SL 감소 적용
    5. 트리거 체크 및 청산 실행
    """
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

            # 원본 TP/SL 조회
            original_tp, original_sl = get_tp_sl(symbol)
            
            # 시간 경과 계산 (초 단위)
            time_elapsed = time.time() - entry_time
            periods_15s = int(time_elapsed / 15)  # 15초 단위
            
            # 심볼별 가중치 가져오기
            symbol_weight = Decimal(str(SYMBOL_CONFIG[symbol]["tp_mult"]))
            
            # TP 감소: 심볼별 가중치 적용
            # BTC(0.6): 15초마다 0.006% 감소, PEPE(1.2): 15초마다 0.012% 감소
            tp_decay_weighted = Decimal("0.00006") * symbol_weight  # 0.006% * 가중치
            tp_reduction = Decimal(str(periods_15s)) * tp_decay_weighted
            adjusted_tp = max(Decimal("0.001"), original_tp - tp_reduction)  # 최소 0.1%
            
            # SL 감소: 심볼별 가중치 적용
            # BTC(0.6): 15초마다 0.012% 감소, PEPE(1.2): 15초마다 0.024% 감소
            sl_decay_weighted = Decimal("0.0001") * symbol_weight  # 0.01% * 가중치
            sl_reduction = Decimal(str(periods_15s)) * sl_decay_weighted
            adjusted_sl = max(Decimal("0.0008"), original_sl - sl_reduction)  # 최소 0.08%

            # TP/SL 트리거 체크
            tp_triggered = False
            sl_triggered = False

            if side == "buy":
                tp_price = entry * (1 + adjusted_tp)
                sl_price = entry * (1 - adjusted_sl)
                if price >= tp_price:
                    tp_triggered = True
                elif price <= sl_price:
                    sl_triggered = True
            else:
                tp_price = entry * (1 - adjusted_tp)
                sl_price = entry * (1 + adjusted_sl)
                if price <= tp_price:
                    tp_triggered = True
                elif price >= sl_price:
                    sl_triggered = True

            if tp_triggered:
                log_debug(f"🎯 TP 트리거 ({symbol})", 
                         f"가격: {price}, TP: {tp_price} ({adjusted_tp*100:.2f}%, {periods_15s*15}초 경과)")
                close_position(symbol, "take_profit")
            elif sl_triggered:
                log_debug(f"🛑 SL 트리거 ({symbol})", 
                         f"가격: {price}, SL: {sl_price} ({adjusted_sl*100:.2f}%, {periods_15s*15}초 경과)")
                close_position(symbol, "stop_loss")

    except Exception as e:
        log_debug(f"❌ TP/SL 체크 오류 ({ticker.get('contract', 'Unknown')})", str(e))

# ========================================
# 13. 백그라운드 모니터링 작업
# ========================================

def position_monitor():
    """포지션 상태 주기적 모니터링"""
    while True:
        try:
            time.sleep(300)  # 5분마다
            
            total_value = Decimal("0")
            active_positions = []
            
            for symbol in SYMBOL_CONFIG:
                if update_position_state(symbol):
                    pos = position_state.get(symbol, {})
                    if pos.get("side"):
                        total_value += pos["value"]
                        entry_count = pos.get("entry_count", 0)
                        tp_mult = SYMBOL_CONFIG[symbol]["tp_mult"]
                        active_positions.append(
                            f"{symbol}: {pos['side']} {pos['size']} @ {pos['price']} "
                            f"(진입 #{entry_count}, 가중치: {tp_mult})"
                        )
            
            if active_positions:
                equity = get_total_collateral()
                exposure_pct = (total_value / equity * 100) if equity > 0 else 0
                log_debug("📊 포지션 현황", 
                         f"활성: {len(active_positions)}개, "
                         f"총 가치: {total_value:.2f} USDT, "
                         f"노출도: {exposure_pct:.1f}%")
                for pos_info in active_positions:
                    log_debug("  └", pos_info)
                
        except Exception as e:
            log_debug("❌ 포지션 모니터링 오류", str(e))

def system_monitor():
    """시스템 상태 주기적 체크"""
    while True:
        try:
            time.sleep(3600)  # 1시간마다
            
            # 메모리 정리
            with signal_lock:
                now = time.time()
                recent_signals.update({
                    k: v for k, v in recent_signals.items() 
                    if now - v["time"] < 3600
                })
            
            with tpsl_lock:
                now = time.time()
                tpsl_storage.update({
                    k: v for k, v in tpsl_storage.items() 
                    if now - v["time"] < 86400
                })
            
            # 시스템 상태 로그
            log_debug("🔧 시스템 상태", 
                     f"신호 캐시: {len(recent_signals)}개, "
                     f"TP/SL 저장소: {len(tpsl_storage)}개")
            
        except Exception as e:
            log_debug("❌ 시스템 모니터링 오류", str(e))

# ========================================
# 14. 메인 실행
# ========================================

if __name__ == "__main__":
    """
    서버 시작 및 초기화
    
    실행 순서:
    1. 서버 설정 로그 출력
    2. 초기 자산 및 포지션 확인
    3. 백그라운드 스레드 시작
       - 포지션 모니터링 (5분 주기)
       - 시스템 모니터링 (1시간 주기)
       - WebSocket 가격 모니터링 (실시간)
    4. Flask 웹서버 시작
    """
    
    log_debug("🚀 서버 시작", "v6.10 - 파인스크립트 단계별 강화 완전 대응")
    log_debug("📊 설정", f"심볼: {len(SYMBOL_CONFIG)}개, 쿨다운: {COOLDOWN_SECONDS}초")
    
    # 심볼별 가중치 로그
    log_debug("🎯 심볼별 가중치", "")
    for symbol, cfg in SYMBOL_CONFIG.items():
        tp_weight = cfg["tp_mult"]
        sl_weight = cfg.get("sl_mult", 1.0)
        symbol_name = symbol.replace("_USDT", "")
        log_debug(f"  └ {symbol_name}", f"TP: {tp_weight*100}%, SL: {sl_weight*100}%")
    
    # 전략 설정 로그
    log_debug("📈 기본 설정", "익절률: 0.5%, 손절률: 2%")
    log_debug("🔄 TP/SL 감소", "15초마다 TP -0.006%*가중치, SL -0.015%*가중치 (최소 TP 0.1%, SL 0.08%)")
    log_debug("📊 진입 전략", "최대 4회 진입, 단계별 수량: 20%→40%→120%→500%")
    log_debug("⚡ 신호 타입", "hybrid_enhanced(메인) / backup_enhanced(백업)")
    log_debug("🔒 조건 강화", "1차 추가: 1.3배, 2차 추가: 1.5배, 3차 추가: 1.6배")
    log_debug("🔄 트레이딩뷰", "파인스크립트 수량: 1%→2%→5%→10% (실제의 1/10)")
    log_debug("⏱️ 쿨다운", f"{COOLDOWN_SECONDS}초 (파인스크립트: 15초)")
    
    # 초기 자산 확인
    equity = get_total_collateral(force=True)
    log_debug("💰 초기 자산", f"{equity:.2f} USDT")
    
    # 초기 포지션 확인
    active_count = 0
    for symbol in SYMBOL_CONFIG:
        if update_position_state(symbol):
            pos = position_state.get(symbol, {})
            if pos.get("side"):
                active_count += 1
                entry_count = pos.get("entry_count", 0)
                tp_mult = SYMBOL_CONFIG[symbol]["tp_mult"]
                log_debug(f"📊 포지션 ({symbol})", 
                         f"{pos['side']} {pos['size']} @ {pos['price']} "
                         f"(진입 #{entry_count}, 가중치: {tp_mult})")
    
    if active_count == 0:
        log_debug("📊 포지션", "활성 포지션 없음")
    
    # 백그라운드 작업 시작
    log_debug("🔄 백그라운드 작업", "시작...")
    
    # 포지션 모니터링 스레드
    threading.Thread(
        target=position_monitor, 
        daemon=True, 
        name="PositionMonitor"
    ).start()
    
    # 시스템 모니터링 스레드
    threading.Thread(
        target=system_monitor, 
        daemon=True, 
        name="SystemMonitor"
    ).start()
    
    # 웹소켓 가격 모니터링 스레드
    threading.Thread(
        target=lambda: asyncio.run(price_monitor()), 
        daemon=True, 
        name="PriceMonitor"
    ).start()
    
    # Flask 실행
    port = int(os.environ.get("PORT", 8080))
    log_debug("🌐 웹서버", f"포트 {port}에서 실행")
    log_debug("✅ 준비 완료", "파인스크립트 v6.10 웹훅 대기중...")
    
    # Flask 서버 실행 (메인 스레드에서 실행)
    app.run(host="0.0.0.0", port=port, debug=False)
