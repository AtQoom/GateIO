import os
import time
import json
import requests
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position

app = Flask(__name__)

@app.route('/', methods=['POST', 'HEAD'])
def webhook():
    if request.method == 'HEAD':
        return '', 200  # 헬스 체크용 응답

    data = request.json
    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data"}), 400

    position = data["position"].lower()
    print(f"📥 Received signal: {position.upper()}")

    # 진입
    side = "buy" if position == "long" else "sell"
    place_order(side)
    time.sleep(1.5)

    # 평단가 확인
    entry_price = get_open_position()
    if not entry_price:
        print("❌ Failed to retrieve entry price.")
        return jsonify({"status": "error", "msg": "no position"}), 500

    print(f"✅ Entry price: {entry_price:.4f}")

    # TP / SL 계산
    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)
    print(f"🎯 Target: TP = {tp_price:.4f}, SL = {sl_price:.4f}")

    # 트레일링 설정
    trail_offset = entry_price * 0.006  # 0.6%
    highest_price = entry_price
    lowest_price = entry_price

    # 가격 모니터링 루프
    while True:
        try:
            resp = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
            price_data = resp.json()
            current_price = float(price_data["tickers"][0]["last"])
        except Exception as e:
            print(f"⚠️ Monitoring error: {e}")
            time.sleep(5)
            continue

        print(f"💹 Price: {current_price:.4f}")

        if position == "long":
            highest_price = max(highest_price, current_price)
            trail_sl = highest_price - trail_offset

            if current_price >= tp_price:
                print(f"✅ TP Hit: {current_price}")
                close_position("sell")
                break
            elif current_price <= sl_price:
                print(f"❌ SL Hit: {current_price}")
                close_position("sell")
                break
            elif current_price <= trail_sl:
                print(f"🔻 Trailing SL Hit: {current_price} <= {trail_sl}")
                close_position("sell")
                break

        else:  # short
            lowest_price = min(lowest_price, current_price)
            trail_sl = lowest_price + trail_offset

            if current_price <= tp_price:
                print(f"✅ TP Hit: {current_price}")
                close_position("buy")
                break
            elif current_price >= sl_price:
                print(f"❌ SL Hit: {current_price}")
                close_position("buy")
                break
            elif current_price >= trail_sl:
                print(f"🔺 Trailing SL Hit: {current_price} >= {trail_sl}")
                close_position("buy")
                break

        time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
