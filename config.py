import os

# 거래 설정
SYMBOL = "SOL_USDT"
MARGIN_MODE = "isolated"  # 격리 모드

# 수익 및 손실 비율 설정
TAKE_PROFIT_PERCENT = 2.2 / 100  # 2.2%
STOP_LOSS_PERCENT = 0.7 / 100    # 0.7%

# 환경 변수에서 API 키 로드
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

# Gate.io API 기본 URL
BASE_URL = "https://api.gateio.ws/api/v4"
