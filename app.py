#!/usr/bin/env python3
"""موقع بحث عن riads في مراكش (Google Maps) مع الإيميلات والواتساب.

    pip install -r requirements.txt
    python3 app.py            ->  http://localhost:5000
"""
import os
import threading
import uuid

from flask import Flask, jsonify, request

import riads_leads as core

app = Flask(__name__)
JOBS = {}  # job_id -> {status, message, rows, error, stop}


def run_job(job_id, key, crawl):
    job = JOBS[job_id]
    try:
        job["rows"] = core.collect(
            key, crawl,
            progress=lambda m: job.update(message=m),
            stop=lambda: job["stop"],
        )
        job["status"] = "stopped" if job["stop"] else "done"
        job["message"] = f"انتهى: {len(job['rows'])} رياض"
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "error", str(e)


@app.post("/api/start")
def start():
    data = request.get_json(force=True)
    key = (data.get("key") or os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip()
    if not key:
        return jsonify(error="أدخل Google Maps API key"), 400
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "message": "بدأ...", "rows": [], "error": None, "stop": False}
    threading.Thread(target=run_job, args=(job_id, key, bool(data.get("crawl", True))), daemon=True).start()
    return jsonify(job=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify(error="not found"), 404
    return jsonify(status=j["status"], message=j["message"], error=j["error"],
                   rows=j["rows"] if j["status"] != "running" else [])


@app.post("/api/stop/<job_id>")
def stop(job_id):
    if job_id in JOBS:
        JOBS[job_id]["stop"] = True
    return jsonify(ok=True)


@app.get("/")
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Riads Leads - مراكش</title>
<style>
:root{--bg:#faf6f0;--card:#fff;--ink:#2b2118;--mut:#7a6a5a;--pri:#b4532a;--ok:#1f9d55;--line:#eadfce}
@media(prefers-color-scheme:dark){:root{--bg:#1c1713;--card:#272019;--ink:#f3e9dc;--mut:#b5a493;--line:#3a3026}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,"Segoe UI",Tahoma,sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:16px}
h1{margin:8px 0 2px}p.sub{color:var(--mut);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=text],input[type=password],input[type=search]{flex:1;min-width:220px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:15px}
button,.btn{background:var(--pri);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:15px;cursor:pointer;text-decoration:none;display:inline-block}
button.sec{background:transparent;color:var(--ink);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:default}
.wa{background:#25d366}.mail{background:#2563eb}
#msg{color:var(--mut);margin-top:10px}#err{color:#c0392b;margin-top:10px;white-space:pre-wrap}
.stats{display:flex;gap:12px;flex-wrap:wrap}.stat{flex:1;min-width:120px;text-align:center}.stat b{display:block;font-size:26px;color:var(--pri)}
.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px;border-bottom:1px solid var(--line);text-align:start;vertical-align:top}th{position:sticky;top:0;background:var(--card)}
td.act a{margin:2px;padding:4px 10px;font-size:13px;border-radius:6px;color:#fff;text-decoration:none;display:inline-block}
.tag{font-size:11px;color:var(--mut)}label{display:flex;gap:6px;align-items:center}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:10px;display:none}.bar i{display:block;height:100%;width:40%;background:var(--pri);animation:m 1.2s infinite}
@keyframes m{0%{margin-inline-start:-40%}100%{margin-inline-start:100%}}
</style>
</head>
<body><div class="wrap">
<h1>🏯 Riads Leads — مراكش</h1>
<p class="sub">كل الـ riads في مراكش فقط من Google Maps، مع الإيميلات والواتساب.</p>

<div class="card">
  <div class="row">
    <input id="key" type="password" placeholder="Google Maps API key (Places API New)" autocomplete="off">
    <label><input type="checkbox" id="crawl" checked> استخراج الإيميل/الواتساب من مواقعهم</label>
  </div>
  <div class="row" style="margin-top:10px">
    <button id="go">ابدأ البحث</button>
    <button id="stop" class="sec" disabled>إيقاف</button>
    <button id="csv" class="sec" disabled>تحميل CSV</button>
  </div>
  <div class="bar" id="bar"><i></i></div>
  <div id="msg"></div><div id="err"></div>
</div>

<div class="card stats">
  <div class="stat"><b id="sN">0</b>riads</div>
  <div class="stat"><b id="sE">0</b>بإيميل</div>
  <div class="stat"><b id="sW">0</b>بواتساب</div>
</div>

<div class="card">
  <div class="row" style="margin-bottom:10px">
    <input type="search" id="q" placeholder="ابحث في النتائج...">
    <label><input type="checkbox" id="fE"> فقط بإيميل</label>
    <label><input type="checkbox" id="fW"> فقط بواتساب</label>
  </div>
  <div class="tablewrap"><table>
    <thead><tr><th>#</th><th>الاسم</th><th>الإيميل</th><th>واتساب</th><th>الهاتف</th><th>الموقع</th><th>تواصل</th></tr></thead>
    <tbody id="tb"><tr><td colspan="7" style="color:var(--mut)">لا توجد نتائج بعد.</td></tr></tbody>
  </table></div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);let ROWS=[],job=null,timer=null;
try{$('key').value=localStorage.getItem('gkey')||''}catch(e){}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function render(){
  const q=$('q').value.toLowerCase(),fe=$('fE').checked,fw=$('fW').checked;
  const list=ROWS.filter(r=>(!fe||r.email)&&(!fw||r.whatsapp)&&(!q||JSON.stringify(r).toLowerCase().includes(q)));
  $('sN').textContent=ROWS.length;$('sE').textContent=ROWS.filter(r=>r.email).length;$('sW').textContent=ROWS.filter(r=>r.whatsapp).length;
  $('tb').innerHTML=list.length?list.map((r,i)=>`<tr>
    <td>${i+1}</td>
    <td><b>${esc(r.name)}</b><div class="tag">${esc(r.address)}</div>${r.rating?`<div class="tag">⭐ ${esc(r.rating)} (${esc(r.reviews)})</div>`:''}</td>
    <td>${esc(r.email)}<div class="tag">${esc(r.other_emails)}</div></td>
    <td dir="ltr">${esc(r.whatsapp)}<div class="tag">${esc(r.whatsapp_status)}</div></td>
    <td dir="ltr">${esc(r.phones)}</td>
    <td>${r.website?`<a href="${esc(r.website)}" target="_blank" rel="noopener">موقع</a>`:''} ${r.maps_url?`<a href="${esc(r.maps_url)}" target="_blank" rel="noopener">Maps</a>`:''}</td>
    <td class="act">${r.whatsapp_link?`<a class="wa" href="${esc(r.whatsapp_link)}" target="_blank" rel="noopener">واتساب</a>`:''}${r.email?`<a class="mail" href="mailto:${esc(r.email)}">إيميل</a>`:''}</td></tr>`).join('')
  :'<tr><td colspan="7" style="color:var(--mut)">لا توجد نتائج.</td></tr>';
}
['q','fE','fW'].forEach(i=>$(i).addEventListener('input',render));
function busy(b){$('go').disabled=b;$('stop').disabled=!b;$('bar').style.display=b?'block':'none'}
$('go').onclick=async()=>{
  $('err').textContent='';const key=$('key').value.trim();
  try{localStorage.setItem('gkey',key)}catch(e){}
  const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,crawl:$('crawl').checked})});
  const d=await r.json();if(!r.ok){$('err').textContent=d.error;return}
  job=d.job;busy(true);ROWS=[];render();$('csv').disabled=true;
  timer=setInterval(poll,2000);
};
async function poll(){
  const d=await (await fetch('/api/status/'+job)).json();$('msg').textContent=d.message||'';
  if(d.status!=='running'){clearInterval(timer);busy(false);
    if(d.error)$('err').textContent=d.error;
    ROWS=d.rows||[];render();$('csv').disabled=!ROWS.length}
}
$('stop').onclick=()=>{fetch('/api/stop/'+job,{method:'POST'});$('msg').textContent='جاري الإيقاف...'};
$('csv').onclick=()=>{
  const cols=Object.keys(ROWS[0]);
  const csv='﻿'+[cols.join(',')].concat(ROWS.map(r=>cols.map(c=>'"'+String(r[c]??'').replace(/"/g,'""')+'"').join(','))).join('\n');
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));a.download='riads_marrakech.csv';a.click();
};
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
