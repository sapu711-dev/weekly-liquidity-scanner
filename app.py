from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pathlib import Path
import json, os
from datetime import datetime
from zoneinfo import ZoneInfo
import threading, logging

app = FastAPI(title="WLS-213 Live Scanner")
STATE = Path(os.getenv("STATE_FILE", "/tmp/wls213_status.json"))
TZ = ZoneInfo("Asia/Kolkata")

def status():
    try:
        data = json.loads(STATE.read_text())
    except Exception:
        data = {"running": False, "connected": False, "status": "Not configured", "last_scan": None, "buy_count": 0, "sell_count": 0, "signals": []}
    data["server_time"] = datetime.now(TZ).isoformat()
    return data

@app.on_event("startup")
def start_scanner_thread():
    # Run the worker in this same service so its local status file is shared with the dashboard.
    if os.getenv("WLS_DISABLE_WORKER", "false").lower() not in ("1", "true", "yes"):
        def run_worker():
            try:
                from worker import scan_loop
                scan_loop()
            except Exception as exc:
                logging.exception("WLS-213 worker stopped")
                try:
                    STATE.parent.mkdir(parents=True, exist_ok=True)
                    STATE.write_text(json.dumps({
                        "running": False, "connected": False,
                        "status": f"ERROR: {exc}", "signals": [],
                        "buy_count": 0, "sell_count": 0
                    }, ensure_ascii=False))
                except Exception:
                    pass
        threading.Thread(target=run_worker, daemon=True, name="wls213-worker").start()

@app.get("/health")
def health():
    return {"ok": True, "service": "WLS-213", "server_time": datetime.now(TZ).isoformat()}

@app.get("/api/status")
def api_status():
    return status()

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML
HTML = r"""<!doctype html><html lang="bn"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WLS-213</title>
<style>body{font-family:Arial,sans-serif;margin:0;background:#f3f5f7;color:#17202a}.wrap{max-width:900px;margin:auto;padding:16px}.top{background:#172b4d;color:white;padding:18px;border-radius:14px}.status{font-size:14px;margin-top:8px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}.panel{background:white;padding:12px;border-radius:12px;min-width:0}.buy{border-top:5px solid #15803d}.sell{border-top:5px solid #b91c1c}.item{border:1px solid #ddd;border-radius:9px;padding:10px;margin:8px 0;overflow-wrap:anywhere}.muted{color:#64748b;font-size:13px}@media(max-width:600px){.grid{grid-template-columns:1fr}}button{padding:10px 16px;border:0;border-radius:8px;background:#dbeafe}</style>
<div class="wrap"><div class="top"><h2>WLS-213 — Weekly Liquidity Scanner</h2><div id="state" class="status">সার্ভারের অবস্থা দেখা হচ্ছে…</div><p class="muted">এটি কেবল সার্ভার-স্ট্যাটাস ড্যাশবোর্ড। এখানে ON/OFF কন্ট্রোল দেখানো হয় না যতক্ষণ না নিরাপদ authenticated control যুক্ত হয়।</p></div>
<div class="grid"><section class="panel buy"><h3>BUY <span id="bc">0</span></h3><div id="buys">কোনো সিগন্যাল নেই</div></section><section class="panel sell"><h3>SELL <span id="sc">0</span></h3><div id="sells">কোনো সিগন্যাল নেই</div></section></div>
<p class="muted">সার্ভার সময়: <span id="clock">—</span></p></div>
<script>
function esc(v){return String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function cards(rows){if(!rows||!rows.length)return '<p>কোনো সিগন্যাল নেই</p>';return rows.map(x=>`<div class="item"><b>${esc(x.symbol)} — ${esc(x.signal)}</b><br>সময়: ${esc(x.time)}<br>Close: ₹${esc(x.close)}<br>PWL: ${esc(x.PWL)} | PWH: ${esc(x.PWH)}<br>Swing: ${esc(x.swing_level)}</div>`).join('')}
async function refresh(){try{let r=await fetch('/api/status',{cache:'no-store'});let d=await r.json();document.getElementById('state').textContent=`অবস্থা: ${d.status||'অজানা'} | সংযোগ: ${d.connected?'যুক্ত':'যুক্ত নয়'} | স্ক্যান: ${d.running?'চলছে':'চলছে না'} | শেষ স্ক্যান: ${d.last_scan||'এখনও নয়'}`;document.getElementById('bc').textContent=d.buy_count||0;document.getElementById('sc').textContent=d.sell_count||0;document.getElementById('buys').innerHTML=cards((d.signals||[]).filter(x=>x.signal==='BUY'));document.getElementById('sells').innerHTML=cards((d.signals||[]).filter(x=>x.signal==='SELL'));document.getElementById('clock').textContent=d.server_time||'—'}catch(e){document.getElementById('state').textContent='সার্ভারে যোগাযোগ হচ্ছে না';}}
refresh();setInterval(refresh,15000);
</script></html>"""
