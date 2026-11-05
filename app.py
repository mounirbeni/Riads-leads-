#!/usr/bin/env python3
"""موقع بحث عن riads في مراكش (Google Maps) مع الإيميلات والواتساب.

    pip install -r requirements.txt
    python3 app.py            ->  http://localhost:5000
"""
import hashlib
import hmac
import ipaddress
import os
import socket
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, make_response, request

import riads_leads as core
import store

app = Flask(__name__)
KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
TOKEN = hmac.new(("riads|" + ACCESS_CODE + "|" + KEY).encode(), b"ok", hashlib.sha256).hexdigest()


def authed():
    return bool(ACCESS_CODE) and hmac.compare_digest(request.cookies.get("auth", ""), TOKEN)


@app.before_request
def guard():
    if request.path.startswith("/api/") and request.path != "/api/login" and not authed():
        return jsonify(error="غير مسجّل الدخول"), 401


@app.post("/api/login")
def login():
    code = str((request.get_json(silent=True) or {}).get("code", ""))
    if not ACCESS_CODE or not hmac.compare_digest(code.encode(), ACCESS_CODE.encode()):
        time.sleep(1)  # إبطاء تخمين الكود
        return jsonify(error="الكود غير صحيح"), 401
    resp = make_response(jsonify(ok=True))
    resp.set_cookie("auth", TOKEN, max_age=30 * 86400, httponly=True, secure=request.is_secure, samesite="Lax")
    return resp


@app.post("/api/logout")
def logout():
    resp = make_response(jsonify(ok=True))
    resp.delete_cookie("auth")
    return resp
CELLS = list(core.grid_cells())


def public_url(u):
    """حماية SSRF: فقط http(s) إلى عناوين عامة."""
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


@app.get("/api/plan")
def plan():
    return jsonify(queries=len(core.QUERIES), cells=len(CELLS), configured=bool(KEY))


@app.post("/api/search")
def search():
    """طلب واحد قصير: استعلام واحد في خلية واحدة (يناسب حدود Vercel)."""
    if not KEY:
        return jsonify(error="GOOGLE_MAPS_API_KEY غير مضبوط على الخادم"), 500
    d = request.get_json(force=True)
    try:
        q, c = core.QUERIES[int(d["q"])], CELLS[int(d["cell"])]
        leads = [l for l in map(core.place_to_lead, core.search_cell(KEY, q, c)) if l]
        return jsonify(leads=leads)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 502


@app.post("/api/enrich")
def enrich():
    """يزور موقع رياض واحد ويستخرج الإيميل والواتساب ثم يعيد الصف النهائي."""
    lead = request.get_json(force=True)
    lead = {k: lead.get(k, "") for k in ("name", "address", "lat", "lon", "website", "maps_url",
                                          "rating", "reviews", "source")} | {
        "emails": [], "whatsapps": [], "phones": list(lead.get("phones") or [])}
    if lead["website"] and public_url(lead["website"]):
        try:
            core.enrich(lead)
        except Exception:  # noqa: BLE001
            pass
    return jsonify(row=core.finalize(lead))


@app.get("/api/leads")
def leads():
    try:
        return jsonify(rows=store.load())
    except Exception as e:  # noqa: BLE001
        return jsonify(error="تعذر قراءة قاعدة البيانات: " + str(e)), 500


@app.post("/api/save")
def save():
    """يدمج نتائج بحث جديدة مع المحفوظ دون أي تكرار."""
    incoming = (request.get_json(force=True) or {}).get("rows", [])
    try:
        rows, added, added_src = store.add_rows(store.load(), incoming)
        store.save(rows)
        return jsonify(rows=rows, added=added, added_sources=added_src)
    except Exception as e:  # noqa: BLE001
        return jsonify(error="تعذر الحفظ: " + str(e)), 500


@app.get("/")
def index():
    return PAGE if authed() else LOGIN


