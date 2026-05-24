"""
Google Maps Lead Scraper — mobile-first Flask app.

Runs scraping jobs in a background thread so the HTTP request returns
immediately and the phone UI can poll for progress. Designed for Render /
Railway / Fly.io free tiers (NOT Netlify — that has 10s timeouts).
"""

import csv
import io
import json
import os
import random
import re
import threading
import time
import uuid
from collections import OrderedDict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, send_file
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# In-memory job store. For a real deployment behind multiple workers you'd
# swap this for Redis, but it's fine for a single-instance free-tier host.
JOBS = {}
JOBS_LOCK = threading.Lock()

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)

# Common pages that tend to contain contact info.
# Ordered by hit rate — homepage + /contact catch ~80% of emails.
# Trimmed from 10 to 4 to keep per-business time under ~5 seconds.
CONTACT_PATHS = [
    "/contact", "/contact-us", "/about", "/about-us",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------- Job helpers ----------

def make_job(query):
    job_id = uuid.uuid4().hex[:8]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "query": query,
            "status": "queued",
            "stage": "Waiting to start...",
            "scraped": 0,
            "total": 0,
            "results": [],
            "error": None,
            "started_at": time.time(),
            "paused": False,
            "stopped": False,
        }
    return job_id


def update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


# ---------- Email + executive harvesting from a website ----------

def fetch(url, timeout=6):
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            allow_redirects=True,
        )
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except Exception:
        return None
    return None


def extract_emails_from_html(html):
    if not html:
        return []
    # Catch mailto: links and plain-text emails. Filter out image filenames.
    found = set()
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            email = a["href"].split(":", 1)[1].split("?")[0].strip()
            if EMAIL_REGEX.fullmatch(email):
                found.add(email.lower())
    for match in EMAIL_REGEX.findall(html):
        m = match.lower()
        if not m.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            found.add(m)
    # Strip obvious junk (e.g., sentry, wixpress, example.com)
    junk = ("sentry.io", "wixpress.com", "example.com", "domain.com",
            "yourdomain", "email.com", "test.com")
    return [e for e in found if not any(j in e for j in junk)]


