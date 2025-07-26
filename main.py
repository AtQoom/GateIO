import json
import logging
import time
import queue
import threading
import asyncio
import websockets
import pytz
import os
from decimal import Decimal, ROUND_DOWN
from threading import Lock, RLock
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder, UnifiedApi
from flask import Flask, request, jsonify
from datetime import datetime

# ========================================
# 설정 및 초기화
# ========================================

app = Flask(__name__)

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Gate.io API 설정
API_KEY = os.getenv('GATE_API_KEY', 'your_api_key_here')
API_SECRET = os.getenv('GATE_API_SECRET', 'your_api_secret_here')
SETTLE = 'usdt'

# API 클라이언트 초기화
configuration = Configuration(
    host="https://api.gateio.ws/api/v4",
    key=API_KEY,
    secret=API_SECRET
)
api = FuturesApi(ApiClient(configuration))
unified_api = UnifiedApi(ApiClient(configuration))

# 전역 변수
position_lock = Lock()
signal_lock = RLock()
tpsl_lock = RLock()
position_state = {}
pyramid_tracking = {}
recent_signals = {}
tpsl_storage = {}

# 큐 및 워커 설정
task_q = queue.Queue(maxsize=100)
WORKER_COUNT = min(6, max(2, os.cpu_count() * 2))

# 시간대 설정
KST = pytz.timezone('Asia/Seoul')

# ========================================
# 심볼 설정
# ========================================

SYMBOL_CONFIG = {
    "BTC_USDT": {
        "contract_size": Decimal("0.0001"), 
        "precision": 1, 
        "min_size": Decimal("0.0001"), 
        "weight": Decimal("0.5"),
        "tp_mult": Decimal("0.5"),
        "sl_mult": Decimal("0.5")
    },
    "ETH_USDT": {
        "contract_size": Decimal("0.001"), 
        "precision": 2, 
        "min_size": Decimal("0.001"), 
        "weight": Decimal("0.6"),
        "tp_mult": Decimal("0.6"),
        "sl_mult": Decimal("0.6")
    },
    "SOL_USDT": {
        "contract_size": Decimal("0.01"), 
        "precision": 3, 
        "min_size": Decimal("0.01"), 
        "weight": Decimal("0.8"),
        "tp_mult": Decimal("0.8"),
        "sl_mult": Decimal("0.8")
    },
    "PEPE_USDT": {
        "contract_size": Decimal("1"), 
        "precision": 8, 
        "min_size": Decimal("1"), 
        "weight": Decimal("1.2"),
        "tp_mult": Decimal("1.2"),
        "sl_mult": Decimal("1.2")
    },
    "DOGE_USDT": {
        "contract_size": Decimal("1"), 
        "precision": 5, 
        "min_size": Decimal("1"), 
        "weight": Decimal("1.2"),
        "tp_mult": Decimal("1.2"),
        "sl_mult": Decimal("1.2")
    }
}

# 심볼 매핑
SYMBOL_MAPPING = {
    "BTCUSDT": "BTC_USDT",
    "ETHUSDT": "ETH_USDT", 
    "SOLUSDT": "SOL_USDT",
    "PEPEUSDT": "PEPE_USDT",
    "DOGEUSDT": "DOGE_USDT",
    "BTC": "BTC_USDT",
    "ETH": "ETH_USDT",
    "SOL": "SOL_USDT", 
    "PEPE": "PEPE_USDT",
    "DOGE": "DOGE_USDT"
}

# ========================================
# 유틸리티 함수
# ========================================

def log_debug(title, message):
    """디버그 로그 출력"""
    logging.info(f"🔍 {title}: {message}")

def log_error(title, message):
    """에러 로그 출력"""
    logging.error(f"❌ {title}: {message}")

def log_success(title, message):
    """성공 로그 출력"""
    logging.info(f"✅ {title}: {message}")

def get_time_based_multiplier():
    """시간대별 배수 계산"""
    now = datetime.now(KST).hour
    # 야간 시간대(22시-9시)는 0.5배, 일반 시간대는 1.0배
    return Decimal("0.5") if now >= 22 or now < 9 else Decimal("1.0")

