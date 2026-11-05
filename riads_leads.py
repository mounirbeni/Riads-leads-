#!/usr/bin/env python3
"""
Riads Leads - Marrakech only.

1) يجمع كل الرياضات (riads) داخل مراكش فقط من OpenStreetMap (Overpass API).
2) يزور موقع كل رياض ويستخرج: الإيميلات + أرقام الواتساب/الهاتف.
3) يحفظ النتيجة في CSV (يفتح مباشرة في Excel).

Usage:
    python3 riads_leads.py                    # كل شيء -> riads_marrakech.csv
    python3 riads_leads.py --no-crawl         # بدون زيارة المواقع (أسرع)
    python3 riads_leads.py --extra-urls my_urls.txt   # أضف مواقع riads من عندك (سطر لكل رابط)
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

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; RiadsLeadsBot/1.0)"}

RIAD_NAME_RE = re.compile(r"\b(riad|ryad|riyad|dar)\b|ryad|riad", re.I)
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


# ----------------------------------------------------------------- OSM
def overpass_query():
    s, w, n, e = MARRAKECH_BBOX
    bbox = f"({s},{w},{n},{e})"
    return f"""
[out:json][timeout:120];
(
  nwr["tourism"~"guest_house|hotel|hostel|apartment"]["name"~"riad|ryad|riyad|^dar |^dar-",i]{bbox};
  nwr["tourism"="guest_house"]["guest_house"="riad"]{bbox};
  nwr["name"~"riad|ryad",i]["tourism"]{bbox};
);
out center tags;
"""


def fetch_osm_riads():
    q = overpass_query()
    last = None
    for url in OVERPASS_MIRRORS:
        try:
            print(f"[osm] {url}")
            r = requests.post(url, data={"data": q}, headers=UA, timeout=150)
            r.raise_for_status()
            return r.json().get("elements", [])
        except Exception as ex:  # noqa: BLE001
            last = ex
            print(f"[osm] failed: {ex}")
            time.sleep(2)
    raise SystemExit(f"Could not reach Overpass API: {last}")


def in_marrakech(lat, lon):
    s, w, n, e = MARRAKECH_BBOX
    return lat is not None and lon is not None and s <= lat <= n and w <= lon <= e


def element_to_lead(el):
    t = el.get("tags", {})
    name = t.get("name") or t.get("name:fr") or t.get("name:en")
    if not name:
        return None
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    if not in_marrakech(lat, lon):
        return None
    city = (t.get("addr:city") or "").lower()
    if city and not re.search(r"marrakech|marrakesh|مراكش", city):
        return None  # مدينة أخرى مكتوبة صراحة -> نستبعد

    phones, whatsapps = [], []
    for k in ("phone", "contact:phone", "mobile", "contact:mobile"):
        for part in re.split(r"[;,]", t.get(k, "")):
            n = normalize_ma_phone(part)
            if n and n not in phones:
                phones.append(n)
    for k in ("contact:whatsapp", "whatsapp"):
        for part in re.split(r"[;,]", t.get(k, "")):
            n = normalize_ma_phone(part)
            if n and n not in whatsapps:
                whatsapps.append(n)
    emails = clean_emails(re.split(r"[;,\s]+", (t.get("email", "") + ";" + t.get("contact:email", ""))))
    website = t.get("website") or t.get("contact:website") or ""
    if website and not website.startswith("http"):
        website = "http://" + website
    return {
        "name": name.strip(),
        "address": " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:suburb")])),
        "lat": lat, "lon": lon,
        "website": website,
        "emails": emails,
        "phones": phones,
        "whatsapps": whatsapps,
        "source": f"osm:{el.get('type')}/{el.get('id')}",
    }


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
        "lat": lead["lat"], "lon": lead["lon"],
        "source": lead["source"],
    }


def read_extra_urls(path):
    leads = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if not u or u.startswith("#"):
                continue
            if not u.startswith("http"):
                u = "https://" + u
            host = urlparse(u).netloc.replace("www.", "")
            leads.append({"name": host, "address": "", "lat": "", "lon": "", "website": u,
                          "emails": [], "phones": [], "whatsapps": [], "source": "extra-urls"})
    return leads


def main():
    ap = argparse.ArgumentParser(description="Riads Marrakech leads: emails + WhatsApp")
    ap.add_argument("-o", "--output", default="riads_marrakech.csv")
    ap.add_argument("--no-crawl", action="store_true", help="skip visiting websites")
    ap.add_argument("--extra-urls", help="text file with extra riad website URLs (one per line)")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    leads, seen = [], set()
    for el in fetch_osm_riads():
        lead = element_to_lead(el)
        if not lead:
            continue
        key = re.sub(r"\W+", "", lead["name"].lower())
        if key in seen:
            continue
        seen.add(key)
        leads.append(lead)
    print(f"[osm] {len(leads)} riads in Marrakech")

    if args.extra_urls:
        known = {urlparse(l["website"]).netloc.replace("www.", "") for l in leads if l["website"]}
        for l in read_extra_urls(args.extra_urls):
            if urlparse(l["website"]).netloc.replace("www.", "") not in known:
                leads.append(l)

    if not args.no_crawl:
        todo = [l for l in leads if l["website"]]
        print(f"[crawl] visiting {len(todo)} websites...")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(enrich, l) for l in todo]
            for i, f in enumerate(as_completed(futs), 1):
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    print(f"  error: {e}", file=sys.stderr)
                if i % 20 == 0:
                    print(f"  {i}/{len(todo)}")

    rows = [finalize(l) for l in leads]
    rows.sort(key=lambda r: (not r["email"], not r["whatsapp"], r["name"].lower()))
    cols = list(rows[0].keys()) if rows else ["name"]
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"[done] {len(rows)} riads | with email: {sum(1 for r in rows if r['email'])} | "
          f"with WhatsApp: {sum(1 for r in rows if r['whatsapp'])} -> {args.output}")


if __name__ == "__main__":
    main()
