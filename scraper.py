#!/usr/bin/env python3
"""
Builds snow.json for the ESP32 powder-day display.

For each resort it tries, in order:
  1. The resort's own report (you fill in URL + extractor below)
  2. Open-Meteo model estimate of the last 24h of snowfall (free, no API key)

Run it on a schedule (GitHub Actions cron, a Pi, a cheap VPS) and host the
resulting snow.json somewhere the ESP32 can fetch over HTTPS
(GitHub Pages is the easiest).

pip install requests beautifulsoup4
"""
import json
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from types import SimpleNamespace

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("America/Edmonton")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}
TIMEOUT = 20


# ---------------------------------------------------------------------------
# Resort list. Order = order the button cycles through.
# lat/lon are approximate base-to-mid-mountain points for the model fallback.
# url / extract are YOURS to fill in after inspecting each page (see README
# notes in the chat: DevTools -> Network -> Fetch/XHR, look for a JSON call).
# ---------------------------------------------------------------------------
def todo(_):
    # Each extractor takes the HTTP response and returns (cm_24h, cm_week).
    # Either can be None; missing values are filled in from the model.
    return None, None


def rcr(name, lat, lon, url):
    """Resorts of the Canadian Rockies sites (Fernie, Kicking Horse, Nakiska,
    Kimberley) share one template whose numbers are filled in by JavaScript,
    so they are loaded in a headless browser."""
    return {
        "name": name, "lat": lat, "lon": lon, "url": url,
        "render_wait": ".rcr-element-box-quantity-snowreport",
        "extract": lambda r: (extract_rcr_snow(r, "LAST 24 HOURS"),
                              extract_rcr_snow(r, "LAST 7 DAYS")),
    }


RESORTS = [
    {"name": "Lake Louise",   "lat": 51.4536, "lon": -116.1388, "url": None, "extract": todo},
    # Labels on this page include "24hr", "5 Day", "Base", "Settled Base", "Cumulative".
    {"name": "Sunshine",      "lat": 51.0786, "lon": -115.7720,
     "url": "https://www.skibanff.com/the-mountain/mountain-report/current-conditions",
     # This page's "week" figure is a 5-day total, labelled "5 Day".
     "extract": lambda r: (extract_after_label(r, "24hr"),
                           extract_after_label(r, "5 Day"))},
    # Norquay shows "Overnight", "Last 24 Hours" and "Last 7 Days".
    {"name": "Norquay",       "lat": 51.2018, "lon": -115.5940,
     "url": "https://banffnorquay.com/conditions/",
     "extract": lambda r: (extract_before_label(r, "Last 24 Hours"),
                           extract_before_label(r, "Last 7 Days"))},
    rcr("Nakiska", 50.9400, -115.1500, "https://skinakiska.com/conditions/snow-report/"),
    # Castle's report is a table: "Fresh Snow (24 hrs)" and "Last 7 days".
    {"name": "Castle",        "lat": 49.3167, "lon": -114.4167,
     "url": "https://www.skicastle.ca/snowreport/",
     "extract": lambda r: (extract_table_value(r, "Fresh Snow (24 hrs)"),
                           extract_table_value(r, "Last 7 days"))},
    rcr("Fernie", 49.4636, -115.0883, "https://skifernie.com/conditions/snow-report/"),
    rcr("Kicking Horse", 51.2985, -117.0486, "https://kickinghorseresort.com/conditions/snow-report/"),
    rcr("Kimberley", 49.6800, -116.0000, "https://skikimberley.com/conditions/snow-report/"),
    # Panorama: numbers load via JavaScript. Wait until the "24 Hours" box has a digit
    # (the page also shows other numbers, so a plain "any text" wait would be too early).
    {"name": "Panorama",      "lat": 50.4600, "lon": -116.2400,
     "url": "https://www.panoramaresort.com/panorama-today/daily-snow-report",
     "render_wait_js": r"""() => [...document.querySelectorAll('.summary-facilities__stat')]
         .some(b => /24\s*hours/i.test(b.textContent) &&
                    /\d/.test((b.querySelector('h4') || {}).textContent || ''))""",
     "extract": lambda r: (extract_stat_box(r, "24 Hours"),
                           extract_stat_box(r, "7 Days"))},
]


