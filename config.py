import os

SYMBOL = "SOL_USDT"
BASE_URL = "https://api.gateio.ws/api/v4"
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not API_KEY or not API_SECRET:
    raise EnvironmentError("❌ API 키가 설정되지 않았습니다. Railway 환경변수 확인 요망.")

TAKE_PROFIT_PERCENT = 0.022
STOP_LOSS_PERCENT = 0.007
