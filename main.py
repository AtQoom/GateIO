import os
import time
import hmac
import hashlib
import requests
import json
from config import BASE_URL, SYMBOL, POSITION_SIZE, TP_RATIO, SL_RATIO
from utils import get_headers, get_open_position, place_order, close_position

from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    data = request.json
    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data"}), 400

    position = data["position"]  # "long" or "short"
    print(f"📥 Received signal: {position.upper()}")

    # 진입 주문 실행
    side = "buy" if position == "long" else "sell"
    place_order(side)

    time.sleep(1.5)  # 약간 대기 후 포지션 확인

    # 진입 후 평단가 확인
    entry_price = get_open_position()
    if not entry_price:
        print("❌ Failed to retrieve entry price.")
        return jsonify({"status": "error", "msg": "no position"}), 500

    print(f"✅ Entry price: {entry_price}")

    # TP/SL 계산
    tp_price = entry_price * (1 + TP_RATIO) if position == "long" else entry_price * (1 - TP_RATIO)
    sl_price = entry_price * (1 - SL_RATIO) if position == "long" else entry_price * (1 + SL_RATIO)

    print(f"🎯 Setting TP at {tp_price:.4f}, SL at {sl_price:.4f}")

    # 모니터링 루프 (5초 간격)
    while True:
        try:
            resp = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}").json()
            current_price = float(resp["tickers"][0]["last"])

            if position == "long":
                if current_price >= tp_price:
                    print(f"✅ TP Hit at {current_price}")
                    close_position("sell")
                    break
                elif current_price <= sl_price:
                    print(f"❌ SL Hit at {current_price}")
                    close_position("sell")
                    break

            else:  # short
                if current_price <= tp_price:
                    print(f"✅ TP Hit at {current_price}")
                    close_position("buy")
                    break
                elif current_price >= sl_price:
                    print(f"❌ SL Hit at {current_price}")
                    close_position("buy")
                    break

            time.sleep(5)

        except Exception as e:
            print(f"Error in loop: {e}")
            time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