LOGIN = r"""<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Riads Leads - دخول</title>
<style>
:root{--bg:#faf6f0;--card:#fff;--ink:#2b2118;--pri:#b4532a;--line:#eadfce}
@media(prefers-color-scheme:dark){:root{--bg:#1c1713;--card:#272019;--ink:#f3e9dc;--line:#3a3026}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font-family:system-ui,"Segoe UI",Tahoma,sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;width:min(340px,calc(100vw - 32px));text-align:center}
input{width:100%;padding:12px;margin:12px 0;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:18px;text-align:center;box-sizing:border-box}
button{width:100%;background:var(--pri);color:#fff;border:0;border-radius:8px;padding:12px;font-size:16px;cursor:pointer}
#e{color:#c0392b;min-height:20px;margin-top:8px}
</style></head><body>
<form id="f"><h2>🏯 Riads Leads</h2><div>أدخل كود الدخول</div>
<input id="c" type="password" autocomplete="current-password" autofocus><button>دخول</button><div id="e"></div></form>
<script>
document.getElementById('f').onsubmit=async e=>{e.preventDefault();
const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:document.getElementById('c').value})});
if(r.ok)location.reload();else document.getElementById('e').textContent=(await r.json()).error;};
</script></body></html>"""

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
    <label><input type="checkbox" id="crawl" checked> استخراج الإيميل/الواتساب من مواقعهم</label>
  </div>
  <div class="row" style="margin-top:10px">
    <button id="go">ابدأ البحث</button>
    <button id="stop" class="sec" disabled>إيقاف</button>
    <button id="csv" class="sec" disabled>تحميل CSV</button>
    <button id="out" class="sec">خروج</button>
  </div>
  <div class="bar" id="bar"><i></i></div>
  <div id="msg"></div><div id="time" class="tag"></div><div id="err"></div>
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
const $=id=>document.getElementById(id);let ROWS=[];
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function render(){
  const q=$('q').value.toLowerCase(),fe=$('fE').checked,fw=$('fW').checked;
  const list=ROWS.filter(r=>(!fe||r.email)&&(!fw||r.whatsapp)&&(!q||JSON.stringify(r).toLowerCase().includes(q)));
  $('sN').textContent=ROWS.length;$('sE').textContent=ROWS.filter(r=>r.email).length;$('sW').textContent=ROWS.filter(r=>r.whatsapp).length;
  $('tb').innerHTML=list.length?list.map((r,i)=>`<tr>
    <td>${i+1}</td>
    <td><b>${esc(r.name)}</b>${srcs(r).some(s=>FRESH.has(s))?' <span class="tag" style="color:var(--ok)">● جديد</span>':''}<div class="tag">${esc(r.address)}</div>${r.rating?`<div class="tag">⭐ ${esc(r.rating)} (${esc(r.reviews)})</div>`:''}</td>
    <td>${esc(r.email)}<div class="tag">${esc(r.other_emails)}</div></td>
    <td dir="ltr">${esc(r.whatsapp)}<div class="tag">${esc(r.whatsapp_status)}</div></td>
    <td dir="ltr">${esc(r.phones)}</td>
    <td>${r.website?`<a href="${esc(r.website)}" target="_blank" rel="noopener">موقع</a>`:''} ${r.maps_url?`<a href="${esc(r.maps_url)}" target="_blank" rel="noopener">Maps</a>`:''}</td>
    <td class="act">${r.whatsapp_link?`<a class="wa" href="${esc(r.whatsapp_link)}" target="_blank" rel="noopener">واتساب</a>`:''}${r.email?`<a class="mail" href="mailto:${esc(r.email)}">إيميل</a>`:''}</td></tr>`).join('')
  :'<tr><td colspan="7" style="color:var(--mut)">لا توجد نتائج.</td></tr>';
}
['q','fE','fW'].forEach(i=>$(i).addEventListener('input',render));
function busy(b){$('go').disabled=b;$('stop').disabled=!b;$('bar').style.display=b?'block':'none'}
let stopFlag=false;
const LIMIT=120000,SEARCH_SHARE=0.6; // 2 دقيقة كحد أقصى للعملية كاملة
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(r.status===401){location.reload();return}const d=await r.json();if(!r.ok)throw new Error(d.error||r.status);return d}
async function pool(items,n,fn,deadline){let i=0;await Promise.all(Array.from({length:n},async()=>{while(i<items.length&&!stopFlag&&Date.now()<deadline){await fn(items[i++])}}))}
function plain(l){ // صف بدون زيارة الموقع (عند انتهاء الوقت)
  const mob=(l.phones||[]).find(p=>/^\+212[67]/.test(p));
  return {name:l.name,email:'',other_emails:'',whatsapp:mob||'',whatsapp_status:mob?'likely (mobile)':'',whatsapp_link:mob?'https://wa.me/'+mob.slice(1):'',phones:(l.phones||[]).join('; '),website:l.website,address:l.address,maps_url:l.maps_url||'',rating:l.rating,reviews:l.reviews,lat:l.lat,lon:l.lon,source:l.source}}
