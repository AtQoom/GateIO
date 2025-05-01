import os
import time
import json
import requests
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position, fetch_current_price

app = Flask(__name__)

TRAIL_PERCENT = 0.012  # 1.2% trailing stop distance
TRAIL_OFFSET = 0.006   # 0.6% offset

@app.route('/', methods=['POST'])
def webhook():
    data = request.json
    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data"}), 400

    position = data["position"].lower()
    print(f"📥 Received signal: {position.upper()}")

    side = "buy" if position == "long" else "sell"
    place_order(side)
    time.sleep(1.5)

    entry_price = get_open_position()
    if not entry_price:
        print("❌ Failed to retrieve entry price.")
        return jsonify({"status": "error", "msg": "no position"}), 500

    print(f"✅ Entry price: {entry_price:.4f}")

    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

    print(f"🎯 TP: {tp_price:.4f}, SL: {sl_price:.4f}")

    highest_price = entry_price
    lowest_price = entry_price

    while True:
        try:
            current_price = fetch_current_price()

            if position == "long":
                highest_price = max(highest_price, current_price)
                trail_sl = highest_price * (1 - TRAIL_PERCENT + TRAIL_OFFSET)

                if current_price >= tp_price:
                    print(f"✅ TP Hit at {current_price:.4f}")
                    close_position("sell")
                    break
                elif current_price <= sl_price:
                    print(f"❌ SL Hit at {current_price:.4f}")
                    close_position("sell")
                    break
                elif current_price <= trail_sl:
                    print(f"🔻 Trailing SL Hit at {current_price:.4f} (Trail SL: {trail_sl:.4f})")
                    close_position("sell")
                    break

            else:  # short
                lowest_price = min(lowest_price, current_price)
                trail_sl = lowest_price * (1 + TRAIL_PERCENT - TRAIL_OFFSET)

                if current_price <= tp_price:
                    print(f"✅ TP Hit at {current_price:.4f}")
                    close_position("buy")
                    break
                elif current_price >= sl_price:
                    print(f"❌ SL Hit at {current_price:.4f}")
                    close_position("buy")
                    break
                elif current_price >= trail_sl:
                    print(f"🔺 Trailing SL Hit at {current_price:.4f} (Trail SL: {trail_sl:.4f})")
                    close_position("buy")
                    break

            time.sleep(5)

        except Exception as e:
            print(f"⚠️ Monitoring error: {e}")
            time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
