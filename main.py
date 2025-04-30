import os
import time
import requests
import json
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_headers, get_open_position, place_order, close_position

app = Flask(__name__)

@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json()

    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data"}), 400

    signal = data["signal"].lower()
    position = data["position"].lower()

    print(f"📩 Signal received → {signal.upper()} | Position: {position.upper()}")

    if signal != "entry":
        return jsonify({"error": "Unsupported signal type"}), 400

    # 진입 시도
    side = "buy" if position == "long" else "sell"
    place_order(side)

    time.sleep(1.5)

    # 포지션 평단가 조회
    entry_price = get_open_position()
    if not entry_price:
        print("❌ Entry price fetch failed.")
        return jsonify({"status": "failed", "message": "No open position"}), 500

    # TP/SL 계산
    tp = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

    print(f"✅ Entry @ {entry_price:.4f} → TP: {tp:.4f}, SL: {sl:.4f}")

    while True:
        try:
            res = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
            data = res.json()
            price = float(data["tickers"][0]["last"])

            if position == "long":
                if price >= tp:
                    print(f"🎯 TP hit @ {price}")
                    close_position("sell")
                    break
                elif price <= sl:
                    print(f"💥 SL hit @ {price}")
                    close_position("sell")
                    break
            else:
                if price <= tp:
                    print(f"🎯 TP hit @ {price}")
                    close_position("buy")
                    break
                elif price >= sl:
                    print(f"💥 SL hit @ {price}")
                    close_position("buy")
                    break

            time.sleep(5)

        except Exception as e:
            print(f"⚠️ Monitoring error: {e}")
            time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Running on port {port}...")
    app.run(host="0.0.0.0", port=port)
