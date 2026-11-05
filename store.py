"""تخزين دائم مشترك (Vercel Blob) + إزالة التكرار نهائياً.

كل المستخدمين يرون نفس القاعدة. بدون BLOB_READ_WRITE_TOKEN (تشغيل محلي) يُستعمل ملف data/riads.json.
"""
import json
import os
import re
import unicodedata
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
def _read_bytes(res):
    body = res.body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if hasattr(body, "read"):
        return body.read()
    return b"".join(body)


def load():
    try:
        if USE_BLOB:
            from vercel.blob import sync as blob
            res = blob.get_blob(BLOB_PATH, access="private")
            return json.loads(_read_bytes(res).decode("utf-8")).get("rows", [])
        with open(LOCAL_PATH, encoding="utf-8") as f:
            return json.load(f).get("rows", [])
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001
        if e.__class__.__name__ == "BlobNotFoundError":
            return []
        raise


def save(rows):
    data = json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8")
    if USE_BLOB:
        from vercel.blob import sync as blob
        blob.put_blob(BLOB_PATH, data, access="private", content_type="application/json",
                      add_random_suffix=False, allow_overwrite=True, cache_control_max_age=60)
    else:
        os.makedirs(os.path.dirname(LOCAL_PATH), exist_ok=True)
        with open(LOCAL_PATH, "wb") as f:
            f.write(data)


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
