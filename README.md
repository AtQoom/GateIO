# MTF 자동매매 전략 (TradingView + Webhook 연동)

이 저장소는 트레이딩뷰 파인스크립트 전략과 자동매매 연동을 위한 구조를 담고 있습니다.

## 📌 전략 개요

- 다중 시간 프레임 필터 (1분, 3분, 5분)
- RSI + 볼린저밴드 + VWAP + EMA 조건
- 거래 강도에 따라 TP/SL 유동 설정
- Webhook 알림 내장 (자동매매 서버 연동)

## 🔗 구성 파일

| 파일 | 설명 |
|------|------|
| `pine/mtf-autobot-strategy.pine` | 파인스크립트 전략 본문 |
| `.gitignore` | 깃 관리 제외 파일 |
| `README.md` | 설명 문서 |

## 🛠️ 알림 포맷 예시 (Webhook)

```json
{
  "signal": "entry",
  "position": "long",
  "ticker": "{{ticker}}",
  "price": "{{close}}",
  "time": "{{time}}"
}