# ---------------------------------------------------------------------------
# Example extractors (templates). Use whichever matches what you find.
# ---------------------------------------------------------------------------
def extract_from_json(resp, path):
    """If the page calls a JSON endpoint, walk a key path like ['report','snow24']."""
    data = resp.json()
    for key in path:
        data = data[key]
    return float(data)


def extract_after_label(resp, label, tag="h4", occurrence=0):
    """Find <tag>label</tag>, return the first number that follows it.
    Matches skibanff.com: <h4>Label</h4> ... <span class="value">10</span> cm"""
    soup = BeautifulSoup(resp.text, "html.parser")
    heads = [h for h in soup.find_all(tag)
             if h.get_text(strip=True).lower() == label.lower()]
    if len(heads) <= occurrence:
        return None
    head = heads[occurrence]
    for s in head.find_all_next(string=True):
        if s.parent is head:  # skip the label's own text (e.g. the "5" in "5 Day")
            continue
        m = re.match(r"\s*(\d+(?:\.\d+)?)", s)
        if m:
            return float(m.group(1))
    return None


def extract_before_label(resp, label, tag="p", occurrence=0):
    """Find <tag>label</tag>, return the nearest "<number>cm" text just BEFORE it.
    Matches banffnorquay.com: <h3>0cm</h3> ... <p>Last 24 Hours</p>"""
    soup = BeautifulSoup(resp.text, "html.parser")
    labels = [t for t in soup.find_all(tag)
              if t.get_text(strip=True).lower() == label.lower()]
    if len(labels) <= occurrence:
        return None
    for s in labels[occurrence].find_all_previous(string=True):
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*cm\b", s)
        if m:
            return float(m.group(1))
    return None


def extract_table_value(resp, label):
    """Find a table cell whose text is `label`, return the number in the next cell.
    Matches skicastle.ca: <tr><td>Fresh Snow (24 hrs)</td><td>0</td></tr>"""
    soup = BeautifulSoup(resp.text, "html.parser")
    for cell in soup.find_all(["td", "th"]):
        if cell.get_text(strip=True).lower() == label.lower():
            nxt = cell.find_next_sibling(["td", "th"])
            if nxt:
                m = re.search(r"(\d+(?:\.\d+)?)", nxt.get_text(" ", strip=True))
                if m:
                    return float(m.group(1))
    return None


def extract_rcr_snow(resp, label):
    """RCR resort sites (Fernie etc.): a box with a number div and a label div:
    <div id="snowReport..." class="rcr-element-box-quantity-snowreport">25 </div>
    ... <div class="rcr-snow-subheader">LAST 7 DAYS</div>
    The number is filled in by JavaScript, so use a rendered page (render_wait)."""
    soup = BeautifulSoup(resp.text, "html.parser")
    for sub in soup.find_all(class_="rcr-snow-subheader"):
        if sub.get_text(strip=True).lower() == label.lower():
            box = sub.find_parent(class_="rcr-element-box-fl16") or sub.parent
            num = box.find(class_="rcr-element-box-quantity-snowreport")
            if num:
                m = re.search(r"(\d+(?:\.\d+)?)", num.get_text(" ", strip=True))
                if m:
                    return float(m.group(1))
    return None


def extract_stat_box(resp, label):
    """Panorama: <div class="summary-facilities__stat"><h4>12<sup>cm</sup></h4>
    <p>24 Hours</p></div>. The number is filled in by JavaScript."""
    soup = BeautifulSoup(resp.text, "html.parser")
    for p in soup.find_all("p"):
        if p.get_text(strip=True).lower() != label.lower():
            continue
        box = p.find_parent(class_="summary-facilities__stat") or p.parent
        h4 = box.find("h4")
        if h4:
            m = re.search(r"(\d+(?:\.\d+)?)", h4.get_text(" ", strip=True))
            if m:
                return float(m.group(1))
    return None


