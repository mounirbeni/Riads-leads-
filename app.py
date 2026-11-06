#!/usr/bin/env python3
"""موقع بحث عن riads في مراكش (Google Maps) مع الإيميلات والواتساب.

    pip install -r requirements.txt
    python3 app.py            ->  http://localhost:5000
"""
import hashlib
import io
import json
import re
import hmac
import ipaddress
import os
import socket
import time
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, make_response, request

import riads_leads as core
import base64

import auto
import store
from icons import ICONS

app = Flask(__name__)
KEYS = [k.strip() for name in ("GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_API_KEY_2", "GOOGLE_MAPS_API_KEYS")
        for k in os.environ.get(name, "").split(",") if k.strip()]
KEYS = list(dict.fromkeys(KEYS))  # بدون تكرار
KEY = KEYS[0] if KEYS else ""
POOL = core.KeyPool(KEYS, on_usage=store.record_usage, on_exhausted=store.mark_exhausted)  # توزيع + تحويل + عدّاد
DAILY_LIMIT = int(os.environ.get("GOOGLE_DAILY_LIMIT", "100"))  # حد Google اليومي لكل مشروع
CRON_SECRET = os.environ.get("CRON_SECRET", "")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
TOKEN = hmac.new(("riads|" + ACCESS_CODE + "|" + KEY).encode(), b"ok", hashlib.sha256).hexdigest()


def authed():
    return bool(ACCESS_CODE) and hmac.compare_digest(request.cookies.get("auth", ""), TOKEN)


@app.before_request
def guard():
    if request.path.startswith("/api/") and request.path not in ("/api/login", "/api/cron", "/api/public-leads") and not authed():
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
    return jsonify(queries=len(core.QUERIES), cells=len(CELLS), configured=bool(KEYS), keys=len(KEYS),
                   skip=core.skip_cells(store.cell_state()))


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
    try:
        known = None if d.get("full") else store.known_ids()  # يوفّر الصفحات المعروفة
        places = POOL.search(int(d["q"]), int(d["cell"]), CELLS, known=known)
        leads = [l for l in map(core.place_to_lead, places) if l]
        return jsonify(leads=leads, raw=60 if places.early else len(places))
    except core.QuotaExhausted as e:
        return jsonify(error="Google API 429: " + str(e)), 429
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 502


@app.post("/api/cells")
def cells_stats():
    """ذاكرة الخلايا: {خلية: عدد نتائج الكلمة الأولى} لتجنّب إعادة فحص الخلايا الفارغة."""
    raw = (request.get_json(force=True) or {}).get("raw", {})
    store.record_cells({int(k): int(v) for k, v in raw.items() if str(k).isdigit()})
    return jsonify(ok=True)


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
    row = core.finalize(lead)
    row["crawled"] = 1
    return jsonify(row=row)


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


@app.get("/api/cron")
def cron():
    """يستدعيه Vercel Cron يومياً (Authorization: Bearer CRON_SECRET)."""
    got = request.headers.get("Authorization", "")
    if not CRON_SECRET or not hmac.compare_digest(got, "Bearer " + CRON_SECRET):
        return jsonify(error="unauthorized"), 401
    return jsonify(auto.run(KEYS, public_url=public_url))


@app.post("/api/auto-run")
def auto_run():
    """زر «تشغيل الآن» في الواجهة (يتطلب تسجيل الدخول)."""
    if not KEYS:
        return jsonify(error="GOOGLE_MAPS_API_KEY غير مضبوط على الخادم"), 500
    return jsonify(auto.run(KEYS, public_url=public_url))


@app.get("/api/last-run")
def last_run():
    return jsonify(run=store.last_run())


