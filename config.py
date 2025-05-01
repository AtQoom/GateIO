import os

SYMBOL = "SOL_USDT"
BASE_URL = "https://api.gate.io/api/v4"
TAKE_PROFIT_PERCENT = 2.2 / 100  # 2.2%
STOP_LOSS_PERCENT = 0.7 / 100    # 0.7%

TRAIL_PERCENT = 1.2 / 100        # 1.2% → trailing 시작 기준 (서버에서는 TP 도달로 판단)
TRAIL_OFFSET_PERCENT = 0.6 / 100 # 0.6% trailing 익절 간격

API_KEY = os.getenv("GATEIO_API_KEY")
API_SECRET = os.getenv("GATEIO_API_SECRET")
