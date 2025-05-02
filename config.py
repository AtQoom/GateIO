import os

# 거래 설정
SYMBOL = "SOL_USDT"
BASE_URL = "https://api.gateio.ws/api/v4"

# 환경변수 (Railway 기준 이름으로 통일)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# 환경 변수 유효성 검사
if not API_KEY or not API_SECRET:
    raise EnvironmentError("❌ API 키가 설정되지 않았습니다. Railway 환경변수 확인 요망.")

# TP/SL 퍼센트
TAKE_PROFIT_PERCENT = 0.022  # 2.2%
STOP_LOSS_PERCENT = 0.007    # 0.7%

# 트레일링 익절은 main.py에서 동적으로 처리
