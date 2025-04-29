import asyncio
import requests
from storage import get_position, clear_position
from trade import place_order

async def monitor_loop():
    while True:
        pos = get_position()
        if pos["symbol"]:
            price = get_current_price(pos["symbol"])
            entry = float(pos["entry_price"])
            pnl_pct = (price - entry) / entry * 100 if pos["side"] == "buy" else (entry - price) / entry * 100

            if pnl_pct >= 1.0:
                print(f"💰 익절 조건 충족 (+{pnl_pct:.2f}%) → 청산")
                place_order(pos["symbol"], "sell" if pos["side"] == "buy" else "buy", price, pos["size"], reduce_only=True)
                clear_position()

            elif pnl_pct <= -0.5:
                print(f"⚠ 손절 조건 충족 ({pnl_pct:.2f}%) → 청산")
                place_order(pos["symbol"], "sell" if pos["side"] == "buy" else "buy", price, pos["size"], reduce_only=True)
                clear_position()

        await asyncio.sleep(5)

def get_current_price(symbol):
    url = f"https://api.gateio.ws/api/v4/futures/usdt/tickers"
    res = requests.get(url)
    data = res.json()
    for d in data:
        if d["contract"] == symbol:
            return float(d["last"])
    return None
