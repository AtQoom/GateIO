import os

# 거래 설정
SYMBOL = "SOL_USDT"
BASE_URL = "https://fx-api.gateio.ws/api/v4"

# API 키 불러오기 (환경변수에서)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# ✅ 디버깅 출력: 키 일부를 출력해서 설정 확인
if not API_KEY or not API_SECRET:
    raise EnvironmentError("❌ API 키가 설정되지 않았습니다. Railway 환경변수 확인 요망.")
else:
    print(f"✅ API_KEY = {API_KEY[:6]}..., API_SECRET = {API_SECRET[:6]}... (환경변수 로딩 성공)")

# TP / SL 비율
TAKE_PROFIT_PERCENT = 0.022
STOP_LOSS_PERCENT = 0.007
