
import os

# 거래 심볼
SYMBOL = "SOL_USDT"

# Gate.io API URL
BASE_URL = "https://api.gateio.ws/api/v4"

# API 키는 Railway 환경변수에서 로드됨
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

# 환경 변수 확인 (디버깅용 – 운영 시 제거하거나 주석 처리 가능)
assert API_KEY is not None, "❌ 환경변수 'API_KEY'가 설정되지 않았습니다."
assert API_SECRET is not None, "❌ 환경변수 'SECRET_KEY'가 설정되지 않았습니다."

# 진입 후 TP/SL 퍼센트
TAKE_PROFIT_PERCENT = 0.022  # 2.2%
STOP_LOSS_PERCENT = 0.007    # 0.7%

# 트레일링 익절은 main.py에서 동적으로 처리
