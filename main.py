import os
import time
import requests
from flask import Flask, request, jsonify

from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position, get_current_price

app = Flask(__name__)


@app.route("/", methods=["POST"])
def webhook():
    data = request.json

    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid signal format"}), 400

    position = data["position"].lower()
    if position not in ["long", "short"]:
        return jsonify({"error": "Invalid position"}), 400

    print(f"\n📥 Signal received: {position.upper()}")

    # 진입
    side = "buy" if position == "long" else "sell"
    place_order(side)

    # 평단가 확인 (약간 대기)
    time.sleep(1.5)
    entry_price = get_open_position()

    if not entry_price:
        print("❌ 포지션 진입 실패 또는 평단가 확인 불가")
        return jsonify({"error": "no open position"}), 500

    print(f"✅ 진입 완료 | 진입가: {entry_price:.4f}")

    # TP / SL 설정
    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)

    print(f"🎯 TP 목표가: {tp_price:.4f}, SL 손절가: {sl_price:.4f}")

    # 모니터링 루프
    while True:
        try:
            price = get_current_price()
            print(f"📊 현재가: {price:.4f}")

            if position == "long":
                if price >= tp_price:
                    print(f"✅ TP 도달! {price:.4f}")
                    close_position("sell")
                    break
                elif price <= sl_price:
                    print(f"❌ SL 도달! {price:.4f}")
                    close_position("sell")
                    break
            else:  # short
                if price <= tp_price:
                    print(f"✅ TP 도달! {price:.4f}")
                    close_position("buy")
                    break
                elif price >= sl_price:
                    print(f"❌ SL 도달! {price:.4f}")
                    close_position("buy")
                    break

            time.sleep(5)

        except Exception as e:
            print(f"⚠️ 오류 발생: {e}")
            time.sleep(5)

    return jsonify({"status": "closed"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
