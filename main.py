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
        return '', 200

    data = request.json
    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data"}), 400

    position = data["position"].lower()
    print(f"📥 시그널 수신: {position.upper()}")

    side = "buy" if position == "long" else "sell"
    place_order(side)
    time.sleep(1.5)

    entry_price = get_open_position()
    if not entry_price:
        print("❌ 평단가 확인 실패")
        return jsonify({"status": "error", "msg": "no position"}), 500

    print(f"✅ 진입 완료: {entry_price:.4f}")

    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)
    print(f"🎯 TP: {tp_price:.4f}, SL: {sl_price:.4f}")

    highest = entry_price
    lowest = entry_price

    while True:
        try:
            resp = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
            current_price = float(resp.json()["tickers"][0]["last"])
        except Exception as e:
            print(f"⚠️ 가격 조회 오류: {e}")
            time.sleep(5)
            continue

        print(f"💹 현재가: {current_price:.4f}")

        # 동적 트레일링 비율 계산
        profit_ratio = (current_price / entry_price - 1) if position == "long" else (entry_price / current_price - 1)
        if profit_ratio >= 0.04:
            trail_pct = 0.018
        elif profit_ratio >= 0.03:
            trail_pct = 0.015
        elif profit_ratio >= 0.02:
            trail_pct = 0.012
        elif profit_ratio >= 0.01:
            trail_pct = 0.009
        else:
            trail_pct = 0.006

        if position == "long":
            highest = max(highest, current_price)
            trail_sl = highest * (1 - trail_pct)

            if current_price >= tp_price:
                print(f"✅ TP 도달: {current_price:.4f}")
                close_position("sell")
                break
            elif current_price <= sl_price:
                print(f"❌ SL 도달: {current_price:.4f}")
                close_position("sell")
                break
            elif current_price <= trail_sl:
                print(f"🔻 트레일링 익절: {current_price:.4f} <= {trail_sl:.4f} (trail_pct={trail_pct})")
                close_position("sell")
                break

        else:  # short
            lowest = min(lowest, current_price)
            trail_sl = lowest * (1 + trail_pct)

            if current_price <= tp_price:
                print(f"✅ TP 도달: {current_price:.4f}")
                close_position("buy")
                break
            elif current_price >= sl_price:
                print(f"❌ SL 도달: {current_price:.4f}")
                close_position("buy")
                break
            elif current_price >= trail_sl:
                print(f"🔺 트레일링 익절: {current_price:.4f} >= {trail_sl:.4f} (trail_pct={trail_pct})")
                close_position("buy")
                break

        time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
