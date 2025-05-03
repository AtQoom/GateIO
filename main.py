import os
import time
import json
import requests
from flask import Flask, request, jsonify
from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_open_position, place_order, close_position

app = Flask(__name__)

@app.route("/", methods=["POST", "HEAD"])
def webhook():
    if request.method == "HEAD":
        return "", 200

    try:
        data = request.get_json(force=True)
        signal = data.get("signal", "").lower()
        print(f"📥 [{time.strftime('%Y-%m-%d %H:%M:%S')}] 시그널 수신: {signal.upper()}")

        if "long" in signal:
            side = "buy"
            exit_side = "sell"
        elif "short" in signal:
            side = "sell"
            exit_side = "buy"
        else:
            return jsonify({"error": "Invalid signal"}), 400

        # 기존 포지션 종료
        close_position(exit_side)
        time.sleep(1.5)

        # 신규 포지션 진입
        place_order(side)
        time.sleep(2)

        # 평단 확인
        entry_price = get_open_position()
        if not entry_price:
            print("❌ 평단가 확인 실패 (포지션 없음 또는 잔고 부족)")
            return jsonify({"error": "entry price not found"}), 500

        print(f"✅ 진입가: {entry_price:.4f}")
        tp = entry_price * (1 + TAKE_PROFIT_PERCENT) if side == "buy" else entry_price * (1 - TAKE_PROFIT_PERCENT)
        sl = entry_price * (1 - STOP_LOSS_PERCENT) if side == "buy" else entry_price * (1 + STOP_LOSS_PERCENT)
        print(f"🎯 TP = {tp:.4f}, SL = {sl:.4f}")

        # 트레일링 감시 루프
        highest = lowest = entry_price

        while True:
            try:
                res = requests.get(f"{BASE_URL}/futures/usdt/ticker?contract={SYMBOL}", timeout=5)
                res.raise_for_status()
                price = float(res.json()["last"])
            except Exception as e:
                print(f"⚠️ 가격 조회 실패: {e}")
                time.sleep(5)
                continue

            print(f"[{time.strftime('%H:%M:%S')}] 💹 현재가: {price:.4f}")

            profit_ratio = (price / entry_price - 1) if side == "buy" else (entry_price / price - 1)
            trail_pct = (
                0.018 if profit_ratio >= 0.04 else
                0.015 if profit_ratio >= 0.03 else
                0.012 if profit_ratio >= 0.02 else
                0.009 if profit_ratio >= 0.01 else
                0.006
            )

            if side == "buy":
                highest = max(highest, price)
                trail_sl = highest * (1 - trail_pct)
                if price >= tp or price <= sl or price <= trail_sl:
                    print(f"🔺 익절/손절/트레일 도달: {price:.4f}")
                    close_position("sell")
                    break
            else:
                lowest = min(lowest, price)
                trail_sl = lowest * (1 + trail_pct)
                if price <= tp or price >= sl or price >= trail_sl:
                    print(f"🔻 익절/손절/트레일 도달: {price:.4f}")
                    close_position("buy")
                    break

            time.sleep(5)

        return jsonify({"status": "closed"}), 200

    except Exception as e:
        print(f"[ERROR] 웹훅 처리 실패: {e}")
        return jsonify({"error": "internal error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 서버 실행중: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port)
