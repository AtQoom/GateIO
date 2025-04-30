import os
import time
import hmac
import hashlib
import requests
import json
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, MARGIN_MODE, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT, API_KEY, API_SECRET
from utils import get_headers, get_open_position, place_order, close_position

app = Flask(__name__)

# ✅ UptimeRobot /ping 체크 대응용 GET 라우터
@app.route("/", methods=["GET"])
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "alive"}), 200


# ✅ 자동매매 웹훅 처리
@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data"}), 400

    position = data["position"]  # "long" or "short"
    print(f"📥 Received signal: {position.upper()}")

    # 진입 방향 결정
    side = "buy" if position == "long" else "sell"
    place_order(side)

    time.sleep(1.5)

    # 평단가 확인
    entry_price = get_open_position()
    if not entry_price:
        print("❌ Failed to get entry price.")
        return jsonify({"status": "error", "msg": "no position"}), 500

    print(f"✅ Entry price: {entry_price:.4f}")

    # 익절/손절 가격 설정
    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

    print(f"🎯 TP: {tp_price:.4f}, SL: {sl_price:.4f}")

    # 모니터링 루프
    while True:
        try:
            r = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
            current_price = float(r.json()["tickers"][0]["last"])
            print(f"📊 Current price: {current_price:.4f}")

            if position == "long":
                if current_price >= tp_price:
                    print(f"✅ TP HIT at {current_price:.4f}")
                    close_position("sell")
                    break
                elif current_price <= sl_price:
                    print(f"❌ SL HIT at {current_price:.4f}")
                    close_position("sell")
                    break
            else:
                if current_price <= tp_price:
                    print(f"✅ TP HIT at {current_price:.4f}")
                    close_position("buy")
                    break
                elif current_price >= sl_price:
                    print(f"❌ SL HIT at {current_price:.4f}")
                    close_position("buy")
                    break

            time.sleep(5)

        except Exception as e:
            print(f"⚠️ Error during monitoring loop: {str(e)}")
            time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
