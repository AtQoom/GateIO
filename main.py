import os
import time
from flask import Flask, request, jsonify
from utils import get_open_position, place_order, close_position
from config import TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT, SYMBOL

app = Flask(__name__)

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print(f"📨 Webhook received: {data}")

        if not data or "signal" not in data or "position" not in data:
            return jsonify({"error": "Invalid payload"}), 400

        position = data["position"]
        print(f"🟢 Entry signal for: {position}")

        side = "buy" if position == "long" else "sell"
        place_order(side)

        time.sleep(2)  # 진입 대기

        entry_price = get_open_position()
        if not entry_price:
            print("❌ No open position found after entry")
            return jsonify({"status": "error", "msg": "no open position"}), 500

        tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
        sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

        print(f"📌 Entry: {entry_price:.4f}, TP: {tp_price:.4f}, SL: {sl_price:.4f}")

        # 모니터링 루프
        while True:
            try:
                from config import BASE_URL
                import requests

                url = f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}"
                res = requests.get(url)
                current_price = float(res.json()["tickers"][0]["last"])
                print(f"📊 Current: {current_price}")

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
                print(f"⚠️ Error in loop: {loop_err}")
                time.sleep(5)

        return jsonify({"status": "closed"}), 200

    except Exception as e:
        print(f"❗ Webhook error: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))  # Railway default
    app.run(host="0.0.0.0", port=port)
