from fastapi import FastAPI, Request
import os
import httpx
from dotenv import load_dotenv
from utils import verify_signature, send_order

load_dotenv()

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    if not verify_signature(data, os.getenv("WEBHOOK_SECRET")):
        return {"status": "unauthorized"}

    signal = data.get("signal")
    price = data.get("price")

    if signal in ["long", "short"]:
        response = await send_order(signal, price)
        return {"status": "ok", "order": response}
    return {"status": "ignored"}