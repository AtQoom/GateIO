import os
import time
import requests
from flask import Flask, request, jsonify

from config import SYMBOL, ORDER_SIZE_USDT, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT, BASE_URL
from utils import get_headers, get_open_position, place_order, close_position

app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    data = request.json
    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data"}), 400

    position = data["position"].lower()  # "long" or "short"
    print(f"📥 Received signal: {position.upper()}")

    side = "buy" if position == "long" else "sell"
    place_order(side)

    time.sleep(1.5)  # 진입 대기

    entry_price = get_open_position()
    if not entry_price:
        print("❌ Failed to retrieve entry price.")
        return jsonify({"status": "error", "msg": "no position"}), 500

    print(f"✅ Entry price: {entry_price:.4f}")

    # TP/SL 계산
    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

    print(f"🎯 TP: {tp_price:.4f}, SL: {sl_price:.4f}")

    # 모니터링 루프
    while True:
        try:
            resp = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}").json()
            current_price = float(resp["tickers"][0]["last"])

            if position == "long":
                if current_price >= tp_price:
                    print(f"✅ TP Hit at {current_price:.4f}")
                    close_position("sell")
                    break
                elif current_price <= sl_price:
                    print(f"❌ SL Hit at {current_price:.4f}")
                    close_position("sell")
                    break
            else:  # short
                if current_price <= tp_price:
                    print(f"✅ TP Hit at {current_price:.4f}")
                    close_position("buy")
                    break
                elif current_price >= sl_price:
                    print(f"❌ SL Hit at {current_price:.4f}")
                    close_position("buy")
                    break

            time.sleep(5)

        except Exception as e:
            print(f"⚠️ Error in monitoring loop: {e}")
            time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
