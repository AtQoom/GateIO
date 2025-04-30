import os
import time
import requests
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position

app = Flask(__name__)

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.json
        print("📥 Received data:", data)

        if not data or "signal" not in data or "position" not in data:
            print("❌ Invalid payload")
            return jsonify({"error": "Invalid data"}), 400

        position = data["position"].lower()
        if position not in ["long", "short"]:
            print("❌ Invalid position:", position)
            return jsonify({"error": "Invalid position"}), 400

        # 진입 처리
        side = "buy" if position == "long" else "sell"
        print(f"📌 Placing {position.upper()} order...")
        place_order(side)

        time.sleep(1.5)  # 포지션 갱신 대기

        # 진입가 확인
        entry_price = get_open_position()
        if not entry_price:
            print("❌ Entry price not found.")
            return jsonify({"status": "error", "message": "entry price unavailable"}), 500

        print(f"✅ Entry price: {entry_price:.4f}")

        # TP/SL 계산
        tp = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
        sl = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

        print(f"🎯 TP: {tp:.4f}, SL: {sl:.4f}")

        # 모니터링 루프
        while True:
            try:
                response = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
                data = response.json()
                current_price = float(data["tickers"][0]["last"])

                print(f"📊 Current Price: {current_price:.4f}")

                if position == "long":
                    if current_price >= tp:
                        print("✅ TP Hit")
                        close_position("sell")
                        break
                    elif current_price <= sl:
                        print("❌ SL Hit")
                        close_position("sell")
                        break
                else:
                    if current_price <= tp:
                        print("✅ TP Hit")
                        close_position("buy")
                        break
                    elif current_price >= sl:
                        print("❌ SL Hit")
                        close_position("buy")
                        break

                time.sleep(5)

            except Exception as e:
                print("⚠️ Error during monitoring loop:", e)
                time.sleep(5)

        return jsonify({"status": "closed"}), 200

    except Exception as e:
        print("❌ Main exception:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
