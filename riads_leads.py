#!/usr/bin/env python3
"""
Riads Leads - Marrakech only.

1) يجمع كل الرياضات (riads) داخل مراكش فقط من Google Maps (Places API).
2) يزور موقع كل رياض ويستخرج: الإيميلات + أرقام الواتساب/الهاتف.
3) يحفظ النتيجة في CSV (يفتح مباشرة في Excel).

Usage:
    python3 app.py                            # الموقع -> http://localhost:5000
    python3 riads_leads.py --key API_KEY      # بدون موقع -> riads_marrakech.csv
"""
import argparse
import csv
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# حدود مدينة مراكش (south, west, north, east) - أي نتيجة خارجها تُحذف
MARRAKECH_BBOX = (31.55, -8.12, 31.72, -7.90)

UA = {"User-Agent": "Mozilla/5.0 (compatible; RiadsLeadsBot/1.0)"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
JUNK_EMAIL_PARTS = (
    "example.", "sentry", "wixpress", "domain.com", "email.com", "yourdomain",
    "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js",
    "noreply", "no-reply", "godaddy", "schema.org", "w3.org",
)
CONTACT_HINTS = ("contact", "kontakt", "contacto", "reserv", "booking", "book", "about", "a-propos", "nous")


# ----------------------------------------------------------------- phones
def normalize_ma_phone(raw: str):
    """Normalize a Moroccan number to +212XXXXXXXXX. Returns None if invalid."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+"):
        digits = digits[1:]
    elif digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("212"):
        rest = digits[3:]
    elif digits.startswith("0"):
        rest = digits[1:]
    else:
        return None
    rest = rest.lstrip("0") if len(rest) == 10 else rest
    if len(rest) != 9 or rest[0] not in "5678":
        return None
    return "+212" + rest


def is_mobile(num: str) -> bool:
    return bool(num) and num.startswith("+212") and num[4] in "67"


PHONE_RE = re.compile(r"(?:\+|00)?212[\s.\-]?\(?0?\)?[\s.\-]?[5-8](?:[\s.\-]?\d){8}|\b0[5-8](?:[\s.\-]?\d){8}\b")


def extract_phones(text: str):
    out = []
    for m in PHONE_RE.findall(text or ""):
        n = normalize_ma_phone(m)
        if n and n not in out:
            out.append(n)
    return out


# ----------------------------------------------------------------- emails
def clean_emails(candidates):
    out = []
    for e in candidates:
        e = e.strip().strip(".,;:()<>[]\"'").lower()
        if not EMAIL_RE.fullmatch(e):
            continue
        if any(j in e for j in JUNK_EMAIL_PARTS):
            continue
        if e not in out:
            out.append(e)
    return out


def decode_cf_email(hexstr: str):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except Exception:
        return None


# ----------------------------------------------------------------- Google Maps
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.websiteUri",
    "places.googleMapsUri", "places.rating", "places.userRatingCount", "places.types",
    "places.businessStatus", "nextPageToken",
])
QUERIES = [  # مرتبة بالأولوية: الأهم أولاً (البحث محدود بـ 2 دقيقة)
    "riad", "riads in Marrakech", "riads in Marrakesh", "riads",
    "ryad", "riyad", "riad medina Marrakech", "boutique riad Marrakech",
    "riad hotel", "riad spa", "luxury riad", "traditional riad",
    "maison d'hôtes Marrakech", "dar guest house Marrakech", "رياض مراكش",
]
GRID = 6  # GRID x GRID خلايا فوق مراكش لتجاوز حد 60 نتيجة لكل بحث
RIAD_NAME = re.compile(r"riad|ryad|riyad|رياض", re.I)
DAR_NAME = re.compile(r"(^|\s)(dar|دار)[\s\-]", re.I)
LODGING = {"lodging", "guest_house", "bed_and_breakfast", "hotel", "inn", "resort_hotel", "hostel"}


def grid_cells(n=GRID):
    s, w, nn, e = MARRAKECH_BBOX
    dl, dn = (nn - s) / n, (e - w) / n
    for i in range(n):
        for j in range(n):
            yield (s + i * dl, w + j * dn, s + (i + 1) * dl, w + (j + 1) * dn)


class Places(list):
    """نتائج بحث خلية. early=True: توقفنا مبكراً لأن الصفحة كلها معروفة (توفير للحصة)."""
    early = False


def search_cell(api_key, query, cell, stop=lambda: False, stats=None, known=None, out=None):
    """صفحات البحث (حتى 60 نتيجة) داخل خلية واحدة.
    stats: يُزاد stats["requests"] مع كل طلب ناجح إلى Google (لعدّاد الحصة).
    known: مجموعة معرّفات أماكن معروفة؛ إن كانت الصفحة كلها معروفة نتوقف ولا نطلب الصفحات التالية."""
    s, w, n, e = cell
    body = {
        "textQuery": query, "pageSize": 20,
        "locationRestriction": {"rectangle": {
            "low": {"latitude": s, "longitude": w}, "high": {"latitude": n, "longitude": e}}},
    }
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"}
    for _ in range(3):
        if stop():
            return
        r = requests.post(PLACES_URL, json=body, headers=headers, timeout=30)
        if r.status_code != 200:
            try:
                msg = r.json()["error"]["message"]
            except Exception:  # noqa: BLE001
                msg = r.text[:200]
            raise RuntimeError(f"Google API {r.status_code}: {msg}")
        if stats is not None:
            stats["requests"] = stats.get("requests", 0) + 1
        data = r.json()
        page = data.get("places", [])
        yield from page
        tok = data.get("nextPageToken")
        if not tok:
            return
        if known and page and all(p.get("id") in known for p in page):
            if out is not None:
                out.early = True
            return
        body["pageToken"] = tok
        time.sleep(1.5)


class QuotaExhausted(Exception):
    """انتهت حصة كل المفاتيح."""


# ---- ذاكرة الخلايا: متى نعيد فحص كل خلية (توفير طلبات Google) ----
DAY = 86400


def skip_cells(state, now=None, n_cells=None):
    """الخلايا التي لا داعي لفحصها الآن. state: {cell: {"raw": n, "at": ts}}.
    فارغة: كل 14 يوماً. صغيرة (<20): كل 7 أيام. متوسطة (<50): كل 3 أيام. مزدحمة: كل يوم."""
    now = now or time.time()
    skip = set()
    for c, st in (state or {}).items():
        raw, age = st.get("raw", 60), now - st.get("at", 0)
        wait = 14 * DAY if raw == 0 else 7 * DAY if raw < 20 else 3 * DAY if raw < 50 else DAY
        if age < wait:
            skip.add(int(c))
    return sorted(skip)


class KeyPool:
    """عدة مفاتيح Google: توزيع بالتناوب + تحويل تلقائي عند خطأ الحصة (429) + عدّاد استعمال."""

    def __init__(self, keys, on_usage=None, on_exhausted=None):
        self.keys = list(keys)
        self.exhausted = {}  # مفتاح -> وقت العودة
        self.on_usage = on_usage  # دالة (رقم المفتاح، عدد الطلبات)
        self.on_exhausted = on_exhausted  # دالة (رقم المفتاح) عند خطأ الحصة

    def alive(self):
        now = time.time()
        return [k for k in self.keys if self.exhausted.get(k, 0) < now]

    def search(self, q_index, cell_index, cells=None, known=None):
        """نتائج (Places) استعلام واحد في خلية واحدة. يرفع QuotaExhausted إن انتهت كل الحصص."""
        cells = cells or list(grid_cells())
        query, cell = QUERIES[q_index], cells[cell_index]
        keys = self.alive() or self.keys
        start = (q_index + cell_index) % max(len(keys), 1)
        last = None
        for i in range(len(keys)):
            key = keys[(start + i) % len(keys)]
            stats, res = {"requests": 0}, Places()
            try:
                res.extend(search_cell(key, query, cell, stats=stats, known=known, out=res))
                return res
            except RuntimeError as e:
                last = e
                if "Google API 429" in str(e):
                    self.exhausted[key] = time.time() + 600
                    if self.on_exhausted:
                        try:
                            self.on_exhausted(self.keys.index(key))
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                raise
            finally:
                if self.on_usage and stats["requests"]:
                    try:
                        self.on_usage(self.keys.index(key), stats["requests"])
                    except Exception:  # noqa: BLE001
                        pass
        raise QuotaExhausted(str(last))


def in_marrakech(lat, lon):
    s, w, n, e = MARRAKECH_BBOX
    return lat is not None and lon is not None and s <= lat <= n and w <= lon <= e


def place_to_lead(p):
    name = (p.get("displayName") or {}).get("text", "").strip()
    addr = p.get("formattedAddress", "")
    loc = p.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if not name or not in_marrakech(lat, lon):
        return None
    if not re.search(r"marrakech|marrakesh|مراكش", addr, re.I):
        return None  # Marrakech فقط
    if p.get("businessStatus") == "CLOSED_PERMANENTLY":
        return None
    is_lodging = bool(LODGING & set(p.get("types") or []))
    if not (RIAD_NAME.search(name) or (DAR_NAME.search(name) and is_lodging)):
        return None  # riads / dars فقط (لا فنادق عادية ولا مطاعم)
    phones = []
    for key in ("internationalPhoneNumber", "nationalPhoneNumber"):
        n = normalize_ma_phone(p.get(key, ""))
        if n and n not in phones:
            phones.append(n)
    return {
        "name": name, "address": addr, "lat": lat, "lon": lon,
        "website": p.get("websiteUri", ""), "maps_url": p.get("googleMapsUri", ""),
        "rating": p.get("rating", ""), "reviews": p.get("userRatingCount", ""),
        "emails": [], "phones": phones, "whatsapps": [], "source": f"google:{p.get('id')}",
    }


def fetch_google_riads(api_key, progress=lambda m: None, stop=lambda: False):
    leads, seen = {}, set()
    cells = list(grid_cells())
    total = len(cells) * len(QUERIES)
    done = 0
    for q in QUERIES:
        for cell in cells:
            if stop():
                return list(leads.values())
            for p in search_cell(api_key, q, cell, stop):
                if p.get("id") in seen:
                    continue
                seen.add(p.get("id"))
                lead = place_to_lead(p)
                if lead:
                    leads[p["id"]] = lead
            done += 1
            progress(f"بحث Google Maps {done}/{total} - وُجد {len(leads)} رياض")
    return list(leads.values())


# ----------------------------------------------------------------- crawl
def fetch(url, session, timeout=12):
    try:
        r = session.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and "text" in r.headers.get("content-type", "text/html"):
            return r.text
    except Exception:  # noqa: BLE001
        pass
    return None


def parse_page(markup):
    emails, wa, phones = [], [], []
    soup = BeautifulSoup(markup, "html.parser")
    for a in soup.find_all("a", href=True):
        href = html.unescape(a["href"]).strip()
        low = href.lower()
        if low.startswith("mailto:"):
            emails += re.split(r"[;,]", href[7:].split("?")[0])
        elif "wa.me/" in low or "whatsapp.com/send" in low or "api.whatsapp.com" in low or low.startswith("whatsapp:"):
            m = re.search(r"(?:wa\.me/|phone=)(\+?\d+)", low)
            if m:
                n = normalize_ma_phone(m.group(1))
                if n and n not in wa:
                    wa.append(n)
        elif low.startswith("tel:"):
            n = normalize_ma_phone(href[4:])
            if n and n not in phones:
                phones.append(n)
    for tag in soup.select("[data-cfemail]"):
        d = decode_cf_email(tag["data-cfemail"])
        if d:
            emails.append(d)
    text = soup.get_text(" ", strip=True)
    emails += EMAIL_RE.findall(text)
    emails += EMAIL_RE.findall(html.unescape(markup))
    for n in extract_phones(text):
        if n not in phones:
            phones.append(n)
    return clean_emails(emails), wa, phones, soup


def crawl_site(website, max_pages=4):
    session = requests.Session()
    emails, wa, phones = [], [], []
    home = fetch(website, session)
    if not home:
        return emails, wa, phones
    e, w, p, soup = parse_page(home)
    emails += e; wa += w; phones += p
    base = urlparse(website).netloc
    links = []
    for a in soup.find_all("a", href=True):
        u = urljoin(website, a["href"])
        if urlparse(u).netloc == base and any(h in u.lower() for h in CONTACT_HINTS) and u not in links:
            links.append(u)
    for u in links[: max_pages - 1]:
        page = fetch(u, session)
        if page:
            e, w, p, _ = parse_page(page)
            emails += [x for x in e if x not in emails]
            wa += [x for x in w if x not in wa]
            phones += [x for x in p if x not in phones]
    return emails, wa, phones


def enrich(lead):
    if not lead["website"]:
        return lead
    e, w, p = crawl_site(lead["website"])
    for x in e:
        if x not in lead["emails"]:
            lead["emails"].append(x)
    for x in w:
        if x not in lead["whatsapps"]:
            lead["whatsapps"].append(x)
    for x in p:
        if x not in lead["phones"]:
            lead["phones"].append(x)
    return lead


# ----------------------------------------------------------------- output
def finalize(lead):
    # رقم الواتساب: الصريح أولاً، وإلا أول رقم موبايل (06/07) كتخمين
    wa = lead["whatsapps"][0] if lead["whatsapps"] else ""
    wa_kind = "confirmed" if wa else ""
    if not wa:
        mob = [p for p in lead["phones"] if is_mobile(p)]
        if mob:
            wa, wa_kind = mob[0], "likely (mobile)"
    return {
        "name": lead["name"],
        "email": lead["emails"][0] if lead["emails"] else "",
        "other_emails": "; ".join(lead["emails"][1:]),
        "whatsapp": wa,
        "whatsapp_status": wa_kind,
        "whatsapp_link": f"https://wa.me/{wa.lstrip('+')}" if wa else "",
        "phones": "; ".join(lead["phones"]),
        "website": lead["website"],
        "address": lead["address"],
        "maps_url": lead.get("maps_url", ""),
        "rating": lead.get("rating", ""), "reviews": lead.get("reviews", ""),
        "lat": lead["lat"], "lon": lead["lon"],
        "source": lead["source"],
    }


def collect(api_key, crawl=True, workers=10, progress=lambda m: None, stop=lambda: False):
    leads = fetch_google_riads(api_key, progress, stop)
    if crawl:
        todo = [l for l in leads if l["website"]]
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(enrich, l) for l in todo]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception:  # noqa: BLE001
                    pass
                done += 1
                progress(f"زيارة المواقع {done}/{len(todo)} لاستخراج الإيميل والواتساب")
                if stop():
                    break
    rows = [finalize(l) for l in leads]
    rows.sort(key=lambda r: (not r["email"], not r["whatsapp"], r["name"].lower()))
    return rows


def main():
    import os
    ap = argparse.ArgumentParser(description="Riads Marrakech leads (Google Maps)")
    ap.add_argument("-o", "--output", default="riads_marrakech.csv")
    ap.add_argument("--key", default=os.environ.get("GOOGLE_MAPS_API_KEY"))
    ap.add_argument("--no-crawl", action="store_true")
    args = ap.parse_args()
    if not args.key:
        raise SystemExit("ضع GOOGLE_MAPS_API_KEY أو استعمل --key")
    rows = collect(args.key, not args.no_crawl, progress=print)
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["name"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} riads -> {args.output}")


if __name__ == "__main__":
    main()
