import os

# 🎯 기본 설정
SYMBOL = "SOL_USDT"             # 거래 심볼
MARGIN_MODE = "isolated"        # 격리 마진모드

# 🎯 TP/SL 퍼센트
TAKE_PROFIT_PERCENT = 2.2 / 100     # 2.2%
STOP_LOSS_PERCENT = 0.7 / 100       # 0.7%

# 🟩 트레일링 익절 설정 (파인스크립트 기준: 1.2% 트레일링, 0.6% 오프셋)
TRAILING_PERCENT = 1.2 / 100        # 가격이익 추적 폭 (예: 1.2%)
TRAILING_OFFSET = 0.6 / 100         # 되돌림 허용 폭 (예: 0.6%)

# 🔐 API 키 (Railway 환경변수에서 자동 로드)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

# 🌐 Gate.io API Endpoint
BASE_URL = "https://api.gateio.ws/api/v4"
