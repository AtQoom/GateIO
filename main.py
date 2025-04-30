import os
import time
import requests
from flask import Flask, request, jsonify
from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position

app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data or "signal" not in data or "position" not in data:
            return jsonify({"error": "Invalid data"}), 400

        position = data["position"]
        print(f"📥 Received signal: {position.upper()}")

        side = "buy" if position == "long" else "sell"
        place_order(side)

        time.sleep(1.5)
        entry_price = get_open_position()
        if not entry_price:
            print("❌ Failed to retrieve entry price.")
            return jsonify({"status": "error", "msg": "no position"}), 500

        print(f"✅ Entry price: {entry_price}")
        tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
        sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)
        print(f"🎯 Setting TP at {tp_price:.4f}, SL at {sl_price:.4f}")

        while True:
            try:
                res = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
                current_price = float(res.json()["tickers"][0]["last"])

                if position == "long":
                    if current_price >= tp_price:
                        print(f"✅ TP Hit at {current_price}")
                        close_position("sell")
                        break
                    elif current_price <= sl_price:
                        print(f"❌ SL Hit at {current_price}")
                        close_position("sell")
                        break
                else:
                    if current_price <= tp_price:
                        print(f"✅ TP Hit at {current_price}")
                        close_position("buy")
                        break
                    elif current_price >= sl_price:
                        print(f"❌ SL Hit at {current_price}")
                        close_position("buy")
                        break

                time.sleep(5)
            except Exception as loop_err:
                print(f"Loop error: {loop_err}")
                time.sleep(5)

        return jsonify({"status": "closed"}), 200

    except Exception as e:
        print(f"🔥 Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
