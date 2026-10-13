# -*- coding: utf-8 -*-
"""
Altametrics Review Sync (free, no API keys needed)

Automatic : Google Play (google-play-scraper), App Store (Apple public RSS feed)
Manual    : Glassdoor, Indeed, Comparably, G2, Capterra etc. -> manual_reviews.csv

Rules:
- Database kabhi wipe nahi hota, sirf naye reviews add/update hote hain.
- Koi fake/fallback number nahi. Fail hua to dashboard par status "FAILED" dikhega.
"""
import csv
import datetime as dt
import hashlib
import json
import os
import traceback

import requests
from google_play_scraper import app as gp_app, reviews as gp_reviews, Sort

# ---------------- CONFIG ----------------
COMPANY_NAME = "Altametrics"
APP_DISPLAY_NAME = "Apps"

# ---- APPS LIST: naya app add karna ho to yahan ek block copy karke IDs bhar dein ----
# android = Play Store link mein "id=" ke baad wala text
# ios     = App Store link mein "id" ke baad wale numbers
# Kisi app ka ek store na ho to us field ko "" (khaali) chhod dein.
APPS = [
    {"name": "Altametrics Schedules", "android": "com.altametrics.zipschedulesers", "ios": "1207426322"},
    {"name": "Altametrics Clock", "android": "com.altametrics.zipclockers", "ios": "1327860435"},
    {"name": "Altametrics DAR", "android": "com.altametrics.ersdashboard", "ios": "1439365607"},
    {"name": "Altametrics Inventory Pro", "android": "com.altametrics.zipinventory", "ios": "1327917494"},
]
STORE_COUNTRY = "us"
GP_MAX_REVIEWS = 300          # har run mein latest kitne Play reviews laane hain
IOS_RSS_PAGES = 10            # Apple RSS max 10 pages (~500 reviews)

DB_FILE = "reviews_database.json"
MANUAL_CSV = "manual_reviews.csv"
WEB_CSV = "web_reviews.csv"          # laptop wala web_scraper_local.py yeh file bharta hai
WEB_STATUS = "web_status.json"
OUT_HTML = "index.html"
OUT_CSV = "all_reviews.csv"
TEMPLATE = "dashboard_template.html"

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ReviewSync/1.0)"}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def make_id(*parts):
    raw = "|".join(str(p or "").strip().lower() for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------- FETCHERS ----------------
def fetch_google_play(app_name, pkg):
    info = gp_app(pkg, lang="en", country=STORE_COUNTRY)
    result, _ = gp_reviews(
        pkg, lang="en", country=STORE_COUNTRY,
        sort=Sort.NEWEST, count=GP_MAX_REVIEWS,
    )
    reviews = []
    for r in result:
        at = r.get("at")
        reviews.append({
            "id": "gp_" + str(r.get("reviewId")),
            "platform": "Google Play",
            "app": app_name,
            "author": r.get("userName") or "",
            "title": "",
            "rating": r.get("score"),
            "date": at.strftime("%Y-%m-%d") if at else "",
            "text": r.get("content") or "",
        })
    summary = {
        "rating": round(float(info.get("score") or 0), 2),
        "total_ratings": info.get("ratings"),
        "total_reviews": info.get("reviews"),
    }
    return reviews, summary


def fetch_app_store(app_name, app_id):
    # Rating summary
    lookup = requests.get(
        "https://itunes.apple.com/lookup",
        params={"id": app_id, "country": STORE_COUNTRY},
        headers=HTTP_HEADERS, timeout=30,
    ).json()
    res = (lookup.get("results") or [{}])[0]
    summary = {
        "rating": round(float(res.get("averageUserRating") or 0), 2),
        "total_ratings": res.get("userRatingCount"),
        "total_reviews": None,
    }

    reviews = []
    for page in range(1, IOS_RSS_PAGES + 1):
        url = (f"https://itunes.apple.com/{STORE_COUNTRY}/rss/customerreviews/"
               f"page={page}/id={app_id}/sortby=mostrecent/json")
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        if resp.status_code != 200:
            break
        entries = resp.json().get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        page_reviews = [e for e in entries if "im:rating" in e]
        if not page_reviews:
            break
        for e in page_reviews:
            reviews.append({
                "id": "ios_" + e.get("id", {}).get("label", ""),
                "platform": "App Store",
                "app": app_name,
                "author": e.get("author", {}).get("name", {}).get("label", ""),
                "title": e.get("title", {}).get("label", ""),
                "rating": int(e.get("im:rating", {}).get("label", 0) or 0),
                "date": e.get("updated", {}).get("label", "")[:10],
                "text": e.get("content", {}).get("label", ""),
            })
    return reviews, summary


def load_csv_reviews(path):
    """CSV (manual_reviews.csv ya web_reviews.csv) se reviews padhta hai."""
    if not os.path.exists(path):
        return {}
    by_platform = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            platform = (row.get("platform") or "").strip()
            if not platform or platform.startswith("#"):
                continue
            date = (row.get("date") or "").strip()
            try:
                dt.date.fromisoformat(date)
            except ValueError:
                print(f"  ! {path}: galat date '{date}' (YYYY-MM-DD chahiye), row skip")
                continue
            try:
                rating = float(row.get("rating") or 0)
            except ValueError:
                rating = 0
            text = (row.get("text") or "").strip()
            title = (row.get("title") or "").strip()
            by_platform.setdefault(platform, []).append({
                "id": "m_" + make_id(platform, date, title, text[:80]),
                "platform": platform,
                "app": "",
                "author": (row.get("author") or "").strip(),
                "title": title,
                "rating": rating,
                "date": date,
                "text": text,
            })
    return by_platform


# ---------------- DATABASE ----------------
def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, encoding="utf-8") as f:
                db = json.load(f)
            db.setdefault("reviews", {})
            db.setdefault("status", {})
            return db
        except Exception:
            print("  ! Database file corrupt, backup bana raha hoon")
            os.replace(DB_FILE, DB_FILE + ".corrupt.bak")
    return {"reviews": {}, "status": {}}


