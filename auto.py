"""البحث التلقائي اليومي (يُشغَّل من Vercel Cron أو من زر «تشغيل الآن»).

داخل حدّ وقت الدالة (≈ 60 ث):
  1) بحث Google Maps بنفس منطق الواجهة (الكلمة الأولى على كل الخلايا، والبقية على الخلايا المزدحمة فقط).
  2) حفظ الرياضات الجديدة فوراً (بلا تكرار).
  3) زيارة مواقع الجدد، ثم ما لم يُفحص موقعه من المحفوظ، لاستخراج الإيميل والواتساب.
"""
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

import riads_leads as core
import store

SEARCH_SHARE = 0.55  # حصة البحث من الميزانية الكلية
MAX_ENRICH = 60


def _lead_from_row(r):
    emails = [e for e in [r.get("email", "")] + str(r.get("other_emails", "")).split(";") if e.strip()]
    return {
        "name": r.get("name", ""), "address": r.get("address", ""), "lat": r.get("lat", ""), "lon": r.get("lon", ""),
        "website": r.get("website", ""), "maps_url": r.get("maps_url", ""), "rating": r.get("rating", ""),
        "reviews": r.get("reviews", ""), "source": r.get("source", ""),
        "emails": [e.strip() for e in emails],
        "phones": [p.strip() for p in str(r.get("phones", "")).split(";") if p.strip()],
        "whatsapps": [r["whatsapp"]] if r.get("whatsapp") and r.get("whatsapp_status") == "confirmed" else [],
    }


def run(keys, budget=50, public_url=lambda u: True):
    t0 = time.time()
    info = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "new": 0, "enriched": 0,
            "searches": 0, "found": 0, "quota": False, "error": ""}
    pool = core.KeyPool(keys, on_usage=store.record_usage, on_exhausted=store.mark_exhausted)
    cells = list(core.grid_cells())
    try:
        stored = store.load()
        known = set()
        for r in stored:
            known |= store.sources_of(r)

        known_ids = store.known_ids()
        skip = set(core.skip_cells(store.cell_state()))  # خلايا فُحصت حديثاً: لا داعي لإعادتها
        info["skipped_cells"] = len(skip)

        # 1) البحث بنظام الأدوار: كل مفتاح يبحث دفعته ثم تُحفظ قائمته، ثم المفتاح التالي
        found, sat, raw0, saved = {}, {}, {}, set()
        by_key = {}
        search_end = t0 + budget * SEARCH_SHARE
        turn = 0
        for q in range(len(core.QUERIES)):
            todo = [c for c in range(len(cells)) if (c not in skip if q == 0 else sat.get(c, 0) >= 50)]
            if not todo or time.time() > search_end or info["quota"]:
                break
            for batch in core.turn_batches(todo, len(pool.alive_idx()) or len(keys)):
                alive = pool.alive_idx()
                if not alive or time.time() > search_end:
                    info["quota"] = info["quota"] or not alive
                    break
                key_i = alive[turn % len(alive)]
                turn += 1

                def one(c, q=q, key_i=key_i):
                    if time.time() > search_end or info["quota"]:
                        return
                    try:
                        places = pool.search(q, c, cells, known=known_ids, prefer=key_i)
                    except core.QuotaExhausted:
                        info["quota"] = True
                        return
                    except Exception as e:  # noqa: BLE001
                        info["error"] = str(e)[:200]
                        return
                    info["searches"] += 1
                    n = 60 if places.early else len(places)
                    sat[c] = max(sat.get(c, 0), n)
                    if q == 0:
                        raw0[c] = n
                    for p in places:
                        lead = core.place_to_lead(p)
                        if lead:
                            found[lead["source"]] = lead
                            by_key[places.key_index] = by_key.get(places.key_index, 0) + 1

                with ThreadPoolExecutor(max_workers=6) as ex:
                    list(ex.map(one, batch))
                # قائمة هذا المفتاح تُحفظ فوراً قبل أن يبدأ دور المفتاح التالي
                fresh = [l for s, l in found.items() if s not in known and s not in saved]
                if fresh:
                    store.save_add([core.finalize(l) for l in fresh])
                    saved.update(l["source"] for l in fresh)
        info["by_key"] = {str(k + 1): v for k, v in sorted(by_key.items())}
        info["found"] = len(found)
        try:
            store.record_cells(raw0)
        except Exception:  # noqa: BLE001
            pass

        # 2) حفظ الجدد فوراً
        new = [l for s, l in found.items() if s not in known]
        info["new"] = len(new)
        pending = [l for l in new if l["source"] not in saved]
        if pending:
            store.save_add([core.finalize(l) for l in pending])

        # 3) استخراج الإيميل/الواتساب: الجدد أولاً ثم ما لم يُفحص
        queue = [l for l in new if l["website"]]
        queue += [_lead_from_row(r) for r in stored if r.get("website") and not r.get("crawled")]
        queue = [l for l in queue if public_url(l["website"])][:MAX_ENRICH]
        end = t0 + budget
        done_rows = []

        def crawl(lead):
            if time.time() > end - 6:
                return None
            core.enrich(lead)
            row = core.finalize(lead)
            row["crawled"] = 1
            return row

        ex = ThreadPoolExecutor(max_workers=12)
        futs = [ex.submit(crawl, l) for l in queue]
        wait(futs, timeout=max(1, end - time.time()))
        for f in futs:
            if f.done() and not f.cancelled() and not f.exception() and f.result():
                done_rows.append(f.result())
        ex.shutdown(wait=False, cancel_futures=True)
        if done_rows:
            store.save_add(done_rows)
        info["enriched"] = len(done_rows)
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)[:300]
    info["seconds"] = round(time.time() - t0, 1)
    try:
        store.record_run(info)
    except Exception:  # noqa: BLE001
        pass
    return info
