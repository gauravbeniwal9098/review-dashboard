# -*- coding: utf-8 -*-
"""
Web platforms (Glassdoor, Indeed, Comparably ...) ke reviews LAPTOP se automatic laata hai.

Kyun laptop? Ye sites cloud servers (GitHub Actions) ko block karti hain, lekin aapke
normal internet + asli Chrome browser ko aam user ki tarah dekhti hain.

Pehli baar:  python web_scraper_local.py --setup   (browser khulega, login/captcha kar lein)
Roz:         python web_scraper_local.py           (Task Scheduler se automatic)
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import time
from collections import Counter
from html import unescape

# ---------------- CONFIG ----------------
# Har site par browser mein company ka REVIEWS page kholein, "Most Recent / Newest" sort chunein,
# phir address bar se poora URL copy karke yahan paste karein. Khaali URL wali site skip hogi.
WEB_SOURCES = [
    {"platform": "Glassdoor", "url": "https://www.glassdoor.co.in/Reviews/Altametrics-Reviews-E269429.htm?sort.sortType=RD"},
    {"platform": "Indeed", "url": "https://www.indeed.com/cmp/Altametrics/reviews?sort=date_desc"},
    {"platform": "Comparably", "url": "https://www.comparably.com/companies/altametrics/reviews"},
    {"platform": "G2", "url": "https://www.g2.com/products/altametrics/reviews?order=most_recent"},
    {"platform": "Capterra", "url": "https://www.capterra.in/software/16156/erestaurant?sort=most_recent"},
    {"platform": "Software Advice", "url": "https://www.softwareadvice.com/inventory-management/altametrics-profile/reviews/"},
    {"platform": "Yelp", "url": "https://www.yelp.com/biz/altametrics-costa-mesa?sort_by=date_desc"},
]

OUT_CSV = "web_reviews.csv"
STATUS_FILE = "web_status.json"
SNAPSHOT_DIR = "snapshots"        # har site ka page yahan save hota hai (debug ke liye)
PROFILE_DIR = "browser_profile"   # aapka login yahan save rehta hai. KABHI GitHub par upload na karein.
AUTO_GIT_PUSH = True

FIELDS = ["platform", "author", "title", "rating", "date", "text"]

# ---------------- REVIEW EXTRACTOR ----------------
# Sites apne page ke andar reviews ka data JSON mein rakhti hain. Yeh code us JSON mein
# woh objects dhoondhta hai jinme rating + date + text teeno hon. Isse site ka design badalne
# par bhi zyaadatar kaam chalta rehta hai.
RATING_KEYS = ("ratingOverall", "overallRating", "reviewRating", "rating", "ratingValue",
               "starRating", "stars", "score", "overall")
DATE_KEYS = ("reviewDateTime", "writtenOn", "datePublished", "submissionDate", "reviewDate", "dateCreated",
             "createdAt", "publishedAt", "publishDate", "submittedAt", "created", "date")
TITLE_KEYS = ("summary", "headline", "title", "reviewTitle", "name")
BODY_KEYS = ("reviewBody", "reviewText", "text", "body", "content", "comment", "description")
PROS_CONS = (("generalComments", "Comments"), ("pros", "Pros"), ("prosText", "Pros"),
             ("cons", "Cons"), ("consText", "Cons"), ("advice", "Advice"),
             ("adviceToManagement", "Advice"))
AUTHOR_KEYS = ("jobTitle", "authorName", "author", "reviewer", "position", "role")
LOCATION_KEYS = ("location", "reviewerLocation", "city")


class Extractor:
    def __init__(self):
        self.refs = {}

    def collect_refs(self, blob):
        # Apollo/GraphQL cache: {"JobTitle:123": {...}} jaise references (kisi bhi depth par)
        stack, seen = [blob], 0
        while stack and seen < 500_000:
            node = stack.pop()
            seen += 1
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, dict) and re.match(r"^[A-Za-z_]+:\S+$", str(k)):
                        self.refs[k] = v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)

    def resolve(self, v):
        for _ in range(3):
            if isinstance(v, dict) and "__ref" in v and v["__ref"] in self.refs:
                v = self.refs[v["__ref"]]
            else:
                break
        return v

    def text(self, v, depth=0):
        v = self.resolve(v)
        if v is None or depth > 3:
            return ""
        if isinstance(v, str):
            return re.sub(r"\s+", " ", v).strip()
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
        if isinstance(v, dict):
            if v.get("firstName"):
                last = (v.get("lastName") or "").strip()
                return f"{v['firstName'].strip()} {last[:1] + '.' if last else ''}".strip()
            for k in ("text", "name", "value", "jobTitle", "body", "label", "displayName"):
                if k in v:
                    t = self.text(v[k], depth + 1)
                    if t:
                        return t
        if isinstance(v, list):
            return " ".join(filter(None, (self.text(x, depth + 1) for x in v[:10])))
        return ""

    def rating(self, v):
        v = self.resolve(v)
        if isinstance(v, dict):
            for k in ("ratingValue", "value", "rating", "overall"):
                if k in v:
                    return self.rating(v[k])
            return None
        try:
            r = float(v)
        except (TypeError, ValueError):
            return None
        return r if 0 < r <= 5 else None

    @staticmethod
    def date(v):
        if v is None or isinstance(v, bool):
            return ""
        if isinstance(v, dict):
            for k in ("date", "value", "text"):
                if k in v:
                    return Extractor.date(v[k])
            return ""
        if isinstance(v, (int, float)):
            ts = v / 1000 if v > 1e11 else v
            if 1e9 < ts < 4e9:
                return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
            return ""
        s = str(v).strip()
        if s.isdigit():
            return Extractor.date(int(s))
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%m/%d/%Y", "%b %d %Y"):
            try:
                return dt.datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                pass
        return ""

    def first(self, d, keys, fn):
        for k in keys:
            if k in d:
                val = fn(d[k])
                if val:
                    return val
        return None

    def as_review(self, d, platform):
        rating = self.first(d, RATING_KEYS, self.rating)
        date = self.first(d, DATE_KEYS, self.date)
        if not rating or not date:
            return None
        if not ("2005-01-01" <= date <= (dt.date.today() + dt.timedelta(days=1)).isoformat()):
            return None

        parts = []
        for key, label in PROS_CONS:
            t = self.text(d.get(key))
            if t:
                parts.append(f"{label}: {t}")
        body = " | ".join(parts) if parts else (self.first(d, BODY_KEYS, self.text) or "")
        title = self.first(d, TITLE_KEYS, self.text) or ""
        if title == body:
            title = ""
        if len(title + body) < 15:
            return None

        author = self.first(d, AUTHOR_KEYS, self.text) or ""
        loc = self.first(d, LOCATION_KEYS, self.text) or ""
        if loc and loc not in author:
            author = f"{author}, {loc}" if author else loc
        return {"platform": platform, "author": author[:150], "title": title[:300],
                "rating": round(rating, 1), "date": date, "text": body[:5000]}

    def walk(self, blob, platform):
        found, stack, seen = [], [blob], 0
        while stack and seen < 2_000_000:
            node = stack.pop()
            seen += 1
            if isinstance(node, dict):
                r = self.as_review(node, platform)
                if r:
                    found.append(r)
                    continue
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return found


def next_flight_blobs(html):
    """Next.js (App Router) pages data ko self.__next_f.push([1,"..."]) mein bhejte hain."""
    chunks = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html):
        try:
            chunks.append(json.loads(m.group(1)))
        except ValueError:
            pass
    blobs = []
    for line in "".join(chunks).split("\n"):
        m = re.match(r"^[0-9a-zA-Z]+:([A-Z]{0,2})([\[{].*)$", line)
        if not m:
            continue
        try:
            blobs.append(json.loads(m.group(2)))
        except ValueError:
            pass
    return blobs


def html_card_reviews(html, platform):
    """Capterra jaisi sites jo reviews seedha HTML cards mein dikhati hain."""
    def clean(x):
        x = re.sub(r"<[^>]+>", " ", x or "")
        return re.sub(r"\s+", " ", unescape(x)).strip()

    out = []
    starts = [m.start() for m in re.finditer(r'<div class="[^"]*review-card[^"]*"[^>]*data-entity="review"', html)]
    for i, st in enumerate(starts):
        card = html[st: starts[i + 1] if i + 1 < len(starts) else st + 60000]
        card = re.sub(r'data-bs-title="[^"]*"', "", card)
        g = lambda pat: (re.search(pat, card, re.S) or [None, ""])[1]
        name = clean(g(r'<div class="fw-600 mb-1">(.*?)</div>'))
        role = clean(g(r'<div class="fs-4 text-neutral-90 mb-1">(.*?)</div>'))
        title = clean(g(r"<h3[^>]*>(.*?)</h3>")).strip('"\u201c\u201d ')
        date = Extractor.date(clean(g(r'<div class="fs-5 text-neutral-90[^"]*">(.*?)</div>')))
        try:
            rating = float(clean(g(r'<span class="ms-1">(.*?)</span>')))
        except ValueError:
            rating = None
        comments = clean(g(r'<div class="fs-4 lh-2 text-neutral-99">\s*<span>(.*?)</span>'))
        pros = clean(g(r'Pros:\s*</div>\s*<div class="fs-4 lh-2 text-neutral-99">(.*?)</div>'))
        cons = clean(g(r'Cons:\s*</div>\s*<div class="fs-4 lh-2 text-neutral-99">(.*?)</div>'))
        parts = [f"Comments: {comments}" if comments else "", f"Pros: {pros}" if pros else "",
                 f"Cons: {cons}" if cons else ""]
        body = " | ".join(p for p in parts if p)
        if date and rating and 0 < rating <= 5 and (title or body):
            author = ", ".join(x for x in (name, role) if x)
            out.append({"platform": platform, "author": author[:150], "title": title[:300],
                        "rating": round(rating, 1), "date": date, "text": body[:5000]})
    return out


# Comparably: reviews questions/groups mein hote hain, star rating nahi hoti.
# Yeh questions "constructive" (sudhaar wale) hain, baaki positive.
CMP_CONSTRUCTIVE = {"90", "94", "95", "96", "97", "98", "192"}


def comparably_reviews(html, platform):
    def clean(x):
        x = re.sub(r"<[^>]+>", " ", x or "")
        return re.sub(r"\s+", " ", unescape(x)).strip()

    out = []
    blocks = re.split(r'(?=<div data-question-id="\d+" class="reviewsPager")', html)
    for block in blocks[1:]:
        qid = re.search(r'data-question-id="(\d+)"', block).group(1)
        question = clean((re.search(r'<h2 class="section-subtitle">(.*?)</h2>', block, re.S) or [None, ""])[1])
        cat_m = re.search(r'</h2>\s*(?:<p[^>]*>\s*)?<a[^>]*>\s*([A-Za-z &]+?) Reviews\s*</a>', block)
        category = cat_m.group(1).strip() if cat_m else "Overall"
        tone = "Constructive" if qid in CMP_CONSTRUCTIVE else "Positive"
        for rv in re.findall(r'<div itemprop="review".*?</cite>', block, re.S):
            text = clean((re.search(r'<p itemprop="reviewBody"[^>]*>(.*?)</p>', rv, re.S) or [None, ""])[1])
            date = (re.search(r'itemprop="datePublished" content="([\d-]+)"', rv) or [None, ""])[1]
            dept = clean((re.search(r"<b>(.*?)</b>", rv, re.S) or [None, ""])[1])
            if not text or not date:
                continue
            out.append({"platform": platform, "author": dept or "Anonymous",
                        "title": f"[{category} · {tone}] {question}"[:300],
                        "rating": "", "date": date[:10], "text": text[:5000]})
    return out


def comparably_summary(html):
    m = re.search(r"Out of ([\d,]+) [^<]*?employee reviews, (\d+)% were positive", html)
    if not m:
        return None
    part = re.search(r'companyParticipantsCount"\]=(\d+)', html)
    return {"rating": None, "total_ratings": None,
            "total_reviews": int(m.group(1).replace(",", "")),
            "positive_pct": int(m.group(2)),
            "participants": int(part.group(1)) if part else None}


def json_blobs_from_html(html):
    blobs = next_flight_blobs(html)
    for body in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I):
        body = body.strip()
        if not body or len(body) > 30_000_000:
            continue
        try:
            blobs.append(json.loads(body))
            continue
        except ValueError:
            pass
        dec = json.JSONDecoder()
        for m in re.finditer(r'JSON\.parse\(("(?:[^"\\]|\\.)*")\)', body):
            try:
                blobs.append(json.loads(json.loads(m.group(1))))
            except ValueError:
                pass
        for m in re.finditer(r"=\s*(?=[\{\[])", body):
            try:
                obj, _ = dec.raw_decode(body, m.end())
                if isinstance(obj, (dict, list)) and len(json.dumps(obj)) > 200:
                    blobs.append(obj)
            except ValueError:
                pass
    return blobs


def extract_reviews(html, extra_json_strings, platform):
    blobs = json_blobs_from_html(html)
    for s in extra_json_strings or []:
        try:
            blobs.append(json.loads(s))
        except ValueError:
            pass
    ex = Extractor()
    for b in blobs:
        ex.collect_refs(b)
    out, seen = [], set()
    found = ([r for b in blobs for r in ex.walk(b, platform)] + html_card_reviews(html, platform)
             + comparably_reviews(html, platform))
    for r in found:
        k = review_key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


SUM_RATING_KEYS = ("overallRating", "ratingValue", "averageRating", "rating", "avgRating")
SUM_COUNT_KEYS = ("reviewCount", "totalReviewCount", "numberOfReviews", "totalReviews",
                  "ratingCount", "reviewsCount")


def extract_summary(html, extra_json_strings):
    """Site ki overall rating aur TOTAL reviews ki ginti (jaise Glassdoor 4.7 · 312)."""
    cmp = comparably_summary(html)
    if cmp:
        return cmp
    blobs = json_blobs_from_html(html)
    for s in extra_json_strings or []:
        try:
            blobs.append(json.loads(s))
        except ValueError:
            pass
    pairs, stack, seen = [], list(blobs), 0
    while stack and seen < 2_000_000:
        x = stack.pop()
        seen += 1
        if isinstance(x, dict):
            r = next((x[k] for k in SUM_RATING_KEYS if k in x and isinstance(x[k], (int, float, str))
                      and not isinstance(x[k], bool)), None)
            c = next((x[k] for k in SUM_COUNT_KEYS if k in x and isinstance(x[k], (int, float, str))
                      and not isinstance(x[k], bool)), None)
            try:
                r, c = round(float(r), 2), int(str(c).replace(",", ""))
                if 0 < r <= 5 and c > 0:
                    pairs.append((r, c))
            except (TypeError, ValueError):
                pass
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    if not pairs:
        return None
    # Page par doosri companies ke ratings bhi ho sakte hain; sabse zyaada baar aane wala pair hi apna hai
    (r, c), _ = Counter(pairs).most_common(1)[0]
    return {"rating": r, "total_ratings": None, "total_reviews": c}


def review_key(r):
    raw = "|".join(str(r.get(f, "")).strip().lower()[:80] for f in ("platform", "date", "title", "text"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------- BROWSER ----------------
GLOBALS_JS = """() => {
  const out = [];
  for (const k of Object.keys(window)) {
    if (!/^(__|_initial|initial|apollo|APOLLO|INITIAL)/i.test(k)) continue;
    try {
      const v = window[k];
      if (v && typeof v === "object") {
        const s = JSON.stringify(v);
        if (s && s.length > 200 && s.length < 30000000) out.push(s);
      }
    } catch (e) {}
  }
  return out;
}"""

ZERO_HINTS = ("could be the first review", "be the first to review", "no reviews yet",
              "be the first to write a review")

BLOCK_HINTS = ("just a moment", "verify you are human", "access denied", "captcha",
               "are you a robot", "security check", "attention required")


def launch(p):
    opts = dict(user_data_dir=PROFILE_DIR, headless=False, viewport=None,
                args=["--disable-blink-features=AutomationControlled"])
    try:
        return p.chromium.launch_persistent_context(channel="chrome", **opts)
    except Exception:
        print("  (Chrome nahi mila, Playwright ka Chromium use kar raha hoon)")
        return p.chromium.launch_persistent_context(**opts)


def safe_goto(page, url):
    """Page kholta hai; agar beech mein koi redirect/navigation takraye to 3 baar retry."""
    last = None
    for _ in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            return
        except Exception as e:
            last = e
            msg = str(e)
            if "interrupted by another navigation" in msg or "ERR_ABORTED" in msg:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                page.wait_for_timeout(4000)
                continue
            raise
    raise last


def comparably_backfill(page, max_pages):
    """Comparably ke har group ke agle pages (Next button) khol kar unka HTML laata hai."""
    pages = []
    qids = page.eval_on_selector_all("div.reviewsPager[data-question-id]",
                                     "els => els.map(e => e.dataset.questionId)")
    for q in qids:
        sel = f'div.reviewsPager[data-question-id="{q}"]'
        for n in range(2, max_pages + 1):
            nxt = page.query_selector(f"{sel} a.pager_next")
            if not nxt or "disabled" in (nxt.get_attribute("class") or ""):
                break
            try:
                before = page.inner_text(f"{sel} .reviewsList")
                nxt.click()
                page.wait_for_function(
                    "([s, b]) => { const e = document.querySelector(s + ' .reviewsList');"
                    " return e && e.innerText !== b; }", arg=[sel, before], timeout=20000)
            except Exception:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass
            page.wait_for_timeout(random.randint(1500, 3500))
            pages.append(page.content())
            print(f"       backfill: group {q}, page {n}")
    return pages


def human_pause(page):
    page.wait_for_timeout(random.randint(4000, 7000))
    for _ in range(random.randint(3, 6)):
        page.mouse.wheel(0, random.randint(400, 900))
        page.wait_for_timeout(random.randint(600, 1400))
    page.wait_for_timeout(random.randint(1500, 3000))


def scrape_all(sources, setup=False, backfill=0):
    from playwright.sync_api import sync_playwright

    results, statuses = [], {}
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = launch(p)

        if setup:
            for s in sources:
                tab = ctx.new_page()
                try:
                    safe_goto(tab, s["url"])
                except Exception as e:
                    print(f"  ! {s['platform']} page nahi khula: {e}")
                input(f"\n>> {s['platform']}: browser mein login / captcha poora karein. "
                      f"Jab reviews dikhne lagein aur page load ho chuka ho, tab yahan Enter dabayein...")
                tab.close()

        for s in sources:
            name = s["platform"]
            page = ctx.new_page()
            try:
                safe_goto(page, s["url"])
                human_pause(page)
                html = page.content()
                if any(h in ((page.title() or "") + html[:5000]).lower() for h in BLOCK_HINTS):
                    print(f"  ..   {name}: bot-check aaya, 20 sec ruk kar dobara try")
                    page.wait_for_timeout(20000)
                    safe_goto(page, s["url"])
                    human_pause(page)
                    html = page.content()
                try:
                    extra = page.evaluate(GLOBALS_JS)
                except Exception:
                    extra = []
                snap = os.path.join(SNAPSHOT_DIR, f"{name}.html")
                with open(snap, "w", encoding="utf-8") as f:
                    f.write(html)

                reviews = extract_reviews(html, extra, name)
                if backfill and name == "Comparably" and reviews:
                    for more_html in comparably_backfill(page, backfill):
                        reviews += comparably_reviews(more_html, name)
                page_text = re.sub(r"<[^>]+>", " ", html).lower()
                summary = extract_summary(html, extra)
                if not reviews and any(h in page_text for h in ZERO_HINTS):
                    statuses[name] = {"ok": True, "fetched": 0,
                                      "summary": {"rating": None, "total_ratings": None, "total_reviews": 0}}
                    print(f"  OK   {name}: is page par abhi koi public review nahi hai")
                    continue
                if not reviews:
                    title = (page.title() or "").strip()
                    blocked = any(h in (title + html[:5000]).lower() for h in BLOCK_HINTS)
                    why = "Block/captcha page aaya" if blocked else "Page khula par reviews nahi pehchaane gaye"
                    raise RuntimeError(f"{why} (page title: '{title[:80]}'). Snapshot: {snap}")
                results.extend(reviews)
                statuses[name] = {"ok": True, "fetched": len(reviews), "summary": summary}
                total = ""
                if summary:
                    total = f" (site par total: {summary['total_reviews']}"
                    total += f", {summary['positive_pct']}% positive)" if summary.get("positive_pct") else f", rating {summary['rating']})"
                print(f"  OK   {name}: {len(reviews)} latest reviews mile{total}")
            except Exception as e:
                statuses[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
                print(f"  FAIL {name}: {str(e).splitlines()[0][:200]}")
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            time.sleep(random.uniform(5, 12))
        ctx.close()
    return results, statuses


# ---------------- SAVE + PUSH ----------------
def merge_csv(new_reviews):
    rows = {}
    if os.path.exists(OUT_CSV):
        with open(OUT_CSV, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows[review_key(r)] = r
    added = 0
    for r in new_reviews:
        k = review_key(r)
        if k not in rows:
            added += 1
        rows[k] = r
    ordered = sorted(rows.values(), key=lambda r: r.get("date", ""), reverse=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)
    return added


def save_status(statuses):
    old = {}
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                old = json.load(f)
        except ValueError:
            old = {}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    for name, st in statuses.items():
        prev = old.get(name, {})
        if st["ok"]:
            prev.update({"ok": True, "error": "", "last_success": now, "fetched": st["fetched"]})
            if st.get("summary"):
                prev["summary"] = st["summary"]
        else:
            prev.update({"ok": False, "error": st["error"], "last_attempt": now})
        old[name] = prev
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(old, f, indent=1, ensure_ascii=False)


def git_push():
    if not os.path.isdir(".git"):
        print("  (Yeh folder git repo nahi hai, push skip)")
        return

    def git(*a):
        r = subprocess.run(["git", *a], capture_output=True, text=True)
        if r.returncode != 0 and a[0] not in ("diff",):
            print(f"  git {a[0]}: {(r.stderr or r.stdout).strip()[:300]}")
        return r

    git("add", OUT_CSV, STATUS_FILE)
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("  Koi naya change nahi")
        return
    git("commit", "-m", "Web reviews sync (laptop)")
    git("pull", "--rebase", "--autostash")
    if git("push").returncode == 0:
        print("  GitHub par push ho gaya, dashboard 1-2 minute mein update hoga")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="pehli baar login/captcha ke liye")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--backfill", type=int, default=0, metavar="PAGES",
                    help="Comparably ke har group ke itne pages tak purane reviews laao (ek baar ke liye)")
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sources = [s for s in WEB_SOURCES if s.get("url", "").strip()]
    if not sources:
        print("WEB_SOURCES mein koi URL nahi hai. web_scraper_local.py kholkar URLs bharein.")
        return

    print(f"=== Web review sync: {dt.datetime.now():%Y-%m-%d %H:%M} ===")
    reviews, statuses = scrape_all(sources, setup=args.setup, backfill=args.backfill)
    added = merge_csv(reviews)
    save_status(statuses)
    print(f"  {added} naye reviews {OUT_CSV} mein jude")
    if AUTO_GIT_PUSH and not args.no_push:
        git_push()
    print("=== Done ===")


if __name__ == "__main__":
    main()
