
import os
import time
import logging
import requests
from flask import Flask, request, jsonify
from config import BASE_URL, SYMBOL, MARGIN_MODE, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT, API_KEY, API_SECRET
from utils import get_headers, get_open_position, place_order, close_position

# === 로깅 설정 ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(__name__)

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data or "signal" not in data or "position" not in data:
            logging.warning("🚫 Invalid or missing data in webhook payload")
            return jsonify({"error": "Invalid data"}), 400

        position = data["position"]
        logging.info(f"📥 Signal received: {position.upper()}")

        side = "buy" if position == "long" else "sell"
        place_order(side)

        time.sleep(1.5)
        entry_price = get_open_position()

        if not entry_price:
            logging.error("❌ Entry price not found. No position open?")
            return jsonify({"status": "error", "msg": "no position"}), 500

        logging.info(f"✅ Entry at {entry_price}")

        tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
        sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

        logging.info(f"🎯 Target: TP={tp_price:.4f}, SL={sl_price:.4f}")

        while True:
            try:
                resp = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
                resp.raise_for_status()
                current_price = float(resp.json()["tickers"][0]["last"])
                logging.info(f"📈 Current price: {current_price}")

                if position == "long":
                    if current_price >= tp_price:
                        logging.info(f"✅ TP hit at {current_price}")
                        close_position("sell")
                        break
                    elif current_price <= sl_price:
                        logging.warning(f"❌ SL hit at {current_price}")
                        close_position("sell")
                        break
                else:
                    if current_price <= tp_price:
                        logging.info(f"✅ TP hit at {current_price}")
                        close_position("buy")
                        break
                    elif current_price >= sl_price:
                        logging.warning(f"❌ SL hit at {current_price}")
                        close_position("buy")
                        break

                time.sleep(5)

            except Exception as e:
                logging.error("💥 Error in monitoring loop", exc_info=e)
                time.sleep(5)

        return jsonify({"status": "closed"}), 200

    except Exception as e:
        logging.critical("🔥 Webhook critical failure", exc_info=e)
        return jsonify({"error": "internal server error"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logging.info(f"🚀 Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)
