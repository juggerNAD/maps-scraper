# Maps Lead Scout

A 9:16 mobile-first web app you open on your Android phone that scrapes Google Maps and harvests business emails. Looks/feels like a native Android app, runs entirely in your browser.

## What it scrapes per business
- Name
- Address
- Phone
- Website
- Rating + review count
- Category
- Emails (by visiting the website + common contact pages)
- Maps URL

## Tech
Python + Flask backend, Playwright (headless Chromium) to drive Google Maps, BeautifulSoup + requests to harvest emails. UI is a single HTML template with vanilla JS, styled as a 9:16 phone frame on desktop and fullscreen on mobile.

## Run locally
```bash
pip install -r requirements.txt
playwright install chromium
python app.py
# open http://localhost:5000
```

## Why not Netlify?
Netlify functions have a 10s timeout (26s paid). Scraping 25+ businesses takes minutes. This needs a host that allows long-running processes.

## Deploy to Render (free tier, recommended)
1. Push this folder to a GitHub repo.
2. Go to https://render.com → New → Web Service → connect the repo.
3. Render auto-detects `render.yaml` and `Dockerfile`. Click **Create**.
4. ~3 min later you get a URL like `https://maps-lead-scout.onrender.com`.
5. Open that URL on your Android phone. Tap browser menu → **"Add to Home screen"** to make it look exactly like a native app icon.

> The free tier spins down after 15 min of inactivity, so the first request after a nap takes ~30s to wake up.

## Deploy to Railway / Fly.io
Both work the same — they read the `Dockerfile`. No config changes needed.

## Use
1. Type a query: `dentists in Manila`, `coffee shops in Austin`, `roofers in Brooklyn`.
2. Pick a limit (25 is a sane default; "All results" can take 10+ minutes).
3. Watch the progress bar.
4. Tap **Export CSV** when done. The file downloads straight to your phone.

## Notes & limits
- Google Maps actively fights scraping. Expect occasional layout changes that break selectors — `extract_place_details()` is where you'd patch them.
- "Paginate to the last page" on Maps means scrolling the results panel until "You've reached the end" appears. There's a hard ceiling around 120 results per query on Maps itself — that's Google's limit, not the scraper's.
- Email harvesting tries homepage + `/contact`, `/about`, `/team`, etc. Some sites hide emails behind JS or contact forms — those will come back empty.
- Per your request, only emails are harvested. The code does NOT try to extract executive names (CEO/founder/etc.).
- Respect Google's ToS and the websites you scrape. Use reasonable volumes.
