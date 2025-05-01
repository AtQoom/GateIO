import os

# 거래 심볼
SYMBOL = "SOL_USDT"

# Gate.io API URL
BASE_URL = "https://api.gateio.ws/api/v4"

# API 키는 Railway 환경변수에 저장됨
API_KEY = os.getenv("GATEIO_API_KEY")
API_SECRET = os.getenv("GATEIO_API_SECRET")

# 진입 후 TP/SL 퍼센트
TAKE_PROFIT_PERCENT = 0.022  # 2.2%
STOP_LOSS_PERCENT = 0.007    # 0.7%

# 트레일링 퍼센트 로직은 main.py에서 동적으로 처리
# 여기에는 고정 비율이 없도록 유지