let STORED=[],FRESH=new Set();
const srcs=r=>String(r.source||'').split(';').map(s=>s.trim()).filter(Boolean);
async function loadStored(){
  try{const d=await (await fetch('/api/leads')).json();if(d.error)throw new Error(d.error);STORED=d.rows||[];ROWS=STORED;render();$('csv').disabled=!ROWS.length;
    $('msg').textContent=ROWS.length?`المحفوظ سابقاً: ${ROWS.length} رياض`:''}catch(e){$('err').textContent=e.message}
}
loadStored();
$('go').onclick=async()=>{
  $('err').textContent='';stopFlag=false;FRESH=new Set();busy(true);$('csv').disabled=true;const NEW=[];
  const KNOWN=new Set();STORED.forEach(r=>srcs(r).forEach(s=>KNOWN.add(s)));
  const t0=Date.now(),tick=setInterval(()=>{$('time').textContent=Math.min(120,Math.round((Date.now()-t0)/1000))+' / 120 ث'},500);
  try{
    const plan=await (await fetch('/api/plan')).json();
    if(!plan.configured)throw new Error('API key غير مضبوط على الخادم');
    const tasks=[];for(let q=0;q<plan.queries;q++)for(let c=0;c<plan.cells;c++)tasks.push({q,cell:c});
    const found=new Map();let done=0,failed=0,lastErr='';
    await pool(tasks,8,async t=>{
      try{(await post('/api/search',t)).leads.forEach(l=>found.set(l.source,l))}catch(e){failed++;lastErr=e.message}
      done++;$('msg').textContent=`بحث Google Maps ${done}/${tasks.length} - وُجد ${found.size} رياض`;
    },t0+LIMIT*SEARCH_SHARE);
    if(done&&failed===done)throw new Error(lastErr);
    const leads=[...found.values()].filter(l=>!KNOWN.has(l.source)),doCrawl=$('crawl').checked,seen=new Set();let n=0;
    const already=found.size-leads.length;
    if(doCrawl)await pool(leads,12,async l=>{
      try{NEW.push((await post('/api/enrich',l)).row);seen.add(l.source)}catch(e){}
      n++;$('msg').textContent=`رياضات جديدة: استخراج الإيميل والواتساب ${n}/${leads.length}`;
    },t0+LIMIT);
    leads.filter(l=>!seen.has(l.source)).forEach(l=>NEW.push(plain(l))); // ما لم يلحق الوقت بزيارته
    const left=leads.length-seen.size;
    $('msg').textContent='جاري الحفظ...';
    const saved=await post('/api/save',{rows:NEW});
    ROWS=STORED=saved.rows;FRESH=new Set(saved.added_sources);
    $('msg').textContent=(stopFlag?'تم الإيقاف':'انتهى')+`: أُضيف ${saved.added} رياض جديد، المجموع المحفوظ ${ROWS.length}`+(already?` (تخطّيت ${already} موجودة سابقاً)`:'')+(doCrawl&&left?` — ${left} لم يُفحص موقعها بسبب حد الوقت`:'');
    if(failed)$('err').textContent=`فشل ${failed} طلب بحث: ${lastErr}`;
  }catch(e){$('err').textContent=e.message}
  clearInterval(tick);busy(false);render();$('csv').disabled=!ROWS.length;
};
$('out').onclick=async()=>{await fetch('/api/logout',{method:'POST'});location.reload()};
$('stop').onclick=()=>{stopFlag=true;$('msg').textContent='جاري الإيقاف...'};
$('csv').onclick=()=>{
  const cols=Object.keys(ROWS[0]);
  const csv='﻿'+[cols.join(',')].concat(ROWS.map(r=>cols.map(c=>'"'+String(r[c]??'').replace(/"/g,'""')+'"').join(','))).join('\n');
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));a.download='riads_marrakech.csv';a.click();
};
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
