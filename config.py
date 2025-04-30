# config.py

# 일반 설정
SYMBOL = "SOL_USDT"
POSITION_MODE = "isolated"         # 또는 "isolated""cross"  
LEVERAGE = 12

# 위험 관리
RISK_PER_TRADE_PCT = 10
MAX_POSITION_SIZE = 100

# TP / SL (% 단위)
TP_PERCENT = 2.2
SL_PERCENT = 0.7

# 트레일링 스탑
TRAIL_OFFSET_PCT = 0.6
TRAIL_STEP_PCT = 1.2
