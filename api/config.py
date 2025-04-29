import os

# 🔐 API 키 (Railway 환경변수에서 자동 주입됨)
API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

# ⚙️ 기본 거래 설정
SYMBOL = "SOL_USDT"
QUANTITY = 1             # 수량
LEVERAGE = 20             # 설정 가능 시

# 🎯 익절/손절 조건 (% 기준)
TP_PERCENT = 1.0         # 익절 기준 (%)
SL_PERCENT = -0.5        # 손절 기준 (%)

# ⏱ 모니터링 주기 (초)
MONITOR_INTERVAL = 5
