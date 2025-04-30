from fastapi import FastAPI, Request
import httpx
import os

app = FastAPI()

API_KEY = os.getenv("GATEIO_API_KEY")
API_SECRET = os.getenv("GATEIO_API_SECRET")
BASE_URL = "https://api.gateio.ws/api/v4"

headers = {
    "Content-Type": "application/json",
    "KEY": API_KEY,
    "SIGN": "",  # Signature will be handled in production setup
}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    signal = data.get("signal")
    position = data.get("position")

    if signal != "entry":
        return {"status": "ignored"}

    symbol = "SOL_USDT"
    size = "1"
    side = "buy" if position == "long" else "sell"

    async with httpx.AsyncClient() as client:
        # Simplified spot market order (in real trading, you need to sign requests)
        response = await client.post(
            f"{BASE_URL}/futures/usdt/orders",
            headers=headers,
            json={
                "contract": symbol,
                "size": size,
                "price": 0,
                "tif": "ioc",
                "text": "auto-trader",
                "side": side,
            },
        )
        return {"status": "order_sent", "response": response.json()}
