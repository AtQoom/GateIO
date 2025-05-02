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
        return '', 200  # 헬스 체크 응답

    data = request.json
    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid data format"}), 400

    position = data["position"].lower()
    print(f"📥 [{time.strftime('%Y-%m-%d %H:%M:%S')}] 시그널 수신: {position.upper()}")

    side = "buy" if position == "long" else "sell"
    place_order(side)
    time.sleep(1.5)

    entry_price = get_open_position()
    if not entry_price:
        print("❌ 평단가 확인 실패 (포지션 없음 또는 잔고 부족)")
        return jsonify({"status": "error", "msg": "entry price not found"}), 500

    print(f"✅ 진입가: {entry_price:.4f}")

    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)
    print(f"🎯 TP = {tp_price:.4f}, SL = {sl_price:.4f}")

    highest_price = entry_price
    lowest_price = entry_price
    retry_delay = 5

    while True:
        try:
            res = requests.get(f"{BASE_URL}/futures/usdt/ticker?contract={SYMBOL}")
            res.raise_for_status()
            current_price = float(res.json()["last"])
        except Exception as e:
            print(f"⚠️ 가격 조회 실패: {e}")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue

        print(f"[{time.strftime('%H:%M:%S')}] 💹 현재가: {current_price:.4f}")

        profit_ratio = (current_price / entry_price - 1) if position == "long" else (entry_price / current_price - 1)
        trail_pct = (
            0.018 if profit_ratio >= 0.04 else
            0.015 if profit_ratio >= 0.03 else
            0.012 if profit_ratio >= 0.02 else
            0.009 if profit_ratio >= 0.01 else
            0.006
        )

        if position == "long":
            highest_price = max(highest_price, current_price)
            trail_sl = highest_price * (1 - trail_pct)

            if current_price >= tp_price:
                print(f"✅ TP 도달: {current_price:.4f}")
                close_position("sell")
                break
            elif current_price <= sl_price:
                print(f"❌ SL 도달: {current_price:.4f}")
                close_position("sell")
                break
            elif current_price <= trail_sl:
                print(f"🔻 트레일링 SL 도달: {current_price:.4f} <= {trail_sl:.4f}")
                close_position("sell")
                break

        else:
            lowest_price = min(lowest_price, current_price)
            trail_sl = lowest_price * (1 + trail_pct)

            if current_price <= tp_price:
                print(f"✅ TP 도달: {current_price:.4f}")
                close_position("buy")
                break
            elif current_price >= sl_price:
                print(f"❌ SL 도달: {current_price:.4f}")
                close_position("buy")
                break
            elif current_price >= trail_sl:
                print(f"🔺 트레일링 SL 도달: {current_price:.4f} >= {trail_sl:.4f}")
                close_position("buy")
                break

        time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 서버 실행중: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port)
