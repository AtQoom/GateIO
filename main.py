import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from utils.gateio import GateioClient
import config  # ← 여기에서 config 값을 import

load_dotenv()

client = GateioClient(
    api_key=os.getenv("GATEIO_API_KEY"),
    secret_key=os.getenv("GATEIO_SECRET_KEY"),
    base_url=os.getenv("GATEIO_API_BASE"),
    config=config  # ← config.py 통째로 넘김
)

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    action = data.get("action", "")

    if action == "long":
        client.open_position("long")
    elif action == "short":
        client.open_position("short")

    return {"status": "ok"}
