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
import base64

import store
from icons import ICONS

app = Flask(__name__)
KEYS = [k.strip() for name in ("GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_API_KEY_2", "GOOGLE_MAPS_API_KEYS")
        for k in os.environ.get(name, "").split(",") if k.strip()]
KEYS = list(dict.fromkeys(KEYS))  # بدون تكرار
KEY = KEYS[0] if KEYS else ""
EXHAUSTED = {}  # مفتاح -> وقت العودة (بعد خطأ الحصة)
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
    return jsonify(queries=len(core.QUERIES), cells=len(CELLS), configured=bool(KEYS), keys=len(KEYS))


@app.post("/api/search")
def search():
    """طلب واحد قصير: استعلام واحد في خلية واحدة. يوزّع الحمل على المفاتيح ويتحول لآخر عند انتهاء حصة أحدها."""
    if not KEYS:
        return jsonify(error="GOOGLE_MAPS_API_KEY غير مضبوط على الخادم"), 500
    d = request.get_json(force=True)
    try:
        q, c = core.QUERIES[int(d["q"])], CELLS[int(d["cell"])]
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 400
    now, start = time.time(), (int(d["q"]) + int(d["cell"])) % len(KEYS)
    order = [KEYS[(start + i) % len(KEYS)] for i in range(len(KEYS))]
    order = [k for k in order if EXHAUSTED.get(k, 0) < now] or order  # الأحياء أولاً
    last = None
    for key in order:
        try:
            places = list(core.search_cell(key, q, c))
            leads = [l for l in map(core.place_to_lead, places) if l]
            return jsonify(leads=leads, raw=len(places))
        except Exception as e:  # noqa: BLE001
            last = e
            if "Google API 429" in str(e):
                EXHAUSTED[key] = now + 600  # استرح 10 دقائق
                continue
            break
    return jsonify(error=str(last)), (429 if "Google API 429" in str(last) else 502)


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


MANIFEST = {
    "name": "Riads Leads - مراكش", "short_name": "Riads", "description": "رياضات مراكش: إيميلات وواتساب",
    "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
    "lang": "ar", "dir": "rtl", "background_color": "#b4532a", "theme_color": "#b4532a",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}
SW = """self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{});  // بدون كاش: البيانات دائماً من الخادم
"""


@app.get("/manifest.webmanifest")
def manifest():
    resp = jsonify(MANIFEST)
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


@app.get("/sw.js")
def sw():
    return SW, 200, {"Content-Type": "application/javascript", "Cache-Control": "no-cache", "Service-Worker-Allowed": "/"}


def _icon(key):
    return base64.b64decode(ICONS[key]), 200, {"Content-Type": "image/png", "Cache-Control": "public, max-age=86400"}


@app.get("/icon-192.png")
def icon192():
    return _icon("192")


@app.get("/icon-512.png")
def icon512():
    return _icon("512")


@app.get("/apple-touch-icon.png")
def apple_icon():
    return _icon("180")


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
        return jsonify(saved=store.save_add(incoming))
    except Exception as e:  # noqa: BLE001
        return jsonify(error="تعذر الحفظ: " + str(e)), 500


@app.post("/api/delete")
def delete():
    """يحذف رياضاً من القاعدة بمعرّف المصدر (source)."""
    gone = {str(s) for s in (request.get_json(force=True) or {}).get("sources", [])}
    try:
        return jsonify(rows=store.delete(gone))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 500


@app.get("/")
def index():
    return PAGE if authed() else LOGIN