def harvest_website(website_url, log):
    """Visit a business website + common contact paths, return emails."""
    if not website_url:
        return []
    if not website_url.startswith("http"):
        website_url = "http://" + website_url

    parsed = urlparse(website_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    visited = set()
    emails = set()

    pages_to_try = [website_url] + [urljoin(base, p) for p in CONTACT_PATHS]
    for url in pages_to_try:
        if url in visited:
            continue
        visited.add(url)
        log(f"  fetching {url}")
        html = fetch(url)
        if html:
            for e in extract_emails_from_html(html):
                emails.add(e)
        # Bail out early once we have a couple — diminishing returns
        if len(emails) >= 2:
            break
        time.sleep(random.uniform(0.2, 0.5))

    return sorted(emails)


# ---------- Google Maps scraping with Playwright ----------

def scrape_maps(query, job_id, max_results=None):
    """Wrapper that catches any unhandled error and writes it to the job
    so the frontend can display it instead of getting a silent failure."""
    try:
        _scrape_maps_inner(query, job_id, max_results)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[{job_id}] FATAL: {tb}")
        update_job(
            job_id,
            status="error",
            stage="Scraper crashed",
            error=f"{type(e).__name__}: {e}",
        )


def _scrape_maps_inner(query, job_id, max_results=None):
    """Drive Google Maps, paginate by scrolling the results panel,
    open each result, extract fields."""

    def log(msg):
        update_job(job_id, stage=msg)
        print(f"[{job_id}] {msg}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        # Block images / fonts / media to save memory on Render free tier.
        # Google Maps still renders + functions without them.
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media")
            else route.continue_(),
        )
        page = context.new_page()

        log("Opening Google Maps…")
        page.goto(
            f"https://www.google.com/maps/search/{query.replace(' ', '+')}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        # Accept consent if it appears (EU traffic)
        try:
            page.locator('button:has-text("Accept all")').first.click(timeout=3000)
        except Exception:
            pass

        # The results panel uses role="feed"
        feed_selector = 'div[role="feed"]'
        try:
            page.wait_for_selector(feed_selector, timeout=15_000)
        except Exception:
            update_job(
                job_id,
                status="error",
                error="Couldn't find results feed — Google may have served a different layout.",
            )
            browser.close()
            return

        # --- PAGINATION: scroll the feed until "You've reached the end" ---
        log("Scrolling through all results…")
        last_count = 0
        stagnant = 0
        while True:
            cards = page.query_selector_all(f'{feed_selector} > div > div > a')
            count = len(cards)
            update_job(job_id, total=count, stage=f"Found {count} results, scrolling…")

            # End-of-list sentinel
            end_text = page.locator('text=/You.{0,3}ve reached the end/i').count()
            if end_text > 0:
                break

            if count == last_count:
                stagnant += 1
                if stagnant >= 4:  # nothing new after several scrolls
                    break
            else:
                stagnant = 0
            last_count = count

            page.evaluate(
                f"document.querySelector('{feed_selector}').scrollBy(0, 2000)"
            )
            time.sleep(random.uniform(1.5, 2.5))

            if max_results and count >= max_results:
                break

        # Collect URLs first, then visit each — more stable than reusing
        # the cards array (DOM gets re-rendered)
        anchors = page.query_selector_all(f'{feed_selector} > div > div > a')
        urls = []
        for a in anchors:
            href = a.get_attribute("href")
            if href and "/maps/place/" in href:
                urls.append(href)
        urls = list(OrderedDict.fromkeys(urls))  # dedupe, keep order
        if max_results:
            urls = urls[:max_results]

        log(f"Visiting {len(urls)} business listings…")
        update_job(job_id, total=len(urls))

        results = []
        skipped = 0
        for i, url in enumerate(urls, 1):
            # --- Pause / stop check (between businesses, the natural breakpoint) ---
            current = get_job(job_id) or {}
            if current.get("stopped"):
                log("Stopped by user.")
                break
            if current.get("paused"):
                update_job(job_id, status="paused", stage="Paused — tap Resume to continue")
                # Block here until user resumes or stops
                while True:
                    time.sleep(1)
                    j = get_job(job_id) or {}
                    if j.get("stopped"):
                        break
                    if not j.get("paused"):
                        update_job(job_id, status="running")
                        log(f"Resuming…")
                        break
                if (get_job(job_id) or {}).get("stopped"):
                    log("Stopped by user.")
                    break

            log(f"[{i}/{len(urls)}] Opening listing…")
            try:
                # 20s is enough — if a listing takes longer than that, it's
                # almost always Google soft-blocking us; skip and move on.
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                try:
                    page.wait_for_selector('div[role="main"]', timeout=6000)
                except Exception:
                    pass
                time.sleep(random.uniform(1.2, 2.0))
                data = extract_place_details(page)

                # Visit the website to find emails
                if data.get("website"):
                    log(f"[{i}/{len(urls)}] Looking for emails on {data['website']}")
                    data["emails"] = harvest_website(data["website"], log)
                else:
                    data["emails"] = []

                results.append(data)
                update_job(job_id, scraped=i, results=results.copy())
            except Exception as e:
                skipped += 1
                # Bump scraped counter so progress bar keeps moving even on skips
                update_job(
                    job_id,
                    scraped=i,
                    stage=f"Skipped listing {i} (took too long) — {skipped} skipped so far",
                )
                print(f"  skip {i}: {type(e).__name__}: {str(e)[:120]}")
                continue

        browser.close()

        final_stage = (
            f"Stopped — {len(results)} businesses scraped."
            if (get_job(job_id) or {}).get("stopped")
            else f"Finished — {len(results)} businesses scraped."
        )
        update_job(
            job_id,
            status="done",
            stage=final_stage,
            results=results,
        )


def extract_place_details(page):
    """Pull fields from an open Google Maps place page."""
    out = {
        "name": "",
        "address": "",
        "phone": "",
        "website": "",
        "rating": "",
        "reviews": "",
        "category": "",
        "maps_url": page.url,
    }

    # ---- Name extraction with multiple fallbacks ----
    # Google A/B tests Maps layouts, especially across regions and IPs.
    # We try in order: wait for h1 → other selectors → page title → URL.
    name = ""

    # Strategy 1: wait briefly for the h1 to actually render, then read it.
    try:
        page.wait_for_selector("h1", timeout=6000, state="visible")
        candidate = page.locator("h1").first.inner_text(timeout=2000).strip()
        if candidate and candidate.lower() not in ("results", "google maps"):
            name = candidate
    except Exception:
        pass

    # Strategy 2: other selectors Google has used historically.
    if not name:
        for selector in [
            'div[role="main"] h1',
            'h1.DUwDvf',
            'h1.fontHeadlineLarge',
            'div.fontHeadlineLarge',
            'div[aria-label][role="main"]',  # the main panel often has aria-label = business name
        ]:
            try:
                el = page.locator(selector).first
                if selector.endswith('[role="main"]'):
                    candidate = (el.get_attribute("aria-label", timeout=1500) or "").strip()
                else:
                    candidate = el.inner_text(timeout=1500).strip()
                if candidate and 2 < len(candidate) < 200 and candidate.lower() not in ("results", "google maps"):
                    name = candidate
                    break
            except Exception:
                continue

    # Strategy 3: parse the URL — Google Maps puts the business name in the path.
    # Example: /maps/place/Joe's+Pizza+%26+Co/@40.7,-74,17z/...
    if not name:
        try:
            from urllib.parse import unquote
            url_part = page.url.split("/maps/place/")[-1].split("/@")[0].split("/data=")[0]
            candidate = unquote(url_part).replace("+", " ").strip()
            if candidate and 2 < len(candidate) < 200:
                name = candidate
        except Exception:
            pass

    # Strategy 4: window/document title, stripping " - Google Maps" suffix.
    if not name:
        try:
            title = page.title()
            if title and "Google Maps" not in title.strip():
                name = title.split(" - ")[0].strip()
            elif title:
                # Title like "Joe's Pizza - Google Maps"
                cleaned = title.replace("- Google Maps", "").strip(" -")
                if cleaned and cleaned.lower() != "google maps":
                    name = cleaned
        except Exception:
            pass

    out["name"] = name

    # Rating + reviews count (e.g. "4.6  (123)")
    try:
        rating_el = page.locator('div[role="img"][aria-label*="stars"]').first
        label = rating_el.get_attribute("aria-label") or ""
        m = re.search(r"([\d.]+)\s*stars?", label)
        if m:
            out["rating"] = m.group(1)
    except Exception:
        pass
    try:
        reviews_text = page.locator('button[aria-label*="reviews" i]').first.inner_text(
            timeout=2000
        )
        m = re.search(r"([\d,]+)", reviews_text)
        if m:
            out["reviews"] = m.group(1).replace(",", "")
    except Exception:
        pass

    # Category — appears as a button under the name
    try:
        out["category"] = page.locator('button[jsaction*="category"]').first.inner_text(
            timeout=2000
        ).strip()
    except Exception:
        pass

    # The info rows use aria-labels that start with the field name
    def field_by_aria(prefix):
        try:
            el = page.locator(f'button[aria-label^="{prefix}"], a[aria-label^="{prefix}"]').first
            label = el.get_attribute("aria-label", timeout=2000) or ""
            return label.split(":", 1)[-1].strip() if ":" in label else label.replace(prefix, "").strip()
        except Exception:
            return ""

    out["address"] = field_by_aria("Address")
    out["phone"] = field_by_aria("Phone")

    # Website — anchor with aria-label "Website"
    try:
        href = page.locator('a[aria-label^="Website"]').first.get_attribute(
            "href", timeout=2000
        )
        if href and href.startswith("http"):
            out["website"] = href
    except Exception:
        pass

    return out


# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    try:
        data = request.get_json(silent=True) or {}
        query = (data.get("query") or "").strip()
        max_results = data.get("max_results")
        try:
            max_results = int(max_results) if max_results else None
        except (TypeError, ValueError):
            max_results = None

        if not query:
            return jsonify({"error": "Query is required"}), 400

        # Prevent concurrent scrapes — free tier RAM only fits one Chromium.
        # Starting a second one while another is alive guarantees a crash.
        with JOBS_LOCK:
            running = [
                j for j in JOBS.values()
                if j.get("status") in ("queued", "running", "paused")
            ]
        if running:
            existing = running[0]
            return jsonify({
                "error": "Another scrape is already running. Wait for it to finish or stop it first.",
                "running_job_id": existing["id"],
            }), 409

        job_id = make_job(query)
        t = threading.Thread(
            target=scrape_maps, args=(query, job_id, max_results), daemon=True
        )
        t.start()
        update_job(job_id, status="running")
        return jsonify({"job_id": job_id})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {type(e).__name__}: {e}"}), 500


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "Server error (500). Check the Render logs."}), 500


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found"}), 404


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/api/control/<job_id>", methods=["POST"])
def api_control(job_id):
    """Pause, resume, or stop a running job. Body: {"action": "pause|resume|stop"}"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").lower()
    if action == "pause":
        update_job(job_id, paused=True)
    elif action == "resume":
        update_job(job_id, paused=False)
    elif action == "stop":
        update_job(job_id, stopped=True, paused=False)
    else:
        return jsonify({"error": "Unknown action. Use pause/resume/stop."}), 400
    return jsonify({"ok": True, "action": action})


@app.route("/api/export/<job_id>")
def api_export(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    rows = job.get("results", [])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Name", "Address", "Phone", "Website", "Rating", "Reviews",
        "Category", "Emails", "Maps URL",
    ])
    for r in rows:
        writer.writerow([
            r.get("name", ""),
            r.get("address", ""),
            r.get("phone", ""),
            r.get("website", ""),
            r.get("rating", ""),
            r.get("reviews", ""),
            r.get("category", ""),
            "; ".join(r.get("emails", [])),
            r.get("maps_url", ""),
        ])

    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)
    safe_query = re.sub(r"[^a-z0-9]+", "-", job["query"].lower()).strip("-")
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"leads-{safe_query}-{job_id}.csv",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
