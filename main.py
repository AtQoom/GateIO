import os
import json
import time
import asyncio
import threading
import websockets
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

# ✅ Binance → Gate.io 심볼 변환 테이블
BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

# ✅ 코인별 설정
SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": 10, "qty_step": 10, "sl_pct": 0.0075},
    "BTC_USDT": {"min_qty": 0.0001, "qty_step": 0.0001, "sl_pct": 0.004},
    "SUI_USDT": {"min_qty": 1, "qty_step": 1, "sl_pct": 0.0075}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

# 현재 포지션 상태 저장
position_state = {}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_equity():
    """계정 잔고 조회 - API 문서에 맞게 수정"""
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        # 'available' 대신 'total'과 'unrealised_pnl' 사용
        total_equity = float(accounts.total) 
        unrealised_pnl = float(accounts.unrealised_pnl or 0)
        total = total_equity + unrealised_pnl  # 실현되지 않은 손익 포함
        
        log_debug("💰 계정 잔액", f"총액: {total_equity} USDT, 미실현 손익: {unrealised_pnl} USDT, 총 가용: {total} USDT")
        return max(total, 0)  # 음수 방지
    except Exception as e:
        log_debug("❌ 잔고 조회 실패", str(e))
        return 1000  # 기본값

def get_positions():
    """모든 포지션 정보 조회 - 올바른 API 메소드 사용"""
    positions = []
    for symbol in SYMBOL_CONFIG.keys():
        try:
            pos = api_instance.get_position(SETTLE, symbol)
            if pos and hasattr(pos, 'size') and float(pos.size) != 0:
                positions.append(pos)
        except Exception as e:
            log_debug(f"⚠️ {symbol} 포지션 조회 실패", str(e))
    return positions

def get_available_equity():
    """사용 가능한 자금 계산 - 간소화된 로직"""
    try:
        total_equity = get_equity()
        # 최소 10% 예비금 확보 (안전장치)
        per_symbol = (total_equity * 0.9) / len(SYMBOL_CONFIG)
        log_debug("💵 자금 분배", f"총액: {total_equity}, 심볼당: {per_symbol}")
        return per_symbol
    except Exception as e:
        log_debug("❌ 사용 가능 증거금 계산 실패", str(e))
        return 100  # 기본값

