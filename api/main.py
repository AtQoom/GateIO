from fastapi import FastAPI, Request
import uvicorn
from api.storage import store_position
from api.trade import place_order
import asyncio
from api.monitor import monitor_loop
from api.config import SYMBOL

app = FastAPI()

@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("📡 Webhook received:", data)

    if data.get("signal") == "entry":
        side = data.get("position")
        symbol = SYMBOL
        price = get_current_price(symbol)

        store_position(symbol, "buy" if side == "long" else "sell", price)
        place_order(symbol, "buy" if side == "long" else "sell", price)

    return {"status": "ok"}


def get_current_price(symbol):
    import requests
    url = f"https://api.gateio.ws/api/v4/futures/usdt/tickers"
    res = requests.get(url)
    data = res.json()
    for d in data:
        if d["contract"] == symbol:
            return float(d["last"])
    return None


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(monitor_loop())

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000)