def normalize_symbol(symbol):
    """심볼 정규화"""
    symbol = symbol.upper().strip()
    if symbol in SYMBOL_MAPPING:
        return SYMBOL_MAPPING[symbol]
    if not symbol.endswith("_USDT"):
        symbol = symbol + "_USDT"
    return symbol if symbol in SYMBOL_CONFIG else None

def is_duplicate_signal(data):
    """중복 신호 체크"""
    with signal_lock:
        now = time.time()
        signal_id = data.get("id", "")
        key = f"{data['symbol']}_{data['side']}"
        
        # 동일 ID 5초 이내 재사용 금지
        if signal_id and signal_id in recent_signals:
            if now - recent_signals[signal_id]["time"] < 5:
                return True
        
        # 같은 symbol/side 12초 이내 금지
        if key in recent_signals:
            if now - recent_signals[key]["time"] < 10:  # 쿨다운 10초로 수정
                return True
        
        # 캐시 갱신
        recent_signals[key] = {"time": now}
        if signal_id:
            recent_signals[signal_id] = {"time": now}
        
        # 반대 포지션 제거 (롱→숏 교차 진입 허용)
        opposite_key = f"{data['symbol']}_{'short' if data['side'] == 'long' else 'long'}"
        recent_signals.pop(opposite_key, None)
        
        # 5분 이상 지난 키 삭제
        for k, v in list(recent_signals.items()):
            if now - v["time"] > 300:
                recent_signals.pop(k, None)
        
        return False

def get_account_equity():
    """계좌 잔고 조회"""
    try:
        account = api.list_futures_accounts(SETTLE)
        total_equity = Decimal(str(account.total))
        available = Decimal(str(account.available))
        log_debug("계좌 조회", f"총 자산: {total_equity} USDT, 사용가능: {available} USDT")
        return total_equity, available
    except Exception as e:
        log_error("계좌 조회", f"실패: {str(e)}")
        return Decimal("0"), Decimal("0")

def get_current_price(symbol):
    """현재 가격 조회"""
    try:
        ticker = api.list_futures_tickers(SETTLE, contract=symbol)
        if ticker:
            price = Decimal(str(ticker[0].last))
            log_debug("가격 조회", f"{symbol}: {price}")
            return price
        return None
    except Exception as e:
        log_error("가격 조회", f"{symbol} 실패: {str(e)}")
        return None

# ========================================
# TP/SL 저장소 관리
# ========================================

def store_tp_sl(symbol, tp, sl, entry_number):
    """TP/SL 저장"""
    with tpsl_lock:
        if symbol not in tpsl_storage:
            tpsl_storage[symbol] = {}
        tpsl_storage[symbol][entry_number] = {
            "tp": tp,
            "sl": sl,
            "time": time.time(),
            "entry_time": time.time()
        }
        log_debug("TP/SL 저장", f"{symbol} {entry_number}차: TP={tp}, SL={sl}")

def get_tp_sl(symbol, entry_number=None):
    """TP/SL 조회"""
    with tpsl_lock:
        if symbol in tpsl_storage and tpsl_storage[symbol]:
            if entry_number and entry_number in tpsl_storage[symbol]:
                data = tpsl_storage[symbol][entry_number]
                return data["tp"], data["sl"], data["entry_time"]
            
            # 최신 진입의 값 사용
            latest_entry = max(tpsl_storage[symbol].keys())
            data = tpsl_storage[symbol][latest_entry]
            return data["tp"], data["sl"], data["entry_time"]
    
    # 기본값 반환
    config = SYMBOL_CONFIG.get(symbol, {"tp_mult": Decimal("1.0"), "sl_mult": Decimal("1.0")})
    default_tp = Decimal("0.006") * config["tp_mult"]  # 기본 0.6% (수정됨)
    default_sl = Decimal("0.04") * config["sl_mult"]  # 기본 4.0% (동일)
    return default_tp, default_sl, time.time()

def clear_tp_sl(symbol):
    """심볼의 TP/SL 데이터 삭제"""
    with tpsl_lock:
        tpsl_storage.pop(symbol, None)
        log_debug("TP/SL 삭제", f"{symbol} 데이터 삭제됨")