def update_position_state(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size) if hasattr(pos, 'size') else 0
        if size != 0:
            position_state[symbol] = {
                "price": float(pos.entry_price) if hasattr(pos, 'entry_price') else 0,
                "side": "buy" if size > 0 else "sell"
            }
            log_debug(f"📊 포지션 상태 ({symbol})", f"사이즈: {size}, 진입가: {position_state[symbol]['price']}, 방향: {position_state[symbol]['side']}")
        else:
            position_state[symbol] = {"price": None, "side": None}
            log_debug(f"📊 포지션 상태 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))
        position_state[symbol] = {"price": None, "side": None}

def close_position(symbol):
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(pos.size) if hasattr(pos, 'size') else 0
        if size == 0:
            log_debug(f"📭 포지션 없음 ({symbol})", "청산할 포지션이 없습니다.")
            return
            
        # 청산 주문
        order = FuturesOrder(contract=symbol, size=0, price="0", tif="ioc", close=True)
        result = api_instance.create_futures_order(SETTLE, order)
        log_debug(f"✅ 전체 청산 성공 ({symbol})", result.to_dict())
        
        # 청산 확인
        for _ in range(10):
            try:
                pos = api_instance.get_position(SETTLE, symbol)
                if float(pos.size) == 0:
                    break
                time.sleep(0.5)
            except:
                break
                
        position_state[symbol] = {"price": None, "side": None}
    except Exception as e:
        log_debug(f"❌ 전체 청산 실패 ({symbol})", str(e))

def get_current_price(symbol):
    """현재 가격 조회 - 여러 API 시도"""
    try:
        # 방법 1: 티커 조회
        tickers = api_instance.list_futures_tickers(settle=SETTLE, contract=symbol)
        if tickers and len(tickers) > 0:
            return float(tickers[0].last)
            
        # 방법 2: 최근 거래 조회
        trades = api_instance.list_futures_trades(settle=SETTLE, contract=symbol)
        if trades and len(trades) > 0:
            return float(trades[0].price)
    except Exception as e:
        log_debug(f"⚠️ {symbol} 가격 조회 실패", str(e))
    return 0

def get_max_qty(symbol):
    """주문 수량 계산 - 심볼별 마크 가격 정확히 조회"""
    try:
        config = SYMBOL_CONFIG[symbol]
        available = get_available_equity()
        
        # 포지션 정보 조회
        pos = api_instance.get_position(SETTLE, symbol)
        leverage = float(pos.leverage) if hasattr(pos, 'leverage') and pos.leverage else 5.0  # 기본 레버리지
        
        # 가격 조회 여러 방법 시도
        mark_price = 0
        if hasattr(pos, 'mark_price') and pos.mark_price:
            mark_price = float(pos.mark_price)
        
        # 가격이 0이면 다른 방법 시도
        if mark_price <= 0:
            mark_price = get_current_price(symbol)
            
        # 가격이 여전히 0이면 기본값 사용
        if mark_price <= 0:
            log_debug(f"⚠️ {symbol} 가격 0", f"심볼 최소 수량 사용")
            return config["min_qty"]
        
        # 수량 계산
        raw_qty = (available * leverage) / mark_price
        step = Decimal(str(config["qty_step"]))
        raw_qty_dec = Decimal(str(raw_qty))
        
        # 내림 처리
        quantized = (raw_qty_dec / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
        qty = float(quantized)
        
        log_debug(f"📊 {symbol} 수량 계산", 
                 f"자금: {available:.2f} USDT, 레버리지: {leverage}, 마크가격: {mark_price}, "
                 f"원시수량: {raw_qty}, 조정수량: {qty}")
                 
        return max(qty, config["min_qty"])
    except Exception as e:
        log_debug(f"❌ {symbol} 수량 계산 실패", str(e))
        return config["min_qty"]

def place_order(symbol, side, qty, reduce_only=False):
    try:
        size = qty if side == "buy" else -qty
        
        # 현재 레버리지 확인 (너무 낮으면 경고)
        try:
            pos = api_instance.get_position(SETTLE, symbol)
            leverage = float(pos.leverage) if hasattr(pos, 'leverage') else 1
            if leverage < 2:
                log_debug(f"⚠️ 낮은 레버리지 ({symbol})", f"현재 {leverage}배, 수익성 저하 가능")
        except:
            pass
            
        # 주문 실행
        order = FuturesOrder(contract=symbol, size=size, price="0", tif="ioc", reduce_only=reduce_only)
        result = api_instance.create_futures_order(SETTLE, order)
        
        # 성공 로그
        log_debug(f"✅ 주문 성공 ({symbol})", 
                 f"수량: {size}, 체결가: {result.fill_price if hasattr(result, 'fill_price') else '알 수 없음'}")
        
        # 포지션 상태 업데이트
        if not reduce_only:
            fill_price = float(result.fill_price) if hasattr(result, 'fill_price') and result.fill_price else 0
            position_state[symbol] = {
                "price": fill_price,
                "side": side
            }
    except Exception as e:
        log_debug(f"❌ 주문 실패 ({symbol})", str(e))

async def price_listener():
    uri = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    contracts = list(SYMBOL_CONFIG.keys())
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                # 구독 요청
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.tickers",
                    "event": "subscribe",
                    "payload": contracts
                }))
                
                log_debug("📡 WebSocket 연결 성공", f"구독 중: {contracts}")
                
                # 핑 타이머 설정
                last_ping_time = time.time()
                last_msg_time = time.time()
                
                while True:
                    try:
                        # 30초마다 핑 전송
                        current_time = time.time()
                        if current_time - last_ping_time > 30:
                            await ws.send(json.dumps({
                                "time": int(current_time),
                                "channel": "futures.ping",
                                "event": "ping",
                                "payload": []
                            }))
                            last_ping_time = current_time
                            
                        # 5분 이상 메시지 없으면 재연결
                        if current_time - last_msg_time > 300:
                            log_debug("⚠️ 오랜 시간 메시지 없음", "연결 재설정")
                            break
                            
                        # 메시지 수신
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        last_msg_time = time.time()
                        
                        # JSON 파싱
                        data = json.loads(msg)
                        
                        # 핑/퐁 처리
                        if 'event' in data and data['event'] in ['ping', 'pong']:
                            continue
                            
                        # 구독 확인
                        if 'event' in data and data['event'] == 'subscribe':
                            log_debug("✅ WebSocket 구독완료", f"{data.get('channel')} - {data.get('payload')}")
                            continue
                            
                        # 결과 없으면 스킵
                        if "result" not in data or not isinstance(data["result"], dict):
                            continue
                            
                        # 심볼 추출
                        symbol = data["result"].get("contract")
                        if not symbol or symbol not in SYMBOL_CONFIG:
                            continue
                            
                        # 가격 추출
                        price = float(data["result"].get("last", 0))
                        if price <= 0:
                            continue
                            
                        # 포지션 상태 업데이트 (WebSocket에서는 가벼운 확인만)
                        state = position_state.get(symbol, {})
                        entry_price = state.get("price")
                        entry_side = state.get("side")
                        
                        # 포지션 없으면 스킵
                        if not entry_price or not entry_side:
                            continue
                            
                        # 손절 계산
                        sl_pct = SYMBOL_CONFIG[symbol]["sl_pct"]
                        sl_hit = (
                            (entry_side == "buy" and price <= entry_price * (1 - sl_pct)) or
                            (entry_side == "sell" and price >= entry_price * (1 + sl_pct))
                        )
                        
                        # 손절 실행
                        if sl_hit:
                            log_debug(f"🛑 손절 조건 충족 ({symbol})", f"현재가: {price}, 진입가: {entry_price}, 손절폭: {sl_pct*100}%")
                            close_position(symbol)
                            
                    except asyncio.TimeoutError:
                        # 타임아웃은 정상적인 상황
                        continue
                    except Exception as e:
                        log_debug("❌ WebSocket 메시지 처리 오류", str(e))
                        continue
        
        except Exception as e:
            log_debug("❌ WebSocket 연결 오류", str(e))
            await asyncio.sleep(5)

