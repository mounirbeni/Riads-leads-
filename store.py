"""تخزين دائم مشترك (Vercel Blob) + إزالة التكرار نهائياً.

كل المستخدمين يرون نفس القاعدة. بدون BLOB_READ_WRITE_TOKEN (تشغيل محلي) يُستعمل ملف data/riads.json.
"""
import json
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

BLOB_PATH = "riads.json"
LOCAL_PATH = os.path.join(os.path.dirname(__file__), "data", "riads.json")
USE_BLOB = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))

# مواقع مشتركة بين عدة رياضات: لا تصلح كمفتاح تكرار
SHARED_DOMAINS = ("facebook.", "instagram.", "booking.", "tripadvisor.", "airbnb.", "expedia.",
                  "agoda.", "hotels.", "google.", "linktr.ee", "wa.me", "whatsapp.", "youtube.",
                  "twitter.", "x.com", "tiktok.", "wixsite.", "business.site")
STOP = {"riad", "riads", "ryad", "ryads", "riyad", "hotel", "spa", "marrakech", "marrakesh",
        "medina", "the", "and", "de", "du", "des", "la", "le", "el", "al", "d", "of", "maison", "dhotes"}


# ----------------------------------------------------------------- storage
# سجلّ إضافات فقط (append-only): كل حفظ = ملف جديد باسم فريد، لا كتابة فوق ملف قديم أبداً.
# هذا يمنع ضياع البيانات عند حفظين متزامنين أو عند قراءة نسخة قديمة من الكاش.
LOG_PREFIX = "log/"


def _read_bytes(res):
    body = res.body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if hasattr(body, "read"):
        return body.read()
    return b"".join(body)


def _blob_json(pathname):
    from vercel.blob import sync as blob
    return json.loads(_read_bytes(blob.get_blob(pathname, access="private")).decode("utf-8"))


def _apply(rows, op):
    if op.get("op") == "del":
        gone = set(op.get("sources", []))
        return [r for r in rows if not (sources_of(r) & gone)]
    return add_rows(rows, op.get("rows", []))[0]


def load():
    if not USE_BLOB:
        try:
            with open(LOCAL_PATH, encoding="utf-8") as f:
                return json.load(f).get("rows", [])
        except FileNotFoundError:
            return []
    from concurrent.futures import ThreadPoolExecutor
    from vercel.blob import sync as blob
    rows = []
    try:  # النسخة القديمة (قبل نظام السجلّ) إن وُجدت
        rows = _blob_json(BLOB_PATH).get("rows", [])
    except Exception as e:  # noqa: BLE001
        if e.__class__.__name__ != "BlobNotFoundError":
            raise
    names = sorted(b.pathname for b in blob.query_blobs(prefix=LOG_PREFIX))
    with ThreadPoolExecutor(max_workers=16) as ex:
        ops = list(ex.map(_blob_json, names))
    for op in ops:  # بترتيب الزمن
        rows = _apply(rows, op)
    return sorted(rows, key=lambda r: (not r.get("email"), not r.get("whatsapp"), (r.get("name") or "").lower()))


def _append(op):
    if USE_BLOB:
        from vercel.blob import sync as blob
        name = f"{LOG_PREFIX}{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}.json"
        blob.put_blob(name, json.dumps(op, ensure_ascii=False).encode("utf-8"), access="private",
                      content_type="application/json", add_random_suffix=False, allow_overwrite=False)
    else:  # محلياً: ملف واحد
        rows = _apply(load(), op)
        os.makedirs(os.path.dirname(LOCAL_PATH), exist_ok=True)
        with open(LOCAL_PATH, "w", encoding="utf-8") as f:
            json.dump({"rows": rows}, f, ensure_ascii=False)


def save_add(incoming):
    """يحفظ نتائج جديدة. يعيد (كل الصفوف، عدد المضاف، مصادر المضاف)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    incoming = [dict(r, added_at=r.get("added_at") or now) for r in incoming]
    current = load()
    rows, added, added_src = add_rows(current, incoming)
    if incoming:
        _append({"op": "add", "rows": incoming})
    return rows, added, added_src


def delete(sources):
    _append({"op": "del", "sources": list(sources)})
    return load()


# ----------------------------------------------------------------- dedupe
def _ascii(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def name_tokens(name):
    toks = set(re.findall(r"[a-z0-9]+", _ascii(name))) - STOP
    return toks or set(re.findall(r"[a-z0-9]+", _ascii(name))) or {name.strip().lower()}


def domain(url):
    try:
        h = (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""
    return "" if not h or any(s in h for s in SHARED_DOMAINS) else h


def phones_of(row):
    return {p.strip() for p in (row.get("phones") or "").split(";") if p.strip()}


def sources_of(row):
    return {s.strip() for s in (row.get("source") or "").split(";") if s.strip()}


def is_dup(a, b):
    if sources_of(a) & sources_of(b):
        return True
    da, db = domain(a.get("website", "")), domain(b.get("website", ""))
    if da and da == db:
        return True
    ta, tb = name_tokens(a.get("name", "")), name_tokens(b.get("name", ""))
    if ta == tb:
        return True
    if phones_of(a) & phones_of(b) and ta & tb:
        return True
    return False


def _union(*vals):
    out = []
    for v in vals:
        for x in re.split(r"[;,]\s*", v or ""):
            x = x.strip()
            if x and x not in out:
                out.append(x)
    return out


def merge_into(old, new):
    """يكمّل الحقول الناقصة في old من new دون إنشاء صف جديد."""
    emails = _union(old.get("email"), old.get("other_emails"), new.get("email"), new.get("other_emails"))
    old["email"], old["other_emails"] = (emails[0] if emails else ""), "; ".join(emails[1:])
    old["phones"] = "; ".join(_union(old.get("phones"), new.get("phones")))
    if new.get("whatsapp_status") == "confirmed" and old.get("whatsapp_status") != "confirmed":
        for k in ("whatsapp", "whatsapp_status", "whatsapp_link"):
            old[k] = new.get(k, "")
    for k in ("whatsapp", "whatsapp_link", "whatsapp_status", "website", "address", "maps_url",
              "rating", "reviews", "lat", "lon"):
        if old.get(k) in ("", None) and new.get(k) not in ("", None):
            old[k] = new[k]
    old["source"] = "; ".join(sorted(sources_of(old) | sources_of(new)))
    return old


def add_rows(stored, incoming):
    """يضيف الجديد فقط. يعيد (rows, عدد المضاف, مصادر المضاف)."""
    rows = list(stored)
    added, added_src = 0, set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for new in incoming:
        hit = next((r for r in rows if is_dup(r, new)), None)
        if hit:
            merge_into(hit, new)
        else:
            new = dict(new)
            new.setdefault("added_at", now)
            rows.append(new)
            added += 1
            added_src |= sources_of(new)
    rows.sort(key=lambda r: (not r.get("email"), not r.get("whatsapp"), (r.get("name") or "").lower()))
    return rows, added, sorted(added_src)
