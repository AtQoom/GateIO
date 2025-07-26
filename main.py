#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 자동매매 서버  v6.12  –  PineScript v6.12 완전 호환
──────────────────────────────────────────────────────────
1) 5-단계 피라미딩 : 0.2 → 0.4 → 1.2 → 4.8 → 9.6 %
2) SL-Rescue(손절직전) : 150 % 가중, 최대 3 회
3) TP/SL 감소 : 15 s 마다 TP -0.002 %·SL -0.004 %(가중치 포함)
4) 심볼 가중치 : BTC 0.5 · ETH 0.6 · SOL 0.8 · PEPE/DOGE 1.2
5) 신호 타입 : main · engulfing · sl_rescue
"""

# ───────────────── 파이썬 표준/외부 모듈
import os, json, time, queue, asyncio, threading, logging, signal, sys
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
import pytz, websockets
from flask import Flask, request, jsonify
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
# ─────────────────────────────────────────

# ──────────────── 1. 로깅 ────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
LOG = logging.getLogger("v6.12")
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ──────────────── 2. 환경 / API ──────────
API_KEY    = os.getenv("API_KEY",    "")
API_SECRET = os.getenv("API_SECRET", "")
SETTLE     = "usdt"

cfg  = Configuration(key=API_KEY, secret=API_SECRET)
api  = FuturesApi(ApiClient(cfg))
app  = Flask(__name__)

# ──────────────── 3. 고정 파라미터 ──────
COOLDOWN_SEC = 10                            # 의도적 단축
KST          = pytz.timezone("Asia/Seoul")
ENTRY_RATIOS = [Decimal(x) for x in ("0.20 0.40 1.2 4.8 9.6".split())]
TP_LEVELS    = [Decimal(x) for x in ("0.005 0.0035 0.003 0.002 0.0015".split())]
SL_LEVELS    = [Decimal(x) for x in ("0.04 0.038 0.035 0.033 0.03".split())]

SYMBOL_CFG = {
    "BTC_USDT": {"cs":Decimal("0.0001"),"qs":Decimal("1"),"min":Decimal("1"),
                 "weight":Decimal("0.5")},
    "ETH_USDT": {"cs":Decimal("0.01"),  "qs":Decimal("1"),"min":Decimal("1"),
                 "weight":Decimal("0.6")},
    "SOL_USDT": {"cs":Decimal("1"),     "qs":Decimal("1"),"min":Decimal("1"),
                 "weight":Decimal("0.8")},
    "PEPE_USDT":{"cs":Decimal("10000000"),"qs":Decimal("1"),"min":Decimal("1"),
                 "weight":Decimal("1.2")},
    "DOGE_USDT":{"cs":Decimal("10"),    "qs":Decimal("1"),"min":Decimal("1"),
                 "weight":Decimal("1.2")},
}
# 축약/파생 심볼 매핑
MAP = {k.replace("_USDT",""):k for k in SYMBOL_CFG}
MAP |= {k.replace("_USDT","USDT"):k for k in SYMBOL_CFG}
MAP |= {k.replace("_USDT","USDT.P"):k for k in SYMBOL_CFG}

# ──────────────── 4. 전역 상태 ───────────
pos_lock   = threading.RLock()
sig_lock   = threading.RLock()
tpsl_lock  = threading.RLock()

pos_state  = {}      # 심볼별 포지션/카운터
recent_sig = {}      # 중복신호 필터
tpsl_store = {}      # TP/SL 저장

task_q     = queue.Queue(maxsize=100)
WORKER_CT  = max(2,min(6,os.cpu_count() or 2))

# ──────────────── 5. 유틸 함수 ───────────
def now(): return time.time()

def n_symbol(raw:str) -> str|None:
    s=raw.upper().strip()
    return MAP.get(s) or MAP.get(s.rstrip("PERP").rstrip(".P")) or None

def g_price(sym):
    try:
        t=api.list_futures_tickers(SETTLE,contract=sym); 
        return Decimal(str(t[0].last)) if t else Decimal("0")
    except: return Decimal("0")

def g_equity():
    try:
        acc=api.list_futures_accounts(SETTLE); return Decimal(str(acc.total))
    except: return Decimal("0")

# ───────────── 6. TP/SL 관리 ─────────────
def tp_sl_default(sym):
    w=SYMBOL_CFG[sym]["weight"]
    return Decimal("0.006")*w, Decimal("0.04")*w, now()

def tp_sl_set(sym,entry,tp,sl):
    with tpsl_lock:
        tpsl_store.setdefault(sym,{})[entry]={"tp":tp,"sl":sl,"t":now(),"et":now()}

def tp_sl_get(sym,entry=None):
    with tpsl_lock:
        d=tpsl_store.get(sym)
        if not d: return tp_sl_default(sym)
        if entry and entry in d: r=d[entry]
        else: r=d[max(d)]
    secs=now()-r["et"]; per=int(secs/15)
    w=SYMBOL_CFG[sym]["weight"]
    tp=max(Decimal("0.0012"), r["tp"]-per*((Decimal("0.002")/100)*w))
    sl=max(Decimal("0.0009"), r["sl"]-per*((Decimal("0.004")/100)*w))
    return tp,sl,r["et"]

def tp_sl_clear(sym): 
    with tpsl_lock: tpsl_store.pop(sym,None)

# ───────────── 7. 중복 신호 체크 ─────────
def dup(sig):
    with sig_lock:
        t=now(); sid=sig.get("id","")
        k=f"{sig['symbol']}_{sig['side']}"
        if sid and sid in recent_sig and t-recent_sig[sid]<5: return True
        if k in recent_sig and t-recent_sig[k]<COOLDOWN_SEC: return True
        recent_sig[k]=recent_sig[sid]=t
        # 5분 초과 제거
        for x in list(recent_sig):
            if t-recent_sig[x]>300: recent_sig.pop(x,None)
        return False

# ───────────── 8. 포지션 상태 갱신 ───────
def upd_pos(sym):
    with pos_lock:
        try:
            p=api.get_position(SETTLE,sym); sz=Decimal(str(p.size))
            if sz!=0:
                ps=pos_state.get(sym,{"sl_cnt":0})
                pos_state[sym]={"side":"buy" if sz>0 else "sell",
                                "size":abs(sz),"price":Decimal(str(p.entry_price)),
                                "entry":ps.get("entry",0),
                                "sl_cnt":ps.get("sl_cnt",0),
                                "et":ps.get("et",now())}
            else:
                pos_state[sym]={"side":None,"size":0,"entry":0,"sl_cnt":0}
                tp_sl_clear(sym)
        except: pos_state[sym]={"side":None,"size":0,"entry":0,"sl_cnt":0}

# ───────────── 9. SL-Rescue 판별 ─────────
def sl_rescue(sym):
    p=pos_state.get(sym,{}); 
    if not p or p["size"]==0 or p["entry"]>=5 or p["sl_cnt"]>=3: return False
    price=g_price(sym)
    if price==0: return False
    tp,sl,_=tp_sl_get(sym)
    side=p["side"]; avg=p["price"]
    sl_price=avg*(1-sl) if side=="buy" else avg*(1+sl)
    near= abs(price-sl_price)<=avg*Decimal("0.0005")
    underwater=price<avg if side=="buy" else price>avg
    return near and underwater

# ───────────── 10. 수량 계산 ─────────────
def qty_calc(sym,sig):
    upd_pos(sym)
    p=pos_state[sym]; idx=p["entry"]
    if idx>=5: return Decimal("0")
    ratio=ENTRY_RATIOS[idx]/Decimal("100")
    val=g_equity()*ratio
    if sl_rescue(sym): val*=Decimal("1.5")
    price=g_price(sym); cs=SYMBOL_CFG[sym]["cs"]
    raw=val/(price*cs)
    step=SYMBOL_CFG[sym]["qs"]; qty=(raw/step).quantize(0,ROUND_DOWN)*step
    return max(qty,SYMBOL_CFG[sym]["min"])

# ───────────── 11. 주문/청산 ─────────────
def order(sym,side,qty,is_sl=False):
    if qty<=0: return False
    try:
        sz=float(qty) if side=="buy" else -float(qty)
        api.create_futures_order(SETTLE,FuturesOrder(contract=sym,size=sz,price="0",tif="ioc"))
        p=pos_state.get(sym,{"entry":0,"sl_cnt":0}); new_entry=p["entry"]+1
        pos_state[sym]={"side":side,"size":p.get("size",0)+abs(qty),"price":g_price(sym),
                        "entry":new_entry,"sl_cnt":p["sl_cnt"]+(1 if is_sl else 0),"et":now()}
        # TP/SL 저장( SL-Rescue 는 기존 유지 )
        if not is_sl and new_entry<=5:
            tp=TP_LEVELS[new_entry-1]*SYMBOL_CFG[sym]["weight"]
            sl=SL_LEVELS[new_entry-1]*SYMBOL_CFG[sym]["weight"]
            tp_sl_set(sym,tp,sl,new_entry)
        LOG.info(f"[ENTRY] {sym} {side.upper()} {qty} (#{new_entry}/5, SL:{is_sl})")
        return True
    except Exception as e:
        LOG.error(f"[ORDER_FAIL] {sym} {e}")
        return False

def close(sym,reason="exit"):
    try:
        api.create_futures_order(SETTLE,FuturesOrder(contract=sym,size=0,price="0",tif="ioc",close=True))
        LOG.info(f"[CLOSE] {sym} ({reason})")
        pos_state[sym]={"side":None,"size":0,"entry":0,"sl_cnt":0}
        tp_sl_clear(sym)
    except Exception as e:
        LOG.error(f"[CLOSE_FAIL] {sym} {e}")

# ───────────── 12. 워커 스레드 ───────────
def worker(i):
    while not shutdown_event.is_set():
        try:
            d=task_q.get(timeout=1)
            sym=n_symbol(d["symbol"]); side=d["side"]
            is_sl = d["signal"]=="sl_rescue"
            qty=qty_calc(sym,d)
            if qty: order(sym,"buy" if side=="long" else "sell",qty,is_sl)
            task_q.task_done()
        except queue.Empty: pass
        except Exception as e:
            LOG.error(f"[WORKER-{i}] {e}")

# ───────────── 13. TP/SL 실시간 체크 ─────
async def ws_loop():
    uri="wss://fx-ws.gateio.ws/v4/ws/usdt"
    subs=list(SYMBOL_CFG)
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"time":int(now()),"channel":"futures.tickers",
                                          "event":"subscribe","payload":subs}))
                while True:
                    msg=await asyncio.wait_for(ws.recv(),timeout=40)
                    data=json.loads(msg).get("result"); 
                    if not data: continue
                    for t in data: tp_sl_watch(t)
        except Exception as e:
            LOG.error(f"[WS] {e}")
        await asyncio.sleep(5)

def tp_sl_watch(t):
    sym=t.get("contract"); price=Decimal(str(t.get("last","0")))
    if sym not in pos_state or pos_state[sym]["size"]==0: return
    p=pos_state[sym]; tp,sl,_=tp_sl_get(sym,p["entry"])
    side=p["side"]; avg=p["price"]
    if side=="buy":
        if price>=avg*(1+tp): close(sym,"TP")
        elif price<=avg*(1-sl): close(sym,"SL")
    else:
        if price<=avg*(1-tp): close(sym,"TP")
        elif price>=avg*(1+sl): close(sym,"SL")

# ───────────── 14. Flask 라우트 ──────────
@app.route("/",methods=["POST"]); app.route("/webhook",methods=["POST"])
def hook():
    try:
        data=request.get_json(force=True)
        if not data or dup(data): return jsonify({"status":"ignored"}),200
        if data["action"]=="exit":
            close(n_symbol(data["symbol"]),data.get("reason","signal")); return {"status":"closed"}
        task_q.put_nowait(data); return {"status":"queued","q":task_q.qsize()}
    except Exception as e:
        return {"error":str(e)},500

@app.route("/status")
def status():
    return {
        "equity": float(g_equity()),
        "positions": {s:pos_state[s] for s in pos_state if pos_state[s]["size"]},
        "queue": task_q.qsize()
    }

# ───────────── 15. 서비스 시작 ───────────
def main():
    for s in SYMBOL_CFG: upd_pos(s)
    for i in range(WORKER_CT):
        threading.Thread(target=worker,args=(i,),daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(ws_loop()),daemon=True).start()
    port=int(os.getenv("PORT",8080)); LOG.info(f"RUN on :{port}")
    app.run("0.0.0.0",port,debug=False,threaded=True)

def stop(sig,frame):
    shutdown_event.set(); sys.exit(0)
signal.signal(signal.SIGINT,stop); signal.signal(signal.SIGTERM,stop)

if __name__=="__main__": main()
