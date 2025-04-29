from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("🚨 Webhook received:", data)
    
    # 예시: long 진입
    if data.get("signal") == "entry" and data.get("position") == "long":
        # 실제 거래 API 연결은 여기에 작성
        print("📈 Long 진입 시그널 실행")
    
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
