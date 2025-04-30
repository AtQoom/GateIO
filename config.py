import os

# 기본 설정
SYMBOL = "SOL_USDT"
MARGIN_MODE = "isolated"  # 격리 모드 사용
ORDER_SIZE_USDT = 10  # 진입 금액 단위 (USDT)

# TP/SL 설정
TAKE_PROFIT_PERCENT = 2.2 / 100  # 2.2%
STOP_LOSS_PERCENT = 0.7 / 100    # 0.7%

# 환경변수에서 API 키 로드 (Railway 환경변수에 저장)
API_KEY = os.getenv("GATEIO_API_KEY")
API_SECRET = os.getenv("GATEIO_API_SECRET")
API_PASSPHRASE = os.getenv("GATEIO_API_PASSPHRASE")  # 혹시 사용하는 경우