# ========================================
# 포지션 관리
# ========================================

def update_position_state(symbol):
    """포지션 상태 업데이트"""
    with position_lock:
        try:
            pos = api.get_position(SETTLE, symbol)
            size = Decimal(str(pos.size))
            
            if size != 0:
                existing_count = position_state.get(symbol, {}).get("entry_count", 0)
                existing_time = position_state.get(symbol, {}).get("entry_time", time.time())
                existing_sl_count = position_state.get(symbol, {}).get("sl_entry_count", 0)
                
                position_state[symbol] = {
                    "price": Decimal(str(pos.entry_price)),
                    "side": "buy" if size > 0 else "sell",
                    "size": abs(size),
                    "value": abs(size) * Decimal(str(pos.mark_price)) * SYMBOL_CONFIG[symbol]["contract_size"],
                    "entry_count": existing_count,
                    "entry_time": existing_time,
                    "sl_entry_count": existing_sl_count,
                    "unrealized_pnl": Decimal(str(pos.unrealised_pnl)),
                    "mark_price": Decimal(str(pos.mark_price))
                }
                log_debug("포지션 업데이트", f"{symbol}: {position_state[symbol]}")
                return False
            else:
                position_state[symbol] = {
                    "price": None, 
                    "side": None, 
                    "size": Decimal("0"), 
                    "value": Decimal("0"),
                    "entry_count": 0,
                    "entry_time": None,
                    "sl_entry_count": 0,
                    "unrealized_pnl": Decimal("0"),
                    "mark_price": None
                }
                pyramid_tracking.pop(symbol, None)
                clear_tp_sl(symbol)
                log_debug("포지션 초기화", f"{symbol}: 포지션 없음")
                return True
        except Exception as e:
            log_error("포지션 상태 업데이트", f"{symbol}: {str(e)}")
            return False

def close_position(symbol, reason="manual"):
    """포지션 청산 (손절직전 카운터 리셋 포함)"""
    with position_lock:
        try:
            api.create_futures_order(
                SETTLE, 
                FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
            )
            
            log_success("청산 완료", f"{symbol} - 이유: {reason}")
            
            # sl_entry_count 리셋
            if symbol in position_state:
                position_state[symbol]["entry_count"] = 0
                position_state[symbol]["entry_time"] = None
                position_state[symbol]["sl_entry_count"] = 0
            
            pyramid_tracking.pop(symbol, None)
            clear_tp_sl(symbol)
            
            # 포지션 상태 업데이트
            update_position_state(symbol)
            
            return True
        except Exception as e:
            log_error("청산 실패", f"{symbol}: {str(e)}")
            return False

# ========================================
# 손절직전 진입 로직
# ========================================

def is_sl_rescue_condition(symbol, signal_data):
    """손절직전 진입 조건 체크"""
    try:
        if symbol not in position_state or position_state[symbol]["size"] == 0:
            return False
            
        current_entry_count = position_state[symbol]["entry_count"]
        sl_entry_count = position_state[symbol].get("sl_entry_count", 0)
        
        # 최대 진입 수 체크
        if current_entry_count >= 5 or sl_entry_count >= 3:
            return False
        
        # 현재 가격과 평균가, 손절가 계산
        current_price = get_current_price(symbol)
        if not current_price:
            return False
            
        avg_price = position_state[symbol]["price"]
        side = position_state[symbol]["side"]
        
        # TP/SL 계산 (신호에서 온 값 사용)
        tp_pct = Decimal(str(signal_data.get("tp_pct", 0.6))) / 100
        sl_pct = Decimal(str(signal_data.get("sl_pct", 4.0))) / 100
        
        # 손절가 계산
        if side == "buy":
            sl_price = avg_price * (1 - sl_pct)
        else:
            sl_price = avg_price * (1 + sl_pct)
        
        # 손절가 근접도 체크 (0.05% 범위)
        sl_proximity_threshold = Decimal("0.0005")
        
        is_near_sl = False
        if side == "buy":
            if current_price > sl_price and current_price < sl_price * (1 + sl_proximity_threshold):
                is_near_sl = True
        else:
            if current_price < sl_price and current_price > sl_price * (1 - sl_proximity_threshold):
                is_near_sl = True
        
        # 평균가 대비 불리한 상황 체크
        is_underwater = False
        if side == "buy" and current_price < avg_price:
            is_underwater = True
        elif side == "sell" and current_price > avg_price:
            is_underwater = True
        
        result = is_near_sl and is_underwater
        
        if result:
            log_debug("손절직전 조건", f"{symbol}: 손절가={sl_price}, 현재가={current_price}, 근접={is_near_sl}, 불리={is_underwater}")
        
        return result
        
    except Exception as e:
        log_error("손절직전 조건 체크", f"{symbol}: {str(e)}")
        return False

