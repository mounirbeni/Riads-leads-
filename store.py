"""تخزين دائم مشترك (Vercel Blob) + إزالة التكرار نهائياً.

كل المستخدمين يرون نفس القاعدة. بدون BLOB_READ_WRITE_TOKEN (تشغيل محلي) يُستعمل ملف data/riads.json.
"""
import json
import os
import re
import threading
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
SNAP_PREFIX = "snap/"


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


def _ts(name):
    """الوقت (13 رقماً) من اسم ملف مثل log/1793889164165-ab12cd34.json"""
    return name.rsplit("/", 1)[-1][:13]


def _list(prefix):
    from vercel.blob import sync as blob
    return sorted(b.pathname for b in blob.query_blobs(prefix=prefix))


def _base_rows():
    """آخر لقطة (snap/) أو النسخة القديمة. يعيد (rows, cutoff)."""
    snaps = _list(SNAP_PREFIX)
    if snaps:
        return _blob_json(snaps[-1]).get("rows", []), _ts(snaps[-1])
    try:
        return _blob_json(BLOB_PATH).get("rows", []), "0" * 13
    except Exception as e:  # noqa: BLE001
        if e.__class__.__name__ != "BlobNotFoundError":
            raise
        return [], "0" * 13


def _sort(rows):
    return sorted(rows, key=lambda r: (not r.get("email"), not r.get("whatsapp"), (r.get("name") or "").lower()))


def load():
    if not USE_BLOB:
        try:
            with open(LOCAL_PATH, encoding="utf-8") as f:
                return json.load(f).get("rows", [])
        except FileNotFoundError:
            return []
    from concurrent.futures import ThreadPoolExecutor
    rows, cutoff = _base_rows()
    names = [n for n in _list(LOG_PREFIX) if _ts(n) >= cutoff]  # ما قبل اللقطة مدموج فيها
    with ThreadPoolExecutor(max_workers=16) as ex:
        ops = list(ex.map(_blob_json, names))
    for op in ops:  # بترتيب الزمن
        rows = _apply(rows, op)
    _maybe_compact(cutoff, names)
    return _sort(rows)


