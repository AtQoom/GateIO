import os
import time
import json
import requests
import traceback
from flask import Flask, request, jsonify
from config import BASE_URL, SYMBOL, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT
from utils import get_headers, get_open_position, place_order, close_position

app = Flask(__name__)
active_position = None  # 현재 포지션 관리 상태

@app.route("/", methods=["POST"])
def webhook():
    global active_position
    data = request.json

    if not data or "signal" not in data or "position" not in data:
        return jsonify({"error": "Invalid payload"}), 400

    position = data["position"].lower()  # long or short
    print(f"\n📩 알림 수신: {position.upper()} 포지션 요청")

    # 중복 진입 방지
    if active_position is not None:
        print(f"⚠️ 이미 실행 중인 포지션 있음: {active_position.upper()}")
        return jsonify({"status": "ignored", "reason": "already in position"}), 200

    side = "buy" if position == "long" else "sell"
    try:
        success = place_order(side)
        if not success:
            raise Exception("❌ 진입 실패: 주문 실행 실패")

        time.sleep(2)
        entry_price = get_open_position()
        if not entry_price:
            raise Exception("❌ 진입 실패: 평단가 확인 실패")

        print(f"✅ 진입 완료: {position.upper()} @ {entry_price:.4f}")
        tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if position == "long" else entry_price * (1 - TAKE_PROFIT_PERCENT)
        sl_price = entry_price * (1 - STOP_LOSS_PERCENT) if position == "long" else entry_price * (1 + STOP_LOSS_PERCENT)
        print(f"🎯 TP: {tp_price:.4f}, SL: {sl_price:.4f}")

        active_position = position

        # 모니터링 루프
        while True:
            try:
                resp = requests.get(f"{BASE_URL}/spot/tickers?currency_pair={SYMBOL}")
                current_price = float(resp.json()["tickers"][0]["last"])
                print(f"📈 현재가: {current_price:.4f}")

                if position == "long":
                    if current_price >= tp_price:
                        print(f"✅ TP 도달 @ {current_price}")
                        close_position("sell")
                        break
                    elif current_price <= sl_price:
                        print(f"❌ SL 도달 @ {current_price}")
                        close_position("sell")
                        break
                else:
                    if current_price <= tp_price:
                        print(f"✅ TP 도달 @ {current_price}")
                        close_position("buy")
                        break
                    elif current_price >= sl_price:
                        print(f"❌ SL 도달 @ {current_price}")
                        close_position("buy")
                        break

                time.sleep(5)

            except Exception as e:
                print("🔁 루프 오류:", e)
                traceback.print_exc()
                time.sleep(5)

        active_position = None
        return jsonify({"status": "closed", "price": current_price}), 200

    except Exception as e:
        print("🔥 처리 중 오류 발생:", e)
        traceback.print_exc()
        active_position = None
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
