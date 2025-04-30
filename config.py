import os
from dotenv import load_dotenv
load_dotenv()

GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_API_SECRET = os.getenv("GATE_API_SECRET")
GATE_SYMBOL = os.getenv("GATE_SYMBOL", "SOL_USDT")