def start_price_listener():
    # 서버 시작 시 포지션 상태 초기화
    for symbol in SYMBOL_CONFIG:
        update_position_state(symbol)
    
    # WebSocket 리스너 시작
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(price_listener())

@app.route("/", methods=["POST"])
def webhook():
    try:
        # JSON 검증
        if not request.is_json:
            log_debug("⚠️ 잘못된 요청", "JSON 형식 아님")
            return jsonify({"error": "JSON 형식의 요청만 허용됩니다"}), 400
            
        # 데이터 파싱
        data = request.get_json()
        signal = data.get("side", "").lower()
        action = data.get("action", "").lower()
        symbol_raw = data.get("symbol", "").upper().replace(".P", "").replace(".F", "")
        
        # 심볼 변환
        symbol = BINANCE_TO_GATE_SYMBOL.get(symbol_raw, symbol_raw)
        log_debug("📥 웹훅 수신", f"심볼: {symbol}, 신호: {signal}, 액션: {action}")

        # 유효성 검증
        if symbol not in SYMBOL_CONFIG:
            log_debug("⚠️ 알 수 없는 심볼", symbol)
            return jsonify({"error": f"지원하지 않는 심볼: {symbol}"}), 400
            
        if signal not in ["long", "short", "buy", "sell"]:
            log_debug("⚠️ 잘못된 신호", signal)
            return jsonify({"error": f"유효하지 않은 신호: {signal}"}), 400
            
        if action not in ["entry", "exit"]:
            log_debug("⚠️ 잘못된 액션", action)
            return jsonify({"error": f"유효하지 않은 액션: {action}"}), 400
            
        # buy/sell → long/short 변환
        if signal == "buy":
            signal = "long"
        elif signal == "sell":
            signal = "short"

        # 포지션 정보 업데이트
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        
        # 청산 액션 처리
        if action == "exit":
            close_position(symbol)
            return jsonify({"status": "청산 완료", "symbol": symbol})

        # 진입 액션 처리 (side 결정)
        side = "buy" if signal == "long" else "sell"
        entry_side = state.get("side")

        # 반대 포지션 감지 시 청산
        if entry_side and ((side == "buy" and entry_side == "sell") or (side == "sell" and entry_side == "buy")):
            log_debug(f"🔁 반대 포지션 감지 ({symbol})", f"{entry_side} → {side}로 전환")
            close_position(symbol)
            time.sleep(0.5)

        # 수량 계산 후 주문
        qty = get_max_qty(symbol)
        place_order(symbol, side, qty)
        
        # 응답
        return jsonify({
            "status": "진입 완료", 
            "symbol": symbol, 
            "side": side,
            "qty": qty
        })
        
    except Exception as e:
        log_debug("❌ 웹훅 처리 실패", str(e))
        return jsonify({"error": "서버 내부 오류"}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    """포지션/계정 상태 확인 엔드포인트"""
    try:
        equity = get_equity()
        
        # 각 심볼 포지션 업데이트
        for symbol in SYMBOL_CONFIG.keys():
            update_position_state(symbol)
            
        return jsonify({
            "status": "running",
            "equity": equity,
            "positions": position_state
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "WebSocket 리스너 실행됨")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
