import os
import time
import requests
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position

app = Flask(__name__)

# 상태 확인용 루트
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "message": "GateIO bot running"})

# favicon 요청 무시
@app.route("/favicon.ico")
def favicon():
    return "", 204

# TradingView 알림 처리
@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data"}), 400

    position = data["position"]  # "long" or "short"
    print(f"📥 Received signal: {position.upper()}")

    # 진입
    side = "buy" if position == "long" else "sell"
    place_order(side)
    time.sleep(1.5)

    # 평단가 확인
    entry_price = get_open_position()
    if not entry_price:
        print("❌ Failed to retrieve entry price.")
        return jsonify({"status": "error", "msg": "no position"}), 500

    print(f"✅ Entry price: {entry_price:.4f}")

    # TP / SL 계산
    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

    print(f"🎯 TP: {tp_price:.4f}, SL: {sl_price:.4f}")

    # 실시간 모니터링
    while True:
        try:
            res = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
            current_price = float(res.json()["tickers"][0]["last"])
            print(f"📈 Current price: {current_price}")

            if position == "long":
                if current_price >= tp_price:
                    print("✅ Take Profit hit!")
                    close_position("sell")
                    break
                elif current_price <= sl_price:
                    print("❌ Stop Loss hit!")
                    close_position("sell")
                    break
            else:  # short
                if current_price <= tp_price:
                    print("✅ Take Profit hit!")
                    close_position("buy")
                    break
                elif current_price >= sl_price:
                    print("❌ Stop Loss hit!")
                    close_position("buy")
                    break

            time.sleep(5)

        except Exception as e:
            print(f"⚠️ Error in monitoring loop: {e}")
            time.sleep(5)

    return jsonify({"status": "closed"}), 200

# 앱 실행
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