def save_db(db):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, DB_FILE)


def merge(db, reviews):
    added = 0
    for r in reviews:
        if not r.get("id"):
            continue
        if r["id"] not in db["reviews"]:
            added += 1
        db["reviews"][r["id"]] = r
    return added


def migrate_old_db(db):
    """Purane single-app database ko naye multi-app format mein convert karta hai."""
    first = APPS[0]["name"] if APPS else ""
    for r in db["reviews"].values():
        if "app" not in r:
            r["app"] = first if r["id"].startswith(("gp_", "ios_")) else ""
    for old_key in ("Google Play", "App Store"):
        db["status"].pop(old_key, None)


def run_source(db, name, func, *args):
    status = db["status"].setdefault(name, {})
    status["source"] = "auto"
    try:
        reviews, summary = func(*args)
        added = merge(db, reviews)
        status.update({"ok": True, "last_success": now_iso(), "error": "",
                       "summary": summary, "fetched": len(reviews)})
        print(f"  OK  {name}: {len(reviews)} fetched, {added} new")
    except Exception as e:
        status.update({"ok": False, "last_attempt": now_iso(),
                       "error": f"{type(e).__name__}: {e}"[:300]})
        print(f"  FAIL {name}: {e}")
        traceback.print_exc()


# ---------------- OUTPUT ----------------
def write_outputs(db):
    reviews = sorted(db["reviews"].values(), key=lambda r: r.get("date", ""), reverse=True)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["platform", "app", "author", "title", "rating", "date", "text"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(reviews)

    payload = {
        "company": COMPANY_NAME,
        "app": APP_DISPLAY_NAME,
        "generated": now_iso(),
        "status": db["status"],
        "reviews": reviews,
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(tpl.replace("__REVIEW_DATA__", data_json))


def main():
    print("=== Review Sync start ===")
    db = load_db()
    migrate_old_db(db)

    for a in APPS:
        if a.get("android"):
            run_source(db, f"Google Play · {a['name']}", fetch_google_play, a["name"], a["android"])
        if a.get("ios"):
            run_source(db, f"App Store · {a['name']}", fetch_app_store, a["name"], str(a["ios"]))

    for platform, rows in load_csv_reviews(MANUAL_CSV).items():
        added = merge(db, rows)
        db["status"][platform] = {"ok": True, "source": "manual", "last_success": now_iso(),
                                  "error": "", "fetched": len(rows)}
        print(f"  CSV {platform}: {len(rows)} rows, {added} new")

    for platform, rows in load_csv_reviews(WEB_CSV).items():
        added = merge(db, rows)
        print(f"  WEB {platform}: {len(rows)} rows, {added} new")
    if os.path.exists(WEB_STATUS):
        try:
            with open(WEB_STATUS, encoding="utf-8") as f:
                for platform, st in json.load(f).items():
                    db["status"][platform] = dict(st, source="laptop")
        except ValueError:
            print(f"  ! {WEB_STATUS} padh nahi paya")

    save_db(db)
    write_outputs(db)
    print(f"=== Done: {len(db['reviews'])} total reviews in database ===")


if __name__ == "__main__":
    main()