def _check_key(key):
    try:
        r = requests.post(core.PLACES_URL, json={"textQuery": "riad", "pageSize": 1}, timeout=15,
                          headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.id", "Content-Type": "application/json"})
        if r.status_code == 200:
            return {"state": "ok"}
        err = r.json().get("error", {})
        m = re.search(r"project_number:(\d+)", err.get("message", ""))
        return {"state": "exhausted" if r.status_code == 429 else "error", "project": m.group(1) if m else "",
                "detail": err.get("status", str(r.status_code))}
    except Exception as e:  # noqa: BLE001
        return {"state": "error", "detail": str(e)[:80]}


@app.get("/api/keys")
def keys_status():
    """عدّاد كل مفتاح: المستخدم اليوم من الحد، بلا فحص Google (لا يستهلك الحصة).
    ?fresh=1: فحص فعلي بطلب واحد لكل مفتاح، ويسجّل المفاتيح المنتهية."""
    used, dead = store.usage_today()
    probe = {}
    if request.args.get("fresh") == "1":
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            res = list(ex.map(_check_key, KEYS))
        for i, r in enumerate(res):
            probe[i] = r
            if r["state"] == "exhausted" and i not in dead:
                store.mark_exhausted(i)
                dead.add(i)
            if r["state"] == "ok":
                used[i] = used.get(i, 0) + 1  # طلب الفحص نفسه
                try:
                    store.record_usage(i, 1)
                except Exception:  # noqa: BLE001
                    pass
    out = []
    for i in range(len(KEYS)):
        u = used.get(i, 0)
        exhausted = i in dead or u >= DAILY_LIMIT
        if probe.get(i, {}).get("state") == "error":
            state = "error"
        else:
            state = "exhausted" if exhausted else "ok"
        out.append({"n": i + 1, "state": state, "used": DAILY_LIMIT if exhausted else min(u, DAILY_LIMIT),
                    "limit": DAILY_LIMIT, "left": 0 if exhausted else max(0, DAILY_LIMIT - u)})
    return jsonify(keys=out, limit=DAILY_LIMIT, total_left=sum(k["left"] for k in out))


_PUB = {"at": 0.0, "rows": [], "v": ""}
HIDDEN = ("source", "crawled")  # حقول داخلية لا تُعرض للعامة


def _version(rows):
    return hashlib.md5(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]


@app.get("/api/public-leads")
def public_leads():
    """القائمة العامة (بلا تسجيل دخول). كاش 20 ث على الخادم و30 ث على الحافة لتخفيف الحمل."""
    try:
        if time.time() - _PUB["at"] > 20:
            rows = [{k: v for k, v in r.items() if k not in HIDDEN} for r in store.load()]
            _PUB.update(at=time.time(), rows=rows, v=_version(rows))
        resp = jsonify(rows=_PUB["rows"], v=_PUB["v"], updated=int(_PUB["at"]))
        resp.headers["Cache-Control"] = "public, s-maxage=30, stale-while-revalidate=60"
        return resp
    except Exception as e:  # noqa: BLE001
        return jsonify(error="تعذر قراءة القائمة: " + str(e)), 500


COLS = [("name", "الاسم"), ("email", "الإيميل"), ("other_emails", "إيميلات أخرى"), ("whatsapp", "واتساب"),
        ("whatsapp_link", "رابط واتساب"), ("phones", "الهاتف"), ("website", "الموقع"), ("address", "العنوان"),
        ("maps_url", "الخريطة"), ("rating", "التقييم"), ("reviews", "عدد التقييمات"), ("added_at", "أُضيف في")]
WIDTHS = {"name": 34, "email": 32, "other_emails": 28, "whatsapp": 16, "whatsapp_link": 30, "phones": 20,
          "website": 34, "address": 44, "maps_url": 30, "rating": 9, "reviews": 12, "added_at": 20}


@app.get("/api/export.xlsx")
def export_xlsx():
    """ملف Excel (للمسجّلين فقط): عربي من اليمين، روابط قابلة للنقر، فلتر وتثبيت العنوان."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    rows = store.load()
    wb = Workbook()
    ws = wb.active
    ws.title = "Riads Marrakech"
    ws.sheet_view.rightToLeft = True
    ws.append([label for _, label in COLS])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="B4532A")
        c.alignment = Alignment(horizontal="center", vertical="center")
    for r in rows:
        ws.append([r.get(k, "") for k, _ in COLS])
    for i, (k, _) in enumerate(COLS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = WIDTHS.get(k, 18)
        if k in ("whatsapp_link", "website", "maps_url"):
            for row in range(2, len(rows) + 2):
                cell = ws.cell(row=row, column=i)
                if cell.value:
                    cell.hyperlink = str(cell.value)
                    cell.font = Font(color="0563C1", underline="single")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    name = "riads_marrakech_%s.xlsx" % time.strftime("%Y%m%d")
    return buf.getvalue(), 200, {
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Content-Disposition": 'attachment; filename="%s"' % name, "Cache-Control": "no-store"}


@app.get("/api/leads")
def leads():
    try:
        rows = store.load()
        return jsonify(rows=rows, v=_version(rows))
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
    html = PAGE.replace("__ADMIN__", "true" if authed() else "false")
    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "private, no-store"}


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
.i{width:1.15em;height:1.15em;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none;vertical-align:-.2em}
.i.f{fill:currentColor;stroke:none}
.logo{width:26px;height:26px;stroke-width:1.8;vertical-align:-6px;margin-inline-start:6px}
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
.icon-btn{background:rgba(255,255,255,.18);border:0;color:#fff;width:40px;height:40px;border-radius:50%;font-size:20px;display:grid;place-items:center}.icon-btn .i{width:20px;height:20px;vertical-align:0}
.icon-btn:active{background:rgba(255,255,255,.3)}
.stats{display:flex;gap:8px;margin-top:12px}
.stat{flex:1;background:rgba(255,255,255,.16);border-radius:14px;padding:8px 6px;text-align:center;font-size:12px;opacity:.95}
.stat b{display:block;font-size:20px;line-height:1.2}
main{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:14px 14px calc(88px + var(--sab))}
.tab{display:none}.tab.on{display:block}
.search{position:sticky;top:-14px;z-index:5;background:var(--bg);padding:6px 0 10px}
.sbox{position:relative}.sbox .i{position:absolute;inset-inline-start:14px;top:50%;transform:translateY(-50%);color:var(--mut);width:20px;height:20px;pointer-events:none}
.search input{width:100%;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:14px;padding:13px 16px;padding-inline-start:44px;font-size:16px;outline:0}
.chips{display:flex;gap:8px;margin-top:10px;overflow-x:auto}
.chip{display:inline-flex;align-items:center;gap:6px;flex:none;border:0;background:var(--chip);color:var(--ink);border-radius:20px;padding:8px 16px;font-size:14px}
.chip.on{background:var(--pri);color:#fff}
.rc{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.rc h3{margin:0 0 4px;font-size:16px;line-height:1.35;user-select:text;-webkit-user-select:text}
.new{display:inline-block;background:var(--ok);color:#fff;border-radius:8px;font-size:11px;padding:1px 7px;margin-inline-start:6px;vertical-align:middle}
.addr{color:var(--mut);font-size:13px;margin-bottom:8px}
.meta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;margin-bottom:10px;user-select:text;-webkit-user-select:text}
.meta span{color:var(--mut);display:inline-flex;align-items:center;gap:5px}.meta .i{width:15px;height:15px;color:var(--mut)}.meta .i.f{color:#e0a030}.meta b{color:var(--ink);font-weight:600;direction:ltr;unicode-bidi:embed}
.acts{display:flex;gap:8px;flex-wrap:wrap}
.acts a{flex:1;min-width:84px;display:flex;align-items:center;justify-content:center;gap:6px;text-align:center;text-decoration:none;color:#fff;padding:11px 8px;border-radius:12px;font-size:14px;font-weight:600}
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
.btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;border:0;border-radius:16px;padding:16px;font-size:17px;font-weight:700;color:#fff;background:var(--pri);box-shadow:0 4px 12px rgba(180,83,42,.3)}
.btn:active{transform:scale(.985)}.btn:disabled{opacity:.45;box-shadow:none}
.btn.sec{background:var(--chip);color:var(--ink);box-shadow:none;font-weight:600;font-size:15px;padding:14px}
.btn.red{background:#d64545}
.gap{height:10px}
body.pub .adm{display:none!important}body:not(.pub) .pubonly{display:none!important}
.live{display:flex;align-items:center;gap:8px;color:var(--mut);font-size:12px;margin:10px 2px 0}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none;animation:pl 2s infinite}
.pulse.off{background:#d64545;animation:none}
@keyframes pl{0%{box-shadow:0 0 0 0 rgba(31,157,85,.5)}70%{box-shadow:0 0 0 8px rgba(31,157,85,0)}100%{box-shadow:0 0 0 0 rgba(31,157,85,0)}}
.field{width:100%;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:14px;padding:14px 16px;font-size:18px;text-align:center;outline:0;margin:8px 0 12px;letter-spacing:2px}
.quota{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;margin-bottom:14px}
.quota h2{margin:0 0 4px;font-size:15px;color:var(--mut);font-weight:600}
.cd{font-size:38px;font-weight:700;letter-spacing:1px;direction:ltr;text-align:center;font-variant-numeric:tabular-nums;color:var(--pri);margin:6px 0 2px}
.cd.ok{color:var(--ok)}
.cdsub{text-align:center;color:var(--mut);font-size:13px;margin-bottom:12px;line-height:1.6}
.krow{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line);font-size:14px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mut);flex:none}
.dot.ok{background:var(--ok)}.dot.exhausted{background:#d64545}.dot.error{background:#e0a030}
.krow .st{margin-inline-start:auto;font-weight:600}
.mt{flex-basis:100%;height:6px;background:var(--chip);border-radius:3px;overflow:hidden;margin-top:2px}.mt i{display:block;height:100%;background:var(--ok);border-radius:3px}.mt i.exhausted{background:#d64545}
.krow small{color:var(--mut);direction:ltr}
.qbtn{margin-top:10px;width:100%;border:0;background:var(--chip);color:var(--ink);border-radius:12px;padding:11px;font-size:14px;display:flex;align-items:center;justify-content:center;gap:6px}
.bar{height:8px;background:var(--chip);border-radius:4px;overflow:hidden;margin:14px 0 6px;display:none}
.bar i{display:block;height:100%;width:35%;background:var(--pri);border-radius:4px;animation:m 1.1s infinite ease-in-out}
@keyframes m{0%{margin-inline-start:-35%}100%{margin-inline-start:100%}}
#msg{color:var(--ink);font-size:14px;line-height:1.6;min-height:20px;user-select:text;-webkit-user-select:text}
#time{color:var(--mut);font-size:12px;direction:ltr;text-align:center}
#err{color:#d64545;font-size:14px;line-height:1.6;margin-top:8px;white-space:pre-wrap;user-select:text;-webkit-user-select:text}
.hint{color:var(--mut);font-size:13px;line-height:1.8}
nav{position:fixed;bottom:0;left:0;right:0;max-width:640px;margin:0 auto;display:flex;background:color-mix(in srgb,var(--card) 92%,transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-top:1px solid var(--line);padding:6px 6px calc(6px + var(--sab));z-index:10}
nav button{flex:1;border:0;background:transparent;color:var(--mut);display:flex;flex-direction:column;align-items:center;gap:2px;font-size:11px;padding:6px 0;border-radius:12px}
nav button .ic{width:24px;height:24px;vertical-align:0;stroke-width:1.9}
nav button.on{color:var(--pri);font-weight:700}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-logo" viewBox="0 0 24 24"><path d="M4 21V12a8 8 0 0 1 16 0v9"/><path d="M9 21v-5a3 3 0 0 1 6 0v5"/><path d="M12 2v1.5"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></symbol>
<symbol id="i-list" viewBox="0 0 24 24"><rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4M12 16h4M8 11h.01M8 16h.01"/></symbol>
<symbol id="i-settings" viewBox="0 0 24 24"><path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4"/></symbol>
<symbol id="i-refresh" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></symbol>
<symbol id="i-mail" viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></symbol>
<symbol id="i-chat" viewBox="0 0 24 24"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/></symbol>
<symbol id="i-phone" viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/></symbol>
<symbol id="i-pin" viewBox="0 0 24 24"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></symbol>
<symbol id="i-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20M2 12h20"/></symbol>
<symbol id="i-star" viewBox="0 0 24 24"><path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/></symbol>
<symbol id="i-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></symbol>
<symbol id="i-logout" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/></symbol>
<symbol id="i-layers" viewBox="0 0 24 24"><path d="m12 2 10 5-10 5L2 7z"/><path d="m2 12 10 5 10-5"/><path d="m2 17 10 5 10-5"/></symbol>
<symbol id="i-smartphone" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/></symbol>
</defs></svg>
<div id="app">
  <div class="top">
    <div class="row1"><h1><svg class="i logo"><use href="#i-logo"/></svg>Riads<small>مراكش</small></h1><button class="icon-btn" id="refresh" aria-label="تحديث"><svg class="i"><use href="#i-refresh"/></svg></button></div>
    <div class="stats">
      <div class="stat"><b id="sN">0</b>riads</div>
      <div class="stat"><b id="sE">0</b>بإيميل</div>
      <div class="stat"><b id="sW">0</b>بواتساب</div>
    </div>
  </div>

  <main>
    <section class="tab on" id="tab-list">
      <div class="search">
        <div class="sbox"><svg class="i"><use href="#i-search"/></svg><input type="search" id="q" placeholder="ابحث بالاسم أو الإيميل أو الرقم" autocomplete="off"></div>
        <div class="chips">
          <button class="chip on" id="cAll">الكل</button>
          <button class="chip" id="cE"><svg class="i"><use href="#i-mail"/></svg> بإيميل</button>
          <button class="chip" id="cW"><svg class="i"><use href="#i-chat"/></svg> بواتساب</button>
        </div>
        <input type="checkbox" id="fE" hidden><input type="checkbox" id="fW" hidden>
      </div>
      <div class="live"><span class="pulse" id="pulse"></span><span id="upd">جاري التحميل...</span></div>
      <div id="lerr" style="color:#d64545;font-size:14px;margin-top:8px"></div>
      <div id="list" style="margin-top:12px"></div>
    </section>

    <section class="tab pubonly" id="tab-login">
      <div class="panel">
        <h2>دخول الإدارة</h2>
        <p class="hint">القائمة مفتوحة للجميع. البحث وإدارة المفاتيح وتصدير Excel لمن لديه الكود فقط.</p>
        <input class="field" id="code" type="password" placeholder="الكود" autocomplete="current-password">
        <button class="btn" id="login">دخول</button>
        <div id="lgerr" style="color:#d64545;margin-top:10px;font-size:14px"></div>
      </div>
      <div class="panel">
        <h2>تثبيت التطبيق</h2>
        <p class="hint"><b>آيفون:</b> زر المشاركة ثم «إضافة إلى الشاشة الرئيسية».<br><b>أندرويد:</b> قائمة المتصفح ثم «تثبيت التطبيق».</p>
      </div>
    </section>

    <section class="tab adm" id="tab-search">
      <div class="quota">
        <h2>حصة Google اليومية تتجدد بعد</h2>
        <div class="cdsub" id="kcount" style="text-align:start;margin-bottom:4px"></div>
        <div class="cd" id="cd">--:--:--</div>
        <div class="cdsub" id="cdsub"></div>
        <div id="klist"></div>
        <button class="qbtn" id="kref"><svg class="i"><use href="#i-refresh"/></svg> فحص دقيق (طلب واحد لكل مفتاح)</button>
      </div>
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

    <section class="tab adm" id="tab-more">
      <div class="panel">
        <h2>البحث التلقائي اليومي</h2>
        <p class="hint" id="autoinfo">يعمل كل يوم تلقائياً (09:13 UTC) ويضيف الرياضات الجديدة فقط ويكمل استخراج الإيميلات.</p>
        <button class="btn sec" id="autorun"><svg class="i"><use href="#i-refresh"/></svg> تشغيل الآن</button>
        <p class="hint" id="autoout"></p>
      </div>
      <div class="panel">
        <h2>تصدير</h2>
        <button class="btn sec" id="xl" disabled><svg class="i"><use href="#i-download"/></svg> إنشاء ملف Excel</button>
        <p class="hint" id="xlmsg">على الآيفون يفتح الملف للمشاركة: اختر «حفظ في الملفات».</p>
      </div>
      <div class="panel">
        <h2>تثبيت التطبيق</h2>
        <p class="hint"><b>آيفون:</b> زر المشاركة ثم «إضافة إلى الشاشة الرئيسية».<br><b>أندرويد:</b> قائمة المتصفح ثم «تثبيت التطبيق».</p>
      </div>
      <div class="panel"><button class="btn sec" id="out"><svg class="i"><use href="#i-logout"/></svg> تسجيل الخروج</button></div>
    </section>
  </main>

  <nav>
    <button class="on" data-tab="list"><svg class="i ic"><use href="#i-list"/></svg>القائمة</button>
    <button class="adm" data-tab="search"><svg class="i ic"><use href="#i-search"/></svg>بحث</button>
    <button class="adm" data-tab="more"><svg class="i ic"><use href="#i-settings"/></svg>المزيد</button>
    <button class="pubonly" data-tab="login"><svg class="i ic"><use href="#i-logout"/></svg>دخول</button>
  </nav>
</div>
<script>
const ADMIN=__ADMIN__;document.body.classList.toggle('pub',!ADMIN);
const $=id=>document.getElementById(id);let ROWS=[],SHOW=40,RUNNING=false,LASTV='';
const ic=(n,c)=>`<svg class="i${c?' '+c:''}"><use href="#i-${n}"/></svg>`;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let KLOADED=false;
function showTab(n){if(n==='search'&&!KLOADED){KLOADED=true;loadKeys(false)}document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.id==='tab-'+n));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.tab===n));$('app').querySelector('main').scrollTop=0}
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
    <div class="meta">${r.rating?`<span>${ic('star','f')}<b>${esc(r.rating)}</b> (${esc(r.reviews)})</span>`:''}${r.email?`<span>${ic('mail')}<b>${esc(r.email)}</b></span>`:''}${r.whatsapp?`<span>${ic('chat')}<b>${esc(r.whatsapp)}</b>${r.whatsapp_status&&r.whatsapp_status!=='confirmed'?' <small>(محتمل)</small>':''}</span>`:''}${tel&&tel!==r.whatsapp?`<span>${ic('phone')}<b>${esc(tel)}</b></span>`:''}</div>
    <div class="acts">${r.whatsapp_link?`<a class="a-wa" href="${esc(r.whatsapp_link)}" target="_blank" rel="noopener">${ic('chat')}واتساب</a>`:''}${r.email?`<a class="a-mail" href="mailto:${esc(r.email)}">${ic('mail')}إيميل</a>`:''}${tel?`<a class="a-tel" href="tel:${esc(tel)}">${ic('phone')}اتصال</a>`:''}${r.maps_url?`<a class="a-map" href="${esc(r.maps_url)}" target="_blank" rel="noopener">${ic('pin')}الخريطة</a>`:''}${r.website?`<a class="a-web" href="${esc(r.website)}" target="_blank" rel="noopener">${ic('globe')}الموقع</a>`:''}</div></article>`}).join('')
   +(list.length>SHOW?`<button class="more" id="more">عرض المزيد (${list.length-SHOW})</button>`:'');
  const m=$('more');if(m)m.onclick=()=>{SHOW+=40;render()};
}
function busy(b){RUNNING=b;$('go').disabled=b;$('stop').disabled=!b;$('bar').style.display=b?'block':'none';$('go').textContent=b?'جاري البحث...':'ابدأ البحث'}
$('refresh').onclick=()=>{$('refresh').style.opacity=.5;loadStored().finally(()=>$('refresh').style.opacity=1)};
// مؤقت تجدد الحصة: منتصف الليل بتوقيت كاليفورنيا (Pacific)
function msToReset(){const p=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}).formatToParts(new Date()).map(x=>[x.type,x.value]));
  const s=(+p.hour%24)*3600+(+p.minute)*60+(+p.second);return (86400-s)*1000}
let KEYST=[];
function tickCd(){const ms=msToReset(),s=Math.floor(ms/1000),pad=n=>String(n).padStart(2,'0');
  $('cd').textContent=`${pad(Math.floor(s/3600))}:${pad(Math.floor(s%3600/60))}:${pad(s%60)}`;
  const at=new Date(Date.now()+ms).toLocaleTimeString('ar-MA',{hour:'2-digit',minute:'2-digit'});
  const dead=KEYST.length&&KEYST.every(k=>k.state==='exhausted');
  $('cd').classList.toggle('ok',KEYST.length>0&&!dead);
  $('cdsub').textContent=(dead?'كل المفاتيح منتهية الآن. ':'')+`التجدد عند الساعة ${at} بتوقيتك (منتصف الليل بتوقيت كاليفورنيا).`}
setInterval(tickCd,1000);tickCd();
const ST={ok:'متاح',exhausted:'منتهٍ اليوم',error:'تعذّر الفحص'};
async function loadKeys(fresh){$('kref').disabled=true;$('klist').innerHTML='<div class="cdsub">جاري فحص المفاتيح...</div>';
  try{const d=await (await fetch('/api/keys'+(fresh?'?fresh=1':''))).json();KEYST=d.keys||[];{const ok=KEYST.filter(k=>k.state==='ok').length,ex=KEYST.filter(k=>k.state==='exhausted').length;$('kcount').textContent=`عدد المفاتيح: ${KEYST.length} — متاح ${ok}، منتهٍ ${ex} — المتبقي تقريباً ${d.total_left??'؟'} طلب بحث اليوم`}
    $('klist').innerHTML=KEYST.map(k=>{const pct=Math.min(100,Math.round(100*(k.used||0)/(k.limit||100)));return `<div class="krow" style="flex-wrap:wrap"><span class="dot ${k.state}"></span>مفتاح ${k.n}<span class="st">${k.used}/${k.limit} · ${ST[k.state]||k.state}</span><div class="mt"><i class="${k.state}" style="width:${pct}%"></i></div></div>`}).join('')+'<div class="cdsub" style="margin:8px 0 0;font-size:12px">العدّ يشمل طلبات هذا الموقع منذ تفعيل العدّاد؛ المفتاح المنتهي يظهر 100/100.</div>';tickCd()}
  catch(e){$('klist').innerHTML='<div class="cdsub">تعذّر فحص المفاتيح.</div>'}$('kref').disabled=false}
$('kref').onclick=()=>loadKeys(true);
const fmtRun=r=>{if(!r)return 'لم يعمل بعد.';const d=new Date(r.at);const when=isNaN(d)?r.at:d.toLocaleString('ar-MA',{dateStyle:'medium',timeStyle:'short'});
  return `آخر تشغيل: ${when} — جديد: ${r.new}، أُكمل استخراج: ${r.enriched}، طلبات بحث: ${r.searches}`+(r.quota?' — توقف بسبب حصة Google':'')+(r.error?' — خطأ: '+r.error:'')};
async function loadRun(){try{const d=await (await fetch('/api/last-run')).json();$('autoout').textContent=fmtRun(d.run)}catch(e){}}
$('autorun').onclick=async()=>{const b=$('autorun');b.disabled=true;$('autoout').textContent='جاري التشغيل... (حتى دقيقة)';
  try{const r=await post('/api/auto-run',{});$('autoout').textContent=fmtRun(r);await loadStored()}catch(e){$('autoout').textContent='تعذّر التشغيل: '+e.message}b.disabled=false};
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
const LEADS_URL=ADMIN?'/api/leads':'/api/public-leads';
async function getLeads(){for(let k=0;;k++){try{const d=await (await fetch(LEADS_URL,{cache:'no-store'})).json();if(d.error)throw new Error(d.error);return d}catch(e){if(k>=3)throw e;await new Promise(z=>setTimeout(z,1500*(k+1)))}}}
async function loadStored(){
  $('msg').textContent='جاري تحميل القائمة المحفوظة...';
  try{const d=await getLeads();STORED=d.rows||[];ROWS=STORED;LASTV=d.v||'';render();if(ADMIN)$('xl').disabled=!ROWS.length;setUpd(Date.now());$('lerr').textContent='';
    $('msg').textContent=ROWS.length?`المحفوظ سابقاً: ${ROWS.length} رياض`:''}catch(e){$('lerr').textContent=e.message;setUpd(null,true)}
}
function setUpd(ms,bad){$('pulse').classList.toggle('off',!!bad);$('upd').textContent=bad?'تعذّر التحديث — سنعيد المحاولة':'محدّث تلقائياً • آخر تحديث '+new Date(ms).toLocaleTimeString('ar-MA',{hour:'2-digit',minute:'2-digit'})}
async function refreshList(){if(RUNNING||document.hidden)return;try{const d=await (await fetch(LEADS_URL,{cache:'no-store'})).json();if(d.error)throw new Error(d.error);
  if(!d.v||d.v!==LASTV){LASTV=d.v||'';STORED=ROWS=d.rows||[];render();if(ADMIN)$('xl').disabled=!ROWS.length}setUpd(Date.now())}catch(e){setUpd(null,true)}}
setInterval(refreshList,30000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshList()});
loadStored();if(ADMIN)loadRun();
$('go').onclick=async()=>{
  $('err').textContent='';stopFlag=false;FRESH=new Set();busy(true);$('xl').disabled=true;const buf=[];let addedTotal=0,flushing=Promise.resolve();const addedSrc=new Set();
  const flush=(force)=>{if(!buf.length||(!force&&buf.length<40))return;const chunk=buf.splice(0);
    flushing=flushing.then(async()=>{try{await post('/api/save',{rows:chunk})}catch(e){buf.unshift(...chunk)}})};
  const KNOWN=new Set();STORED.forEach(r=>srcs(r).forEach(s=>KNOWN.add(s)));
  const t0=Date.now(),tick=setInterval(()=>{$('time').textContent=Math.min(120,Math.round((Date.now()-t0)/1000))+' / 120 ث'},500);
  try{
    const plan=await (await fetch('/api/plan')).json();
    if(!plan.configured)throw new Error('API key غير مضبوط على الخادم');
    // توفير الاستهلاك: الكلمة الأولى على كل الخلايا، وباقي الكلمات فقط على الخلايا "المشبعة" (≥50 نتيجة = قد يكون فيها المزيد)
    const found=new Map(),sat={},raw0={};let done=0,failed=0,lastErr='',quota=false,sent=0;
    const wide=$('wide').checked,SEARCH_END=t0+LIMIT*SEARCH_SHARE;
    for(let q=0;q<plan.queries&&!stopFlag&&!quota&&Date.now()<SEARCH_END;q++){
      const skip=new Set(wide?[]:(plan.skip||[]));
      const cells=[];for(let c=0;c<plan.cells;c++)if(q===0?!skip.has(c):(wide||(sat[c]||0)>=50))cells.push(c);
      if(!cells.length)break;
      await pool(cells,8,async c=>{
        sent++;
        try{const r=await post('/api/search',{q,cell:c,full:wide});r.leads.forEach(l=>found.set(l.source,l));sat[c]=Math.max(sat[c]||0,r.raw||0);if(q===0)raw0[c]=r.raw||0}
        catch(e){failed++;lastErr=e.message;if(/Google API 429|Quota/i.test(lastErr))quota=true}
        done++;$('msg').textContent=`بحث Google Maps: كلمة ${q+1}/${plan.queries} - ${done} طلب - وُجد ${found.size} رياض`;
      },SEARCH_END,()=>!quota);
    }
    if(Object.keys(raw0).length)post('/api/cells',{raw:raw0}).catch(()=>{});
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
  clearInterval(tick);busy(false);render();$('xl').disabled=!ROWS.length;
};
$('out').onclick=async()=>{await fetch('/api/logout',{method:'POST'});location.reload()};
$('stop').onclick=()=>{stopFlag=true;$('msg').textContent='جاري الإيقاف...'};
$('xl').onclick=async()=>{const b=$('xl');b.disabled=true;$('xlmsg').textContent='جاري إنشاء الملف...';
  try{const r=await fetch('/api/export.xlsx');if(r.status===401){location.reload();return}if(!r.ok)throw new Error(r.status);
    const blob=await r.blob(),name='riads_marrakech.xlsx',file=new File([blob],name,{type:blob.type});
    if(navigator.canShare&&navigator.canShare({files:[file]})){await navigator.share({files:[file],title:'Riads Marrakech'}).catch(()=>{})}
    else{const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click()}
    $('xlmsg').textContent='تم إنشاء الملف ('+ROWS.length+' رياض).'}
  catch(e){$('xlmsg').textContent='تعذّر إنشاء الملف: '+e.message}b.disabled=false};
// دخول الإدارة
async function doLogin(){const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('code').value})});
  if(r.ok)location.reload();else $('lgerr').textContent=(await r.json()).error||'تعذّر الدخول'}
$('login').onclick=doLogin;$('code').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
