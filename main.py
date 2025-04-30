import os
import time
import requests
import json
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "ok", "message": "GateIO bot is running!"})

@app.route('/', methods=['POST'])
def webhook():
    data = request.json

    if not data or "signal" not in data or "position" not in data:
        return jsonify({"status": "error", "message": "Invalid request format"}), 400

    position = data["position"].lower()
    signal = data["signal"].lower()

    print(f"[📩 RECEIVED] signal: {signal}, position: {position}")

    if signal != "entry" or position not in ["long", "short"]:
        return jsonify({"status": "ignored", "message": "Unsupported signal or position"}), 200

    side = "buy" if position == "long" else "sell"
    place_order(side)

    time.sleep(1.5)
    entry_price = get_open_position()

    if not entry_price:
        print("❌ Entry price fetch failed.")
        return jsonify({"status": "error", "message": "Failed to confirm entry"}), 500

    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

    print(f"🎯 Entry: {entry_price:.4f}, TP: {tp_price:.4f}, SL: {sl_price:.4f}")

    try:
        while True:
            resp = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
            data = resp.json()

            if "tickers" not in data or not data["tickers"]:
                print("⏳ Ticker not found, retrying...")
                time.sleep(3)
                continue

            price = float(data["tickers"][0]["last"])

            if position == "long":
                if price >= tp_price:
                    print(f"✅ TAKE PROFIT hit at {price}")
                    close_position("sell")
                    break
                elif price <= sl_price:
                    print(f"🛑 STOP LOSS hit at {price}")
                    close_position("sell")
                    break
            else:
                if price <= tp_price:
                    print(f"✅ TAKE PROFIT hit at {price}")
                    close_position("buy")
                    break
                elif price >= sl_price:
                    print(f"🛑 STOP LOSS hit at {price}")
                    close_position("buy")
                    break

            time.sleep(3)

    except Exception as e:
        print(f"[ERROR LOOP] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "closed", "message": f"{position} position closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