def _maybe_compact(cutoff, names):
    """يدمج السجلات الأقدم من دقيقة في لقطة واحدة فتصير القراءة أسرع. لا يحذف شيئاً."""
    if not USE_BLOB:
        return
    limit = f"{int((time.time() - 60) * 1000):013d}"
    old = [n for n in names if _ts(n) < limit]
    if len(old) < 8:
        return
    try:
        from vercel.blob import sync as blob
        rows, _ = _base_rows()
        for op in (_blob_json(n) for n in old):
            rows = _apply(rows, op)
        blob.put_blob(f"{SNAP_PREFIX}{limit}-{uuid.uuid4().hex[:6]}.json",
                      json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8"), access="private",
                      content_type="application/json", add_random_suffix=False, allow_overwrite=False)
    except Exception:  # noqa: BLE001
        pass  # الضغط اختياري؛ لا يؤثر على القراءة


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
    """يحفظ فقط (سريع، بلا قراءة). يعيد عدد الصفوف المحفوظة."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    incoming = [dict(r, added_at=r.get("added_at") or now) for r in incoming]
    if incoming:
        _append({"op": "add", "rows": incoming})
    return len(incoming)


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
    if new.get("crawled"):
        old["crawled"] = 1
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


# ----------------------------------------------------------------- سجل البحث التلقائي
RUN_PREFIX = "runs/"
LOCAL_RUN = os.path.join(os.path.dirname(__file__), "data", "last_run.json")


def record_run(info):
    if USE_BLOB:
        from vercel.blob import sync as blob
        name = f"{RUN_PREFIX}{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:6]}.json"
        blob.put_blob(name, json.dumps(info, ensure_ascii=False).encode("utf-8"), access="private",
                      content_type="application/json", add_random_suffix=False, allow_overwrite=False)
    else:
        os.makedirs(os.path.dirname(LOCAL_RUN), exist_ok=True)
        with open(LOCAL_RUN, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)


def last_run():
    try:
        if USE_BLOB:
            names = _list(RUN_PREFIX)
            return _blob_json(names[-1]) if names else None
        with open(LOCAL_RUN, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------- معرّفات معروفة (توقّف مبكر عن تصفّح الصفحات)
_KNOWN = {"at": 0.0, "ids": set()}


def known_ids(ttl=60):
    """معرّفات Google Place للرياضات المحفوظة (كاش دقيقة)."""
    if time.time() - _KNOWN["at"] > ttl:
        ids = set()
        for r in load():
            for s in sources_of(r):
                if s.startswith("google:"):
                    ids.add(s[7:])
        _KNOWN.update(at=time.time(), ids=ids)
    return _KNOWN["ids"]


# ----------------------------------------------------------------- ذاكرة الخلايا
CELLS_PREFIX = "cells/"
LOCAL_CELLS = os.path.join(os.path.dirname(__file__), "data", "cells.json")


def record_cells(raw):
    """raw: {cell_index: عدد نتائج الكلمة الأولى}. يُحفظ ملف جديد لكل تشغيل (لا كتابة فوق شيء)."""
    if not raw:
        return
    info = {"at": time.time(), "raw": {str(k): int(v) for k, v in raw.items()}}
    if USE_BLOB:
        from vercel.blob import sync as blob
        name = f"{CELLS_PREFIX}{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:6]}.json"
        blob.put_blob(name, json.dumps(info).encode(), access="private", content_type="application/json",
                      add_random_suffix=False, allow_overwrite=False)
    else:
        os.makedirs(os.path.dirname(LOCAL_CELLS), exist_ok=True)
        old = cell_state()
        for k, v in info["raw"].items():
            old[k] = {"raw": v, "at": info["at"]}
        with open(LOCAL_CELLS, "w") as f:
            json.dump(old, f)


def cell_state(last_n=12):
    """{cell: {"raw", "at"}} بدمج آخر الملفات (الأحدث يغلب)."""
    state = {}
    try:
        if USE_BLOB:
            from concurrent.futures import ThreadPoolExecutor
            names = _list(CELLS_PREFIX)[-last_n:]
            with ThreadPoolExecutor(max_workers=8) as ex:
                infos = list(ex.map(_blob_json, names))
            for info in infos:  # من الأقدم للأحدث
                for k, v in info.get("raw", {}).items():
                    state[k] = {"raw": v, "at": info.get("at", 0)}
        elif os.path.exists(LOCAL_CELLS):
            with open(LOCAL_CELLS) as f:
                state = json.load(f)
    except Exception:  # noqa: BLE001
        pass
    return state


# ----------------------------------------------------------------- عدّاد استعمال المفاتيح (يوم باسيفيك)
USAGE_PREFIX = "usage/"
LOCAL_USAGE = os.path.join(os.path.dirname(__file__), "data", "usage.json")
_LOCK = threading.Lock()


def pacific_day():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def record_usage(key_index, n):
    """يسجّل n طلباً لمفتاح. في Blob: الاسم نفسه يحمل البيانات (usage/اليوم/مفتاح-عدد-وقت) فيُقرأ بالقائمة دون تنزيل."""
    day = pacific_day()
    if USE_BLOB:
        from vercel.blob import sync as blob
        name = f"{USAGE_PREFIX}{day}/{key_index}-{n}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"
        blob.put_blob(name, b"1", access="private", content_type="text/plain",
                      add_random_suffix=False, allow_overwrite=False)
    else:
        os.makedirs(os.path.dirname(LOCAL_USAGE), exist_ok=True)
        with _LOCK:
            try:
                with open(LOCAL_USAGE) as f:
                    u = json.load(f)
            except Exception:  # noqa: BLE001
                u = {}
            u.setdefault(day, {})[str(key_index)] = u.get(day, {}).get(str(key_index), 0) + n
            with open(LOCAL_USAGE, "w") as f:
                json.dump(u, f)


def mark_exhausted(key_index):
    """علامة «انتهت حصة هذا المفتاح اليوم» (تبقى حتى تجدد الحصة)."""
    day = pacific_day()
    if USE_BLOB:
        from vercel.blob import sync as blob
        name = f"{USAGE_PREFIX}{day}/E{key_index}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"
        blob.put_blob(name, b"1", access="private", content_type="text/plain",
                      add_random_suffix=False, allow_overwrite=False)
    else:
        os.makedirs(os.path.dirname(LOCAL_USAGE), exist_ok=True)
        with _LOCK:
            try:
                with open(LOCAL_USAGE) as f:
                    u = json.load(f)
            except Exception:  # noqa: BLE001
                u = {}
            u.setdefault(day, {})["E" + str(key_index)] = 1
            with open(LOCAL_USAGE, "w") as f:
                json.dump(u, f)


def usage_today():
    """(used, exhausted): used = {رقم المفتاح: عدد الطلبات اليوم}، exhausted = {أرقام المفاتيح المنتهية}."""
    day = pacific_day()
    used, dead = {}, set()
    try:
        if USE_BLOB:
            for name in _list(f"{USAGE_PREFIX}{day}/"):
                part = name.rsplit("/", 1)[-1].split("-")
                if part[0].startswith("E"):
                    dead.add(int(part[0][1:]))
                else:
                    used[int(part[0])] = used.get(int(part[0]), 0) + int(part[1])
        elif os.path.exists(LOCAL_USAGE):
            with open(LOCAL_USAGE) as f:
                for k, v in json.load(f).get(day, {}).items():
                    if k.startswith("E"):
                        dead.add(int(k[1:]))
                    else:
                        used[int(k)] = v
    except Exception:  # noqa: BLE001
        pass
    return used, dead
