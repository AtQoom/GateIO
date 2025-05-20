import os
import json
import time
import hmac
import hashlib
import threading
from decimal import Decimal
from datetime import datetime
from flask import Flask, jsonify
import requests
from gate_api import ApiClient, Configuration, FuturesApi

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
SETTLE = "usdt"

SYMBOL_LEVERAGE = {
    "BTC_USDT": Decimal("10"),
    "ADA_USDT": Decimal("10"),
    "SUI_USDT": Decimal("10"),
}

SYMBOL_CONFIG = {
    "ADA_USDT": {"min_qty": Decimal("10"), "qty_step": Decimal("10"), "sl_pct": Decimal("0.0075"), "min_order_usdt": Decimal("5")},
    "BTC_USDT": {"min_qty": Decimal("0.0001"), "qty_step": Decimal("0.0001"), "sl_pct": Decimal("0.004"), "min_order_usdt": Decimal("5")},
    "SUI_USDT": {"min_qty": Decimal("1"), "qty_step": Decimal("1"), "sl_pct": Decimal("0.0075"), "min_order_usdt": Decimal("5")}
}

API_URL_BASE = "https://api.gateio.ws"
API_PREFIX = "/api/v4"

config = Configuration(key=API_KEY, secret=API_SECRET)
client = ApiClient(config)
api = FuturesApi(client)

position_state = {}

def log_debug(title, content):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {content}")

# Gate.io 공식 샘플 방식과 같이 json.dumps의 옵션(separators) 엄격 적용
def sign(payload, secret):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()

def set_leverage(contract, leverage, mode="cross"):
    try:
        url_path = f"/api/v4/futures/{SETTLE}/positions/{contract}/leverage"
        url = f"{API_URL_BASE}{url_path}"
        body = {"leverage": str(leverage), "mode": mode}
        body_str = json.dumps(body, separators=(',', ':'))  # ←★ 공백 없이
        t = str(int(time.time()))
        to_sign = t + 'POST' + url_path + body_str
        sign_header = sign(to_sign, API_SECRET)
        headers = {
            "KEY": API_KEY,
            "Timestamp": t,
            "SIGN": sign_header,
            "Content-Type": "application/json"
        }
        resp = requests.post(url, headers=headers, data=body_str)
        if resp.status_code in (200, 201):
            log_debug(f"✅ 레버리지 설정 성공 ({contract})", f"{leverage}x ({mode})")
        else:
            log_debug(f"❌ 레버리지 설정 실패 ({contract})", f"status:{resp.status_code}, {resp.text}")
    except Exception as e:
        log_debug(f"❌ REST 레버리지 설정 중 에러 ({contract})", str(e))

def update_position_state(symbol):
    try:
        pos = api.get_position(SETTLE, symbol)
        size = Decimal(str(getattr(pos, "size", "0")))
        if size != 0:
            entry_price = Decimal(str(getattr(pos, "entry_price", "0")))
            leverage = Decimal(str(getattr(pos, "leverage", "1")))
            mark_price = Decimal(str(getattr(pos, "mark_price", "0")))
            position_value = abs(size) * mark_price
            margin = position_value / leverage
            position_state[symbol] = {
                "price": entry_price,
                "side": "buy" if size > 0 else "sell",
                "leverage": leverage,
                "size": abs(size),
                "value": position_value,
                "margin": margin
            }
            log_debug(f"📊 포지션 ({symbol})", f"수량: {abs(size)}, 진입가: {entry_price}, 방향: {'롱' if size > 0 else '숏'}, "
                      f"레버리지: {leverage}x, 포지션가치: {position_value}, 증거금: {margin}")
        else:
            position_state[symbol] = {
                "price": None, "side": None, "leverage": Decimal("1"),
                "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
            }
            log_debug(f"📊 포지션 ({symbol})", "포지션 없음")
    except Exception as e:
        log_debug(f"❌ 포지션 조회 실패 ({symbol})", str(e))
        position_state[symbol] = {
            "price": None, "side": None, "leverage": Decimal("1"),
            "size": Decimal("0"), "value": Decimal("0"), "margin": Decimal("0")
        }

def start_price_listener():
    for sym, lev in SYMBOL_LEVERAGE.items():
        set_leverage(sym, lev)
    for sym in SYMBOL_CONFIG:
        update_position_state(sym)
    log_debug("🚀 초기화", "레버리지/포지션 정보 초기 세팅 완료")

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/status", methods=["GET"])
def status():
    try:
        equity = Decimal("0")  # 실제 자산 조회는 별도로 구현 필요
        positions = {}
        for symbol in SYMBOL_CONFIG:
            update_position_state(symbol)
            positions[symbol] = position_state.get(symbol, {})
        return jsonify({
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "equity": float(equity),
            "positions": {k: {sk: (float(sv) if isinstance(sv, Decimal) else sv) 
                              for sk, sv in v.items()} 
                          for k, v in positions.items()}
        })
    except Exception as e:
        log_debug("❌ 상태 조회 실패", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=start_price_listener, daemon=True).start()
    log_debug("🚀 서버 시작", "초기화 스레드 실행됨, 웹서버 가동")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
