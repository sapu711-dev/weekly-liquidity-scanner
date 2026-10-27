"""Signal-only worker. No order placement. Requires server-side Kotak credentials."""
import os, time, json, logging, csv, requests
from pathlib import Path
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from wls213_strategy import scan_candles

TZ=ZoneInfo("Asia/Kolkata")
STATE=Path(os.getenv("STATE_FILE","/tmp/wls213_status.json"))
LOG=logging.getLogger("wls213")
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))

def write_state(**kwargs):
    old={}
    try: old=json.loads(STATE.read_text())
    except Exception: pass
    old.update(kwargs); old["updated_at"]=datetime.now(TZ).isoformat()
    STATE.parent.mkdir(parents=True,exist_ok=True); STATE.write_text(json.dumps(old,ensure_ascii=False,default=str))

def send_telegram(message):
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat: return False
    r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",json={"chat_id":chat,"text":message},timeout=15)
    r.raise_for_status(); return True

def market_open(now):
    return now.weekday()<5 and dtime(9,15)<=now.time()<=dtime(15,30)

def authenticate():
    from neo_api_client import NeoAPI
    consumer=os.getenv("KOTAK_CONSUMER_KEY","").strip()
    mobile=os.getenv("KOTAK_MOBILE_NUMBER","").strip()
    ucc=os.getenv("KOTAK_UCC","").strip()
    mpin=os.getenv("KOTAK_MPIN","").strip()
    totp_secret=os.getenv("KOTAK_TOTP_SECRET","").strip()
    if not all([consumer,mobile,ucc,mpin,totp_secret]):
        raise RuntimeError("Set KOTAK_CONSUMER_KEY, KOTAK_MOBILE_NUMBER, KOTAK_UCC, KOTAK_MPIN and KOTAK_TOTP_SECRET as private server environment variables.")
    if len(mobile)==10 and mobile.isdigit(): mobile="+91"+mobile
    import pyotp
    client=NeoAPI(consumer_key=consumer,environment="prod")
    login=client.totp_login(mobile_number=mobile,ucc=ucc,totp=pyotp.TOTP(totp_secret).now())
    def status(x):
        if not isinstance(x,dict): return ""
        d=x.get("data")
        return str(d.get("status","") if isinstance(d,dict) else x.get("status","")).lower()
    if "error" in (login or {}) or status(login) not in ("success","ok"):
        raise RuntimeError("Kotak TOTP login was not confirmed; check server secrets and account access.")
    validated=client.totp_validate(mpin=mpin)
    if "error" in (validated or {}) or status(validated) not in ("success","ok"):
        raise RuntimeError("Kotak MPIN validation was not confirmed.")
    return client

def load_symbols(client):
    # Generate mapping from live scrip masters, following the user's notebook Step 2.
    import pandas as pd
    from io import StringIO
    url=client.scrip_master(exchange_segment="nse_fo")
    if not isinstance(url,str) or not url.startswith("http"): raise RuntimeError("Kotak F&O scrip-master URL unavailable")
    fo=pd.read_csv(StringIO(requests.get(url,timeout=45).text),skipinitialspace=True)
    fo.columns=fo.columns.astype(str).str.strip()
    needed={"pExchSeg","pInstType","pSymbolName"}
    if not needed.issubset(fo.columns): raise RuntimeError("Unexpected F&O scrip master columns")
    for c in needed: fo[c]=fo[c].astype(str).str.strip()
    names=sorted(fo.loc[(fo.pExchSeg.str.lower()=="nse_fo")&(fo.pInstType.str.upper()=="FUTSTK"),"pSymbolName"].str.upper().unique())
    cm_url=url.replace("nse_fo.csv","nse_cm.csv")
    if cm_url==url: raise RuntimeError("Could not derive NSE cash scrip-master URL")
    cm=pd.read_csv(StringIO(requests.get(cm_url,timeout=45).text),skipinitialspace=True)
    cm.columns=cm.columns.astype(str).str.strip()
    for c in ["pExchSeg","pTrdSymbol","pSymbol","pSymbolName"]:
        if c not in cm.columns: raise RuntimeError("Unexpected cash scrip master columns")
        cm[c]=cm[c].astype(str).str.strip()
    eq=cm[(cm.pExchSeg.str.lower()=="nse_cm")&cm.pTrdSymbol.str.endswith("-EQ")&cm.pSymbol.str.fullmatch(r"\d+",na=False)]
    mapped=eq[eq.pSymbolName.str.upper().isin(names)][["pSymbolName","pSymbol","pTrdSymbol"]].drop_duplicates("pSymbolName")
    rows=[{"symbol":r.pSymbolName.upper(),"token":str(r.pSymbol),"trade_symbol":r.pTrdSymbol} for r in mapped.itertuples(index=False)]
    if not rows: raise RuntimeError("No cash EQ tokens mapped from live scrip masters")
    Path("cash_stock_map.csv").write_text("symbol,token,trade_symbol\\n"+"".join(f"{x['symbol']},{x['token']},{x['trade_symbol']}\\n" for x in rows))
    return rows

