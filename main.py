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
MARGIN_BUFFER = 0.8  # 증거금 안전 계수
ALLOCATION_RATIO = 0.33  # 각 코인당 33%

BINANCE_TO_GATE_SYMBOL = {
    "BTCUSDT": "BTC_USDT",
    "ADAUSDT": "ADA_USDT",
    "SUIUSDT": "SUI_USDT"
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": 10, "qty_step": 10, "sl_pct": 0.0075},
    "BTC_USDT": {"min_qty": 0.0001, "qty_step": 0.0001, "sl_pct": 0.004},
    "SUI_USDT": {"min_qty": 1, "qty_step": 1, "sl_pct": 0.0075}
}

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api_instance = FuturesApi(client)

position_state = {}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

def get_account_info():
    """실제 사용 가능한 증거금 조회 (레버리지 미적용)"""
    try:
        accounts = api_instance.list_futures_accounts(settle=SETTLE)
        available = float(getattr(accounts, 'available', 0))
        safe_available = available * MARGIN_BUFFER
        log_debug("💰 사용 가능 증거금", f"{safe_available:.2f} USDT (원본: {available:.2f})")
        return safe_available
    except Exception as e:
        log_debug("❌ 증거금 조회 실패", str(e))
        return 160 * MARGIN_BUFFER

def update_position_state(symbol):
    """포지션 상태 업데이트 (레버리지 포함)"""
    try:
        pos = api_instance.get_position(SETTLE, symbol)
        size = float(getattr(pos, 'size', 0))
        leverage = float(getattr(pos, 'leverage', 1))  # 앱에서 설정된 레버리지
        
        if size != 0:
            position_state[symbol] = {
                "price": float(getattr(pos, 'entry_price', 0)),
                "side": "buy" if size > 0 else "sell",
                "size": size,
                "leverage": leverage  # 레버리지 정보 저장
            }
            log_debug(f"📊 포지션 상태 ({symbol})", f"사이즈: {size}, 레버리지: {leverage}x")
        else:
            position_state[symbol] = {"price": None, "side": None, "size": 0, "leverage": 1}
    except Exception as e:
        log_debug(f"❌ 포지션 업데이트 실패 ({symbol})", str(e))

def get_max_qty(symbol):
    """레버리지 반영한 수량 계산"""
    try:
        config = SYMBOL_CONFIG[symbol]
        safe_available = get_account_info()
        current_price = get_current_price(symbol)
        update_position_state(symbol)
        state = position_state.get(symbol, {})
        
        # 앱에서 설정된 레버리지 가져오기 (기본값: 1)
        leverage = state.get("leverage", 1)
        
        # 레버리지 적용한 목표 증거금 계산
        target_margin = safe_available * ALLOCATION_RATIO
        target_value = target_margin * leverage  # 레버리지 적용
        
        # 현재 포지션 가치
        current_size = state.get("size", 0)
        current_value = abs(current_size) * current_price
        
        # 추가 필요 수량 계산
        if state.get("side") == "buy" and current_size > 0:
            additional_value = max(target_value - current_value, 0)
        elif state.get("side") == "sell" and current_size < 0:
            additional_value = max(target_value - abs(current_value), 0)
        else:
            additional_value = target_value
        
        additional_qty = additional_value / current_price
        
        # 최소 단위 내림 처리
        step = Decimal(str(config["qty_step"]))
        raw_qty_dec = Decimal(str(additional_qty))
        quantized = (raw_qty_dec / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
        qty = float(quantized)
        final_qty = max(qty, config["min_qty"])
        
        log_debug(f"📊 {symbol} 수량 계산", 
                 f"레버리지: {leverage}x, 목표가치: {target_value:.2f} USDT, "
                 f"추가수량: {final_qty}")
        return final_qty
    except Exception as e:
        log_debug(f"❌ {symbol} 수량 계산 실패", str(e))
        return SYMBOL_CONFIG[symbol]["min_qty"]

# 나머지 함수(close_position, get_current_price, place_order 등)는 이전 버전 유지
# WebSocket 리스너 및 웹훅 핸들러도 동일

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
