import os

# 기본 설정
SYMBOL = "SOL_USDT"
MARGIN_MODE = "isolated"

# TP/SL 설정
TAKE_PROFIT_PERCENT = 2.2 / 100  # 2.2%
STOP_LOSS_PERCENT = 0.7 / 100    # 0.7%

# API 키 (Railway 환경변수에서 로드)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

# API URL
BASE_URL = "https://api.gateio.ws/api/v4"