def candles_for(client,row):
    start=(date.today()-timedelta(days=35)).isoformat(); end=date.today().isoformat()
    response=client.historical_data(neosymbol=f"nse_cm|{row['token']}",interval="15min",from_date=start,to_date=end)
    if not isinstance(response,dict) or str(response.get("status","")).upper()!="SUCCESS":
        raise RuntimeError(f"Historical API response not successful for {row['symbol']}")
    data=response.get("data",[])
    return data.get("candles",[]) if isinstance(data,dict) else data if isinstance(data,list) else []

def scan_loop():
    client=authenticate()
    symbols=load_symbols(client)
    if len(symbols)<150: raise RuntimeError(f"Only {len(symbols)} stock tokens mapped; expected broad NSE stock universe. Stopping rather than claim complete.")
    logging.info("Mapped %d NSE cash symbols",len(symbols))
    sent=set()
    while True:
        now=datetime.now(TZ)
        if market_open(now):
            write_state(running=True,connected=True,status="Scanning live market",last_scan=None,signals=[],buy_count=0,sell_count=0,universe_count=len(symbols))
            found=[]
            for i,row in enumerate(symbols,1):
                try:
                    cs=candles_for(client,row)
                    out=scan_candles(cs,row["symbol"])
                    if not out.empty:
                        for r in out.to_dict("records"):
                            item={"time":str(r["datetime_ist"]),"symbol":r["symbol"],"signal":r["signal"],"close":float(r["close"]),"PWL":None if r["pwl"]!=r["pwl"] else float(r["pwl"]),"PWH":None if r["pwh"]!=r["pwh"] else float(r["pwh"]),"swing_level":None if r["swing_level"]!=r["swing_level"] else float(r["swing_level"])}
                            found.append(item)
                            key=f"{item['symbol']}|{item['signal']}|{item['time']}"
                            if key not in sent:
                                try: send_telegram(f"WLS-213 {item['signal']} {item['symbol']}\\nTime: {item['time']}\\nClose: {item['close']}\\nPWL: {item['PWL']} | PWH: {item['PWH']}\\nSwing: {item['swing_level']}")
                                except Exception: logging.exception("Telegram alert failed")
                                sent.add(key)
                except Exception as e:
                    logging.warning("Scan failed for %s: %s",row["symbol"],e)
                if i%20==0: time.sleep(1.2)
            found.sort(key=lambda x:x["time"],reverse=True)
            buys=sum(x["signal"]=="BUY" for x in found); sells=sum(x["signal"]=="SELL" for x in found)
            write_state(running=True,connected=True,status="Scan completed",last_scan=datetime.now(TZ).isoformat(),signals=found[:250],buy_count=buys,sell_count=sells,universe_count=len(symbols))
        else:
            write_state(running=False,connected=True,status="Waiting for NSE market hours",universe_count=len(symbols))
        time.sleep(max(120,int(os.getenv("SCAN_INTERVAL_SECONDS","300"))))

if __name__=="__main__":
    try: scan_loop()
    except Exception as e:
        logging.exception("Worker stopped")
        write_state(running=False,connected=False,status=f"ERROR: {e}")
        raise
