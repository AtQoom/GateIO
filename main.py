import os
import time
import json
import requests
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position

app = Flask(__name__)

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        if not data or "signal" not in data or "position" not in data:
            return jsonify({"error": "Invalid data"}), 400

        signal = data["signal"].lower()
        position = data["position"].lower()
        print(f"📩 Received: signal={signal}, position={position}")

        if signal != "entry" or position not in ["long", "short"]:
            return jsonify({"error": "Invalid signal or position"}), 400

        # 1. 진입
        side = "buy" if position == "long" else "sell"
        print(f"🟢 Placing {position.upper()} order...")
        place_order(side)

        # 2. 약간 대기 후 포지션 확인
        time.sleep(2)
        entry_price = get_open_position()
        if entry_price is None:
            print("❌ Failed to get entry price. Position might not be open.")
            return jsonify({"status": "entry_failed"}), 500

        print(f"📈 Entry price: {entry_price}")

        # 3. 익절 / 손절 가격 계산
        tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
        sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

        print(f"🎯 TP: {tp_price:.4f}, SL: {sl_price:.4f}")

        # 4. 모니터링
        while True:
            try:
                resp = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}", timeout=5)
                current_price = float(resp.json()["tickers"][0]["last"])

                print(f"🔁 Current price: {current_price}")

                if position == "long":
                    if current_price >= tp_price:
                        print(f"✅ TAKE PROFIT triggered at {current_price}")
                        close_position("sell")
                        break
                    elif current_price <= sl_price:
                        print(f"🛑 STOP LOSS triggered at {current_price}")
                        close_position("sell")
                        break
                else:  # short
                    if current_price <= tp_price:
                        print(f"✅ TAKE PROFIT triggered at {current_price}")
                        close_position("buy")
                        break
                    elif current_price >= sl_price:
                        print(f"🛑 STOP LOSS triggered at {current_price}")
                        close_position("buy")
                        break

                time.sleep(5)

            except Exception as e:
                print(f"⚠️ Error in monitoring: {e}")
                time.sleep(5)

        return jsonify({"status": "closed"}), 200

    except Exception as e:
        print(f"🔥 Webhook error: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