# ========================================
# 수량 계산
# ========================================

def calculate_position_size(symbol, signal_data, equity):
    """포지션 수량 계산 (5단계 피라미딩 + 손절직전 가중치)"""
    try:
        price = Decimal(str(signal_data["price"]))
        if equity <= 0 or price <= 0:
            return Decimal("0")
        
        current_entry_count = position_state.get(symbol, {}).get("entry_count", 0)
        next_entry = current_entry_count + 1
        
        # 5단계 진입 비율
        entry_ratios = [Decimal("0.20"), Decimal("0.40"), Decimal("1.2"), Decimal("4.8"), Decimal("9.6")]
        
        if next_entry > len(entry_ratios):
            log_debug("수량 계산", f"{symbol}: 최대 진입 수 초과 ({next_entry})")
            return Decimal("0")
        
        # 기본 수량 계산
        ratio = entry_ratios[next_entry - 1]
        time_mult = get_time_based_multiplier()
        symbol_weight = SYMBOL_CONFIG[symbol]["weight"]
        
        base_qty = equity * ratio / 100 / price * time_mult * symbol_weight
        
        # 손절직전 진입일 경우 50% 추가 (총 150%)
        is_sl_rescue = is_sl_rescue_condition(symbol, signal_data)
        if is_sl_rescue:
            final_qty = base_qty * Decimal("1.5")  # 150% 적용
            log_debug("손절직전 수량", f"{symbol}: 기본 {base_qty} → 최종 {final_qty} (150% 적용)")
        else:
            final_qty = base_qty
        
        # 최소 단위로 반올림
        min_size = SYMBOL_CONFIG[symbol]["min_size"]
        final_qty = (final_qty / min_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * min_size
        
        log_debug("수량 계산", f"{symbol}: {next_entry}차 진입, 비율={ratio}%, 수량={final_qty}")
        return final_qty
        
    except Exception as e:
        log_error("수량 계산", f"{symbol}: {str(e)}")
        return Decimal("0")

# ========================================
# 메인 진입 처리 함수
# ========================================

def handle_entry(signal_data):
    """진입 신호 처리"""
    try:
        symbol = signal_data["symbol"] + "_USDT"
        side = signal_data["side"]
        action = signal_data["action"]
        entry_type = signal_data.get("type", "Simple_Long")
        
        log_debug("진입 신호", f"{symbol}: {side} {action} ({entry_type})")
        
        # 심볼 설정 확인
        if symbol not in SYMBOL_CONFIG:
            log_error("진입 실패", f"지원하지 않는 심볼: {symbol}")
            return {"success": False, "message": "지원하지 않는 심볼"}
        
        # 포지션 상태 업데이트
        update_position_state(symbol)
        
        # 계좌 잔고 조회
        equity, available = get_account_equity()
        if equity <= 0:
            log_error("진입 실패", "계좌 잔고 부족")
            return {"success": False, "message": "계좌 잔고 부족"}
        
        # 수량 계산
        size = calculate_position_size(symbol, signal_data, equity)
        if size <= 0:
            log_error("진입 실패", f"{symbol}: 계산된 수량이 0")
            return {"success": False, "message": "수량 계산 실패"}
        
        # 주문 방향 설정
        order_size = size if side == "long" else -size
        
        # 주문 실행
        order = FuturesOrder(
            contract=symbol,
            size=str(order_size),
            price="0",  # 시장가 주문
            tif="ioc"
        )
        
        result = api.create_futures_order(SETTLE, order)
        
        # 성공 처리
        current_entry_count = position_state.get(symbol, {}).get("entry_count", 0)
        
        # 손절직전 진입인지 확인
        is_sl_rescue = is_sl_rescue_condition(symbol, signal_data)
        
        # 진입 카운터 업데이트
        if symbol not in position_state:
            position_state[symbol] = {
                "price": None, "side": None, "size": Decimal("0"), "value": Decimal("0"),
                "entry_count": 0, "entry_time": None, "sl_entry_count": 0
            }
        
        position_state[symbol]["entry_count"] = current_entry_count + 1
        if current_entry_count == 0:  # 첫 진입
            position_state[symbol]["entry_time"] = time.time()
        
        # 손절직전 진입 카운터 업데이트
        if is_sl_rescue:
            position_state[symbol]["sl_entry_count"] = position_state[symbol].get("sl_entry_count", 0) + 1
        
        # TP/SL 저장
        tp_pct = Decimal(str(signal_data.get("tp_pct", 0.6))) / 100
        sl_pct = Decimal(str(signal_data.get("sl_pct", 4.0))) / 100
        entry_number = position_state[symbol]["entry_count"]
        
        store_tp_sl(symbol, tp_pct, sl_pct, entry_number)
        
        # 포지션 상태 업데이트
        update_position_state(symbol)
        
        log_success("진입 완료", f"{symbol}: {entry_type}, 수량={size}, 주문ID={result.id}")
        
        return {
            "success": True, 
            "message": "진입 완료",
            "order_id": result.id,
            "size": str(size),
            "entry_count": position_state[symbol]["entry_count"],
            "sl_entry_count": position_state[symbol].get("sl_entry_count", 0),
            "is_sl_rescue": is_sl_rescue
        }
        
    except Exception as e:
        log_error("진입 처리", f"실패: {str(e)}")
        return {"success": False, "message": str(e)}

def handle_exit(signal_data):
    """청산 신호 처리"""
    try:
        symbol = signal_data["symbol"] + "_USDT"
        reason = signal_data.get("reason", "manual")
        
        log_debug("청산 신호", f"{symbol}: {reason}")
        
        # 포지션 확인
        if symbol not in position_state or position_state[symbol]["size"] == 0:
            return {"success": False, "message": "청산할 포지션이 없음"}
        
        # 청산 실행
        success = close_position(symbol, reason)
        
        if success:
            return {"success": True, "message": "청산 완료"}
        else:
            return {"success": False, "message": "청산 실패"}
            
    except Exception as e:
        log_error("청산 처리", f"실패: {str(e)}")
        return {"success": False, "message": str(e)}

# ========================================
# 워커 스레드
# ========================================

def worker(worker_id):
    """작업 큐 처리 워커"""
    log_debug("워커 시작", f"워커 {worker_id} 시작됨")
    
    while True:
        try:
            # 큐에서 작업 가져오기 (1초 타임아웃)
            data = task_q.get(timeout=1)
            
            try:
                # 진입 처리
                result = handle_entry(data)
                log_debug("워커 처리", f"워커 {worker_id}: {result}")
            except Exception as e:
                log_error("워커 오류", f"워커 {worker_id}: {str(e)}")
            finally:
                task_q.task_done()
                
        except queue.Empty:
            # 타임아웃 - 계속 대기
            continue
        except Exception as e:
            log_error("워커 심각한 오류", f"워커 {worker_id}: {str(e)}")
            time.sleep(1)

# 워커 스레드 시작
for i in range(WORKER_COUNT):
    worker_thread = threading.Thread(target=worker, args=(i,), daemon=True)
    worker_thread.start()
    log_debug("워커 생성", f"워커 {i} 생성됨")

# ========================================
# Flask 라우트
# ========================================

@app.route('/webhook', methods=['POST'])
def webhook():
    """TradingView 웹훅 처리"""
    try:
        data = request.get_json()
        log_debug("웹훅 수신", f"데이터: {data}")
        
        if not data:
            return jsonify({"success": False, "message": "데이터 없음"}), 400
        
        action = data.get("action")
        
        if action == "entry":
            result = handle_entry(data)
        elif action == "exit":
            result = handle_exit(data)
        else:
            return jsonify({"success": False, "message": "알 수 없는 액션"}), 400
        
        return jsonify(result)
        
    except Exception as e:
        log_error("웹훅 처리", f"실패: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/status', methods=['GET'])
def status():
    """서버 상태 및 포지션 조회"""
    try:
        # 모든 포지션 업데이트
        for symbol in SYMBOL_CONFIG:
            update_position_state(symbol)
        
        equity, available = get_account_equity()
        
        return jsonify({
            "success": True,
            "equity": str(equity),
            "available": str(available),
            "positions": {k: {
                "price": str(v["price"]) if v["price"] else None,
                "side": v["side"],
                "size": str(v["size"]),
                "value": str(v["value"]),
                "entry_count": v["entry_count"],
                "sl_entry_count": v.get("sl_entry_count", 0),
                "entry_time": v["entry_time"]
            } for k, v in position_state.items()}
        })
        
    except Exception as e:
        log_error("상태 조회", f"실패: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/close/<symbol>', methods=['POST'])
def manual_close(symbol):
    """수동 청산"""
    try:
        full_symbol = symbol + "_USDT"
        if full_symbol not in SYMBOL_CONFIG:
            return jsonify({"success": False, "message": "지원하지 않는 심볼"}), 400
        
        success = close_position(full_symbol, "manual")
        
        if success:
            return jsonify({"success": True, "message": "수동 청산 완료"})
        else:
            return jsonify({"success": False, "message": "청산 실패"}), 500
            
    except Exception as e:
        log_error("수동 청산", f"실패: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    """캐시 및 상태 초기화"""
    try:
        with signal_lock:
            recent_signals.clear()
        with tpsl_lock:
            tpsl_storage.clear()
        
        pyramid_tracking.clear()
        
        log_success("캐시 삭제", "모든 캐시가 초기화됨")
        return jsonify({"status": "cache_cleared", "timestamp": datetime.now(KST).isoformat()})
        
    except Exception as e:
        log_error("캐시 삭제", f"실패: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/queue-status', methods=['GET'])
def queue_status():
    """큐 상태 조회"""
    return jsonify({
        "queue_size": task_q.qsize(),
        "max_queue_size": task_q.maxsize,
        "worker_count": WORKER_COUNT,
        "utilization": f"{task_q.qsize() / task_q.maxsize * 100:.1f}%",
        "timestamp": datetime.now(KST).isoformat()
    })

@app.route('/tpsl/<symbol>', methods=['GET'])
def get_tpsl_info(symbol):
    """심볼별 TP/SL 정보 조회"""
    try:
        normalized_symbol = normalize_symbol(symbol)
        if not normalized_symbol:
            return jsonify({"success": False, "message": "지원하지 않는 심볼"}), 400
        
        with tpsl_lock:
            data = tpsl_storage.get(normalized_symbol, {})
        
        return jsonify({
            "success": True,
            "symbol": normalized_symbol,
            "tpsl_data": {k: {
                "tp": str(v["tp"]),
                "sl": str(v["sl"]),
                "time": v["time"],
                "entry_time": v["entry_time"]
            } for k, v in data.items()},
            "timestamp": datetime.now(KST).isoformat()
        })
        
    except Exception as e:
        log_error("TP/SL 조회", f"실패: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """헬스체크"""
    return jsonify({
        "success": True, 
        "message": "서버 정상 동작",
        "timestamp": datetime.now(KST).isoformat(),
        "queue_size": task_q.qsize(),
        "worker_count": WORKER_COUNT
    })

# ========================================
# 메인 실행
# ========================================

if __name__ == '__main__':
    # 시작 시 모든 포지션 상태 초기화
    log_success("서버 시작", "포지션 상태 초기화 중...")
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
    
    log_success("서버 준비", "웹훅 서버가 시작되었습니다.")
    app.run(host='0.0.0.0', port=5000, debug=False)
