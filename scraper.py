#!/usr/bin/env python3
"""
Builds snow.json for the ESP32 powder-day display.

For each resort it tries, in order:
  1. The resort's own report
  2. Open-Meteo model estimate of the last 24h of snowfall (free, no API key)

pip install requests beautifulsoup4 playwright
playwright install --with-deps chromium
"""
import json
import os
import re
import subprocess
import sys
import concurrent.futures
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
NOTES = {}  # resort name -> why the resort scrape failed (written into snow.json)


# ---------------------------------------------------------------------------
# Resort list. Order = order the button cycles through.
# ---------------------------------------------------------------------------
def todo(_):
    return None, None


def rcr(name, lat, lon, url):
    return {
        "name": name, "lat": lat, "lon": lon, "url": url,
        "render_wait": ".rcr-element-box-quantity-snowreport",
        "extract": lambda r: (extract_rcr_snow(r, "LAST 24 HOURS"),
                            extract_rcr_snow(r, "LAST 7 DAYS")),
    }


RESORTS = [
    {"name": "Lake Louise",    "lat": 51.4536, "lon": -116.1388, "url": None, "extract": todo},
    {"name": "Sunshine",       "lat": 51.0786, "lon": -115.7720,
     "url": "https://www.skibanff.com/the-mountain/mountain-report/current-conditions",
     "extract": lambda r: (extract_after_label(r, "24hr"),
                           extract_after_label(r, "5 Day"))},
    {"name": "Norquay",        "lat": 51.2018, "lon": -115.5940,
     "url": "https://banffnorquay.com/conditions/",
     "extract": lambda r: (extract_before_label(r, "Last 24 Hours"),
                           extract_before_label(r, "Last 7 Days"))},
    rcr("Nakiska", 50.9400, -115.1500, "https://skinakiska.com/conditions/snow-report/"),
    {"name": "Castle",         "lat": 49.3167, "lon": -114.4167,
     "url": "https://www.skicastle.ca/snowreport/",
     "extract": lambda r: (extract_table_value(r, "Fresh Snow (24 hrs)"),
                           extract_table_value(r, "Last 7 days"))},
    rcr("Fernie", 49.4636, -115.0883, "https://skifernie.com/conditions/snow-report/"),
    rcr("Kicking Horse", 51.2985, -117.0486, "https://kickinghorseresort.com/conditions/snow-report/"),
    rcr("Kimberley", 49.6800, -116.0000, "https://skikimberley.com/conditions/snow-report/"),
    {"name": "Panorama",       "lat": 50.4600, "lon": -116.2400,
     "url": "https://www.panoramaresort.com/panorama-today/daily-snow-report",
     "render_wait_js": r"""() => [...document.querySelectorAll('.summary-facilities__stat')]
         .some(b => /24\s*hours/i.test(b.textContent) &&
                    /\d/.test((b.querySelector('h4') || {}).textContent || ''))""",
     "extract": lambda r: (extract_stat_box(r, "24 Hours"),
                           extract_stat_box(r, "7 Days"))},
]


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------
def extract_after_label(resp, label, tag="h4", occurrence=0):
    soup = BeautifulSoup(resp.text, "html.parser")
    heads = [h for h in soup.find_all(tag)
             if h.get_text(strip=True).lower() == label.lower()]
    if len(heads) <= occurrence:
        return None
    head = heads[occurrence]
    for s in head.find_all_next(string=True):
        if s.parent is head:
            continue
        m = re.match(r"\s*(\d+(?:\.\d+)?)", s)
        if m:
            return float(m.group(1))
    return None


def extract_before_label(resp, label, tag="p", occurrence=0):
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


# ---------------------------------------------------------------------------
# Fallback Model: Open-Meteo
# ---------------------------------------------------------------------------
def open_meteo_cm(lat, lon):
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "snowfall",
            "past_days": 7,
            "forecast_days": 1,
            "timezone": "America/Edmonton",
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    vals = r.json()["hourly"]["snowfall"]
    now_idx = 7 * 24 + datetime.now(TZ).hour

    def total(hours):
        window = vals[max(0, now_idx - hours + 1): now_idx + 1]
        return round(sum(v for v in window if v is not None), 1)

    return total(24), total(7 * 24)


# ---------------------------------------------------------------------------
# Headless browser runner via ThreadPoolExecutor (bypasses asyncio loop conflicts)
# ---------------------------------------------------------------------------
def _fetch_rendered_worker(url, wait_selector, wait_js, timeout_ms):
    from playwright.sync_api import sync_playwright
    try:
        p = sync_playwright().start()
        browser = p.chromium.launch()
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e).lower():
            print("Playwright binaries missing. Installing chromium...", file=sys.stderr)
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            p = sync_playwright().start()
            browser = p.chromium.launch()
        else:
            raise e

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
        return page.content()
    finally:
        browser.close()
        p.stop()


def fetch_rendered(url, wait_selector=None, wait_js=None, timeout_ms=45000):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_fetch_rendered_worker, url, wait_selector, wait_js, timeout_ms)
        html_content = future.result()
        return SimpleNamespace(text=html_content)


def get_resort(resort):
    cm24 = week = None

    if resort["url"]:
        try:
            if resort.get("render_wait") or resort.get("render_wait_js"):
                resp = fetch_rendered(resort["url"], resort.get("render_wait", None),
                                    resort.get("render_wait_js", None))
            else:
                resp = requests.get(resort["url"], headers=HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
            cm24, week = resort["extract"](resp)
            print(f"[{resort['name']}] fetched {len(resp.text)} chars, "
                  f"extracted 24h={cm24} week={week}", file=sys.stderr)
            if cm24 is None or week is None:
                NOTES[resort["name"]] = (f"page loaded ({len(resp.text)} chars) but extractor "
                                         f"found 24h={cm24} week={week}")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}".replace("\n", " ")[:300]
            NOTES[resort["name"]] = msg
            print(f"[{resort['name']}] resort scrape failed: {msg}", file=sys.stderr)

    if cm24 is not None and week is not None:
        return cm24, week, "resort"

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
        entry = {"name": r["name"], "cm": cm24, "week": week, "src": src}
        if NOTES.get(r["name"]):
            entry["note"] = NOTES[r["name"]]
        out["resorts"].append(entry)
        print(f"{r['name']:<14} 24h={cm24} cm  week={week} cm  ({src})")
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:])