def extract_from_html(resp, css, pattern=r"(\d+(?:\.\d+)?)"):
    """Grab text from a CSS selector and pull the first number out of it."""
    soup = BeautifulSoup(resp.text, "html.parser")
    node = soup.select_one(css)
    if not node:
        return None
    m = re.search(pattern, node.get_text(" ", strip=True))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Fallback: model-estimated snowfall over the last 24h and last 7 days
# ---------------------------------------------------------------------------
def open_meteo_cm(lat, lon):
    """Return (cm_last_24h, cm_last_7_days) from the Open-Meteo model."""
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "snowfall",  # cm
            "past_days": 7,
            "forecast_days": 1,
            "timezone": "America/Edmonton",
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    vals = r.json()["hourly"]["snowfall"]  # starts 7 days ago, 00:00 local
    now_idx = 7 * 24 + datetime.now(TZ).hour

    def total(hours):
        window = vals[max(0, now_idx - hours + 1): now_idx + 1]
        return round(sum(v for v in window if v is not None), 1)

    return total(24), total(7 * 24)


# ---------------------------------------------------------------------------
def fetch_rendered(url, wait_selector=None, wait_js=None, timeout_ms=45000):
    """Load a page in headless Chrome and return an object with .text (the
    rendered HTML), like a requests response.
    Waits until `wait_js` (a JS function string returning true) passes, or, if
    that isn't given, until `wait_selector` has some text.
    Needs: pip install playwright && python -m playwright install --with-deps chromium"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if wait_js:
                page.wait_for_function(wait_js, timeout=20000)
            elif wait_selector:
                page.wait_for_function(
                    "sel => { const e = document.querySelector(sel); "
                    "return e && e.textContent.trim() !== ''; }",
                    arg=wait_selector, timeout=20000)
            return SimpleNamespace(text=page.content())
        finally:
            browser.close()


def get_resort(resort):
    """Return (cm_24h, cm_week, source). source is resort, mixed, model or none."""
    cm24 = week = None

    # 1) resort's own report
    if resort["url"]:
        try:
            if resort.get("render_wait") or resort.get("render_wait_js"):
                resp = fetch_rendered(resort["url"], resort.get("render_wait"),
                                      resort.get("render_wait_js"))
            else:
                resp = requests.get(resort["url"], headers=HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
            cm24, week = resort["extract"](resp)
            print(f"[{resort['name']}] fetched {len(resp.text)} chars, "
                  f"extracted 24h={cm24} week={week}", file=sys.stderr)
        except Exception as e:
            print(f"[{resort['name']}] resort scrape failed: {e}", file=sys.stderr)

    if cm24 is not None and week is not None:
        return cm24, week, "resort"

    # 2) model fills whatever is missing
    try:
        m24, mweek = open_meteo_cm(resort["lat"], resort["lon"])
    except Exception as e:
        print(f"[{resort['name']}] model fallback failed: {e}", file=sys.stderr)
        m24 = mweek = None

    got_resort = cm24 is not None or week is not None
    got_model = (cm24 is None and m24 is not None) or (week is None and mweek is not None)
    if cm24 is None:
        cm24 = m24
    if week is None:
        week = mweek
    if got_resort and got_model:
        return cm24, week, "mixed"
    if got_resort:
        return cm24, week, "resort"
    return cm24, week, ("model" if cm24 is not None or week is not None else "none")


def main(out_path="snow.json"):
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "resorts": []}
    for r in RESORTS:
        cm24, week, src = get_resort(r)
        out["resorts"].append({"name": r["name"], "cm": cm24, "week": week, "src": src})
        print(f"{r['name']:<14} 24h={cm24} cm  week={week} cm  ({src})")
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:])