LOGIN = r"""<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Riads Leads</title>
<meta name="theme-color" content="#b4532a"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
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
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<title>Riads Leads</title>
<meta name="theme-color" content="#b4532a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Riads">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" type="image/png" href="/icon-192.png">
<style>
:root{--bg:#f6f0e8;--card:#fff;--ink:#2b2118;--mut:#85766a;--pri:#b4532a;--pri2:#c4623a;--ok:#1f9d55;--line:#eadfce;--chip:#efe6da;--sat:env(safe-area-inset-top);--sab:env(safe-area-inset-bottom)}
@media(prefers-color-scheme:dark){:root{--bg:#161210;--card:#231d18;--ink:#f3e9dc;--mut:#a8998b;--line:#352c24;--chip:#2e251e}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font-family:-apple-system,system-ui,"Segoe UI",Tahoma,sans-serif;font-size:16px;overscroll-behavior:none;-webkit-user-select:none;user-select:none}
input,button{font:inherit}
#app{display:flex;flex-direction:column;height:100%;max-width:640px;margin:0 auto}
.top{padding:calc(var(--sat) + 10px) 16px 12px;background:linear-gradient(160deg,var(--pri2),var(--pri));color:#fff;border-radius:0 0 22px 22px;box-shadow:0 4px 16px rgba(120,50,20,.25);flex:none}
.top .row1{display:flex;align-items:center;justify-content:space-between}
.top h1{margin:0;font-size:22px;font-weight:700;letter-spacing:.2px}.top h1 small{font-weight:400;opacity:.85;font-size:14px;margin-inline-start:6px}
.icon-btn{background:rgba(255,255,255,.18);border:0;color:#fff;width:40px;height:40px;border-radius:50%;font-size:20px;display:grid;place-items:center}
.icon-btn:active{background:rgba(255,255,255,.3)}
.stats{display:flex;gap:8px;margin-top:12px}
.stat{flex:1;background:rgba(255,255,255,.16);border-radius:14px;padding:8px 6px;text-align:center;font-size:12px;opacity:.95}
.stat b{display:block;font-size:20px;line-height:1.2}
main{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:14px 14px calc(88px + var(--sab))}
.tab{display:none}.tab.on{display:block}
.search{position:sticky;top:-14px;z-index:5;background:var(--bg);padding:6px 0 10px}
.search input{width:100%;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:14px;padding:13px 16px;font-size:16px;outline:0}
.chips{display:flex;gap:8px;margin-top:10px;overflow-x:auto}
.chip{flex:none;border:0;background:var(--chip);color:var(--ink);border-radius:20px;padding:8px 16px;font-size:14px}
.chip.on{background:var(--pri);color:#fff}
.rc{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.rc h3{margin:0 0 4px;font-size:16px;line-height:1.35;user-select:text;-webkit-user-select:text}
.new{display:inline-block;background:var(--ok);color:#fff;border-radius:8px;font-size:11px;padding:1px 7px;margin-inline-start:6px;vertical-align:middle}
.addr{color:var(--mut);font-size:13px;margin-bottom:8px}
.meta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;margin-bottom:10px;user-select:text;-webkit-user-select:text}
.meta span{color:var(--mut)}.meta b{color:var(--ink);font-weight:600;direction:ltr;unicode-bidi:embed}
.acts{display:flex;gap:8px;flex-wrap:wrap}
.acts a{flex:1;min-width:84px;text-align:center;text-decoration:none;color:#fff;padding:11px 8px;border-radius:12px;font-size:14px;font-weight:600}
.acts a:active{opacity:.75}
.a-wa{background:#25d366}.a-mail{background:#2f6fed}.a-tel{background:#6b7280}.a-map{background:#e0793c}.a-web{background:#7c5cd6}
.empty{text-align:center;color:var(--mut);padding:60px 20px;line-height:1.8}
.more{width:100%;border:1px dashed var(--line);background:transparent;color:var(--mut);border-radius:14px;padding:13px;margin:4px 0 12px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;margin-bottom:14px}
.panel h2{margin:0 0 10px;font-size:17px}
.sw{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);font-size:15px}
.sw:last-of-type{border-bottom:0}.sw small{display:block;color:var(--mut);font-size:12px;margin-top:2px}
.sw input{appearance:none;-webkit-appearance:none;flex:none;width:50px;height:30px;border-radius:15px;background:var(--chip);position:relative;transition:.2s;border:0}
.sw input::after{content:"";position:absolute;top:3px;inset-inline-start:3px;width:24px;height:24px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:.2s}
.sw input:checked{background:var(--ok)}.sw input:checked::after{inset-inline-start:23px}
.btn{width:100%;border:0;border-radius:16px;padding:16px;font-size:17px;font-weight:700;color:#fff;background:var(--pri);box-shadow:0 4px 12px rgba(180,83,42,.3)}
.btn:active{transform:scale(.985)}.btn:disabled{opacity:.45;box-shadow:none}
.btn.sec{background:var(--chip);color:var(--ink);box-shadow:none;font-weight:600;font-size:15px;padding:14px}
.btn.red{background:#d64545}
.gap{height:10px}
.bar{height:8px;background:var(--chip);border-radius:4px;overflow:hidden;margin:14px 0 6px;display:none}
.bar i{display:block;height:100%;width:35%;background:var(--pri);border-radius:4px;animation:m 1.1s infinite ease-in-out}
@keyframes m{0%{margin-inline-start:-35%}100%{margin-inline-start:100%}}
#msg{color:var(--ink);font-size:14px;line-height:1.6;min-height:20px;user-select:text;-webkit-user-select:text}
#time{color:var(--mut);font-size:12px;direction:ltr;text-align:center}
#err{color:#d64545;font-size:14px;line-height:1.6;margin-top:8px;white-space:pre-wrap;user-select:text;-webkit-user-select:text}
.hint{color:var(--mut);font-size:13px;line-height:1.8}
nav{position:fixed;bottom:0;left:0;right:0;max-width:640px;margin:0 auto;display:flex;background:color-mix(in srgb,var(--card) 92%,transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-top:1px solid var(--line);padding:6px 6px calc(6px + var(--sab));z-index:10}
nav button{flex:1;border:0;background:transparent;color:var(--mut);display:flex;flex-direction:column;align-items:center;gap:2px;font-size:11px;padding:6px 0;border-radius:12px}
nav button .ic{font-size:23px;line-height:1.1}
nav button.on{color:var(--pri);font-weight:700}
</style>
</head>
<body>
<div id="app">
  <div class="top">
    <div class="row1"><h1>🏯 Riads<small>مراكش</small></h1><button class="icon-btn" id="refresh" aria-label="تحديث">⟳</button></div>
    <div class="stats">
      <div class="stat"><b id="sN">0</b>riads</div>
      <div class="stat"><b id="sE">0</b>بإيميل</div>
      <div class="stat"><b id="sW">0</b>بواتساب</div>
    </div>
  </div>

  <main>
    <section class="tab on" id="tab-list">
      <div class="search">
        <input type="search" id="q" placeholder="🔎 ابحث بالاسم أو الإيميل أو الرقم" autocomplete="off">
        <div class="chips">
          <button class="chip on" id="cAll">الكل</button>
          <button class="chip" id="cE">✉️ بإيميل</button>
          <button class="chip" id="cW">💬 بواتساب</button>
        </div>
        <input type="checkbox" id="fE" hidden><input type="checkbox" id="fW" hidden>
      </div>
      <div id="list"></div>
    </section>

    <section class="tab" id="tab-search">
      <div class="panel">
        <h2>بحث جديد في مراكش</h2>
        <label class="sw"><span>استخراج الإيميل والواتساب<small>يزور موقع كل رياض جديد</small></span><input type="checkbox" id="crawl" checked></label>
        <label class="sw"><span>بحث موسّع<small>نتائج أكثر لكن يستهلك حصة Google أكثر</small></span><input type="checkbox" id="wide"></label>
      </div>
      <button class="btn" id="go">ابدأ البحث</button>
      <div class="gap"></div>
      <button class="btn sec red" id="stop" disabled style="color:#fff">إيقاف</button>
      <div class="bar" id="bar"><i></i></div>
      <div id="time"></div>
      <div id="msg"></div>
      <div id="err"></div>
    </section>

    <section class="tab" id="tab-more">
      <div class="panel">
        <h2>تصدير</h2>
        <button class="btn sec" id="csv" disabled>⬇️ تحميل القائمة (CSV)</button>
        <p class="hint">على الآيفون يفتح القائمة للمشاركة: اختر «حفظ في الملفات».</p>
      </div>
      <div class="panel">
        <h2>تثبيت التطبيق</h2>
        <p class="hint"><b>آيفون:</b> زر المشاركة ثم «إضافة إلى الشاشة الرئيسية».<br><b>أندرويد:</b> قائمة المتصفح ثم «تثبيت التطبيق».</p>
      </div>
      <div class="panel"><button class="btn sec" id="out">تسجيل الخروج</button></div>
    </section>
  </main>

  <nav>
    <button class="on" data-tab="list"><span class="ic">📋</span>القائمة</button>
    <button data-tab="search"><span class="ic">🔍</span>بحث</button>
    <button data-tab="more"><span class="ic">⚙️</span>المزيد</button>
  </nav>
</div>
<script>
const $=id=>document.getElementById(id);let ROWS=[],SHOW=40;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function showTab(n){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.id==='tab-'+n));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.tab===n));$('app').querySelector('main').scrollTop=0}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
function setChips(){$('cAll').classList.toggle('on',!$('fE').checked&&!$('fW').checked);$('cE').classList.toggle('on',$('fE').checked);$('cW').classList.toggle('on',$('fW').checked);SHOW=40;render()}
$('cAll').onclick=()=>{$('fE').checked=$('fW').checked=false;setChips()};
$('cE').onclick=()=>{$('fE').checked=!$('fE').checked;setChips()};
$('cW').onclick=()=>{$('fW').checked=!$('fW').checked;setChips()};
$('q').addEventListener('input',()=>{SHOW=40;render()});
function render(){
  const q=$('q').value.toLowerCase().trim(),fe=$('fE').checked,fw=$('fW').checked;
  const list=ROWS.filter(r=>(!fe||r.email)&&(!fw||r.whatsapp)&&(!q||[r.name,r.email,r.other_emails,r.phones,r.whatsapp,r.address,r.website].join(' ').toLowerCase().includes(q)));
  $('sN').textContent=ROWS.length;$('sE').textContent=ROWS.filter(r=>r.email).length;$('sW').textContent=ROWS.filter(r=>r.whatsapp).length;
  if(!list.length){$('list').innerHTML=`<div class="empty">${ROWS.length?'لا توجد نتائج مطابقة.':'لا توجد رياضات محفوظة بعد.<br>افتح تبويب «بحث» وابدأ.'}</div>`;return}
  $('list').innerHTML=list.slice(0,SHOW).map(r=>{
    const tel=String(r.phones||'').split(';')[0].trim();
    return `<article class="rc"><h3>${esc(r.name)}${srcs(r).some(s=>FRESH.has(s))?'<span class="new">جديد</span>':''}</h3>
    <div class="addr">${esc(r.address)}</div>
    <div class="meta">${r.rating?`<span>⭐ <b>${esc(r.rating)}</b> (${esc(r.reviews)})</span>`:''}${r.email?`<span>✉️ <b>${esc(r.email)}</b></span>`:''}${r.whatsapp?`<span>💬 <b>${esc(r.whatsapp)}</b>${r.whatsapp_status&&r.whatsapp_status!=='confirmed'?' <small>(محتمل)</small>':''}</span>`:''}${tel&&tel!==r.whatsapp?`<span>📞 <b>${esc(tel)}</b></span>`:''}</div>
    <div class="acts">${r.whatsapp_link?`<a class="a-wa" href="${esc(r.whatsapp_link)}" target="_blank" rel="noopener">واتساب</a>`:''}${r.email?`<a class="a-mail" href="mailto:${esc(r.email)}">إيميل</a>`:''}${tel?`<a class="a-tel" href="tel:${esc(tel)}">اتصال</a>`:''}${r.maps_url?`<a class="a-map" href="${esc(r.maps_url)}" target="_blank" rel="noopener">الخريطة</a>`:''}${r.website?`<a class="a-web" href="${esc(r.website)}" target="_blank" rel="noopener">الموقع</a>`:''}</div></article>`}).join('')
   +(list.length>SHOW?`<button class="more" id="more">عرض المزيد (${list.length-SHOW})</button>`:'');
  const m=$('more');if(m)m.onclick=()=>{SHOW+=40;render()};
}
function busy(b){$('go').disabled=b;$('stop').disabled=!b;$('bar').style.display=b?'block':'none';$('go').textContent=b?'جاري البحث...':'ابدأ البحث'}
$('refresh').onclick=()=>{$('refresh').style.opacity=.5;loadStored().finally(()=>$('refresh').style.opacity=1)};
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
let stopFlag=false;
const LIMIT=120000,SEARCH_SHARE=0.6; // 2 دقيقة كحد أقصى للعملية كاملة
async function post(u,b){let r;for(let k=0;;k++){try{r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});break}catch(e){if(k>=3)throw new Error('انقطع الاتصال بالخادم (Load failed). تأكد من الإنترنت وأعد المحاولة.');await new Promise(z=>setTimeout(z,1000*(k+1)))}}
  if(r.status===401){location.reload();return}const d=await r.json();if(!r.ok)throw new Error(d.error||r.status);return d}
async function pool(items,n,fn,deadline,ok=()=>true){let i=0;await Promise.all(Array.from({length:n},async()=>{while(i<items.length&&!stopFlag&&ok()&&Date.now()<deadline){await fn(items[i++])}}))}
function plain(l){ // صف بدون زيارة الموقع (عند انتهاء الوقت)
  const mob=(l.phones||[]).find(p=>/^\+212[67]/.test(p));
  return {name:l.name,email:'',other_emails:'',whatsapp:mob||'',whatsapp_status:mob?'likely (mobile)':'',whatsapp_link:mob?'https://wa.me/'+mob.slice(1):'',phones:(l.phones||[]).join('; '),website:l.website,address:l.address,maps_url:l.maps_url||'',rating:l.rating,reviews:l.reviews,lat:l.lat,lon:l.lon,source:l.source}}
let STORED=[],FRESH=new Set();
const srcs=r=>String(r.source||'').split(';').map(s=>s.trim()).filter(Boolean);
async function getLeads(){for(let k=0;;k++){try{const d=await (await fetch('/api/leads')).json();if(d.error)throw new Error(d.error);return d}catch(e){if(k>=3)throw e;await new Promise(z=>setTimeout(z,1500*(k+1)))}}}
async function loadStored(){
  $('msg').textContent='جاري تحميل القائمة المحفوظة...';
  try{const d=await getLeads();STORED=d.rows||[];ROWS=STORED;render();$('csv').disabled=!ROWS.length;
    $('msg').textContent=ROWS.length?`المحفوظ سابقاً: ${ROWS.length} رياض`:''}catch(e){$('err').textContent=e.message}
}
loadStored();
$('go').onclick=async()=>{
  $('err').textContent='';stopFlag=false;FRESH=new Set();busy(true);$('csv').disabled=true;const buf=[];let addedTotal=0,flushing=Promise.resolve();const addedSrc=new Set();
  const flush=(force)=>{if(!buf.length||(!force&&buf.length<40))return;const chunk=buf.splice(0);
    flushing=flushing.then(async()=>{try{await post('/api/save',{rows:chunk})}catch(e){buf.unshift(...chunk)}})};
  const KNOWN=new Set();STORED.forEach(r=>srcs(r).forEach(s=>KNOWN.add(s)));
  const t0=Date.now(),tick=setInterval(()=>{$('time').textContent=Math.min(120,Math.round((Date.now()-t0)/1000))+' / 120 ث'},500);
  try{
    const plan=await (await fetch('/api/plan')).json();
    if(!plan.configured)throw new Error('API key غير مضبوط على الخادم');
    // توفير الاستهلاك: الكلمة الأولى على كل الخلايا، وباقي الكلمات فقط على الخلايا "المشبعة" (≥50 نتيجة = قد يكون فيها المزيد)
    const found=new Map(),sat={};let done=0,failed=0,lastErr='',quota=false,sent=0;
    const wide=$('wide').checked,SEARCH_END=t0+LIMIT*SEARCH_SHARE;
    for(let q=0;q<plan.queries&&!stopFlag&&!quota&&Date.now()<SEARCH_END;q++){
      const cells=[];for(let c=0;c<plan.cells;c++)if(q===0||wide||(sat[c]||0)>=50)cells.push(c);
      if(!cells.length)break;
      await pool(cells,8,async c=>{
        sent++;
        try{const r=await post('/api/search',{q,cell:c});r.leads.forEach(l=>found.set(l.source,l));sat[c]=Math.max(sat[c]||0,r.raw||0)}
        catch(e){failed++;lastErr=e.message;if(/Google API 429|Quota/i.test(lastErr))quota=true}
        done++;$('msg').textContent=`بحث Google Maps: كلمة ${q+1}/${plan.queries} - ${done} طلب - وُجد ${found.size} رياض`;
      },SEARCH_END,()=>!quota);
    }
    if(done&&failed===done&&!quota)throw new Error(lastErr);
    const leads=[...found.values()].filter(l=>!KNOWN.has(l.source)),doCrawl=$('crawl').checked,seen=new Set();let n=0;
    const already=found.size-leads.length;
    leads.forEach(l=>buf.push(plain(l)));flush(true); // احفظ نتائج Google فوراً قبل زيارة المواقع
    if(doCrawl)await pool(leads,12,async l=>{
      try{buf.push((await post('/api/enrich',l)).row);seen.add(l.source);flush(false)}catch(e){}
      n++;$('msg').textContent=`رياضات جديدة: استخراج الإيميل والواتساب ${n}/${leads.length}`;
    },t0+LIMIT);
    const left=leads.length-seen.size;
    $('msg').textContent='جاري الحفظ...';
    flush(true);await flushing;if(buf.length){flush(true);await flushing}
    if(buf.length)throw new Error('تعذّر حفظ '+buf.length+' رياض. لا تغلق الصفحة وأعد المحاولة.');
    const before=STORED.length,all=await getLeads();
    ROWS=STORED=all.rows||[];FRESH=new Set();ROWS.forEach(r=>srcs(r).forEach(s=>{if(!KNOWN.has(s))FRESH.add(s)}));const saved={added:Math.max(0,ROWS.length-before)};
    $('msg').textContent=(stopFlag?'تم الإيقاف':'انتهى')+`: أُضيف ${saved.added} رياض جديد، المجموع المحفوظ ${ROWS.length}`+(already?` (تخطّيت ${already} موجودة سابقاً)`:'')+(doCrawl&&left?` — ${left} لم يُفحص موقعها بسبب حد الوقت`:'');
    if(quota)$('err').textContent='توقف البحث: تجاوز مشروع Google حصته اليومية (SearchTextRequest per day). ارفع الحصة من Google Cloud ← APIs & Services ← Places API (New) ← Quotas، أو أعد المحاولة غداً. ما وُجد قبل التوقف تم حفظه.';
    else if(failed)$('err').textContent=`فشل ${failed} طلب بحث: ${lastErr}`;
  }catch(e){$('err').textContent=e.message}
  clearInterval(tick);busy(false);render();$('csv').disabled=!ROWS.length;
};
$('out').onclick=async()=>{await fetch('/api/logout',{method:'POST'});location.reload()};
$('stop').onclick=()=>{stopFlag=true;$('msg').textContent='جاري الإيقاف...'};
$('csv').onclick=()=>{
  const cols=Object.keys(ROWS[0]);
  const csv='﻿'+[cols.join(',')].concat(ROWS.map(r=>cols.map(c=>'"'+String(r[c]??'').replace(/"/g,'""')+'"').join(','))).join('\n');
  const file=new File([csv],'riads_marrakech.csv',{type:'text/csv'});
  if(navigator.canShare&&navigator.canShare({files:[file]})){navigator.share({files:[file],title:'Riads Marrakech'}).catch(()=>{});return}
  const a=document.createElement('a');a.href=URL.createObjectURL(file);a.download='riads_marrakech.csv';a.click();
};
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
