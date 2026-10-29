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

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("America/Edmonton")
HEADERS = {"User-Agent": "Mozilla/5.0 (personal powder-day display)"}
TIMEOUT = 20


# ---------------------------------------------------------------------------
# Resort list. Order = order the button cycles through.
# lat/lon are approximate base-to-mid-mountain points for the model fallback.
# url / extract are YOURS to fill in after inspecting each page (see README
# notes in the chat: DevTools -> Network -> Fetch/XHR, look for a JSON call).
# ---------------------------------------------------------------------------
def todo(_):
    return None  # replace with a real extractor per resort


RESORTS = [
    {"name": "Lake Louise",   "lat": 51.4536, "lon": -116.1388, "url": None, "extract": todo},
    # NOTE: this page has no 24h figure, only "5 Day". Use it to sanity-check the
    # model estimate, or swap the label to whichever number you want on the LEDs.
    {"name": "Sunshine",      "lat": 51.0786, "lon": -115.7720,
     "url": "https://www.skibanff.com/the-mountain/mountain-report/current-conditions",
     "extract": lambda r: extract_after_label(r, "5 Day")},
    {"name": "Norquay",       "lat": 51.2018, "lon": -115.5940, "url": None, "extract": todo},
    {"name": "Nakiska",       "lat": 50.9400, "lon": -115.1500, "url": None, "extract": todo},
    {"name": "Castle",        "lat": 49.3167, "lon": -114.4167, "url": None, "extract": todo},
    {"name": "Fernie",        "lat": 49.4636, "lon": -115.0883, "url": None, "extract": todo},
    {"name": "Kicking Horse", "lat": 51.2985, "lon": -117.0486, "url": None, "extract": todo},
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


def extract_from_html(resp, css, pattern=r"(\d+(?:\.\d+)?)"):
    """Grab text from a CSS selector and pull the first number out of it."""
    soup = BeautifulSoup(resp.text, "html.parser")
    node = soup.select_one(css)
    if not node:
        return None
    m = re.search(pattern, node.get_text(" ", strip=True))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Fallback: model-estimated snowfall over the last 24h
# ---------------------------------------------------------------------------
def open_meteo_24h_cm(lat, lon):
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "snowfall",  # cm
            "past_days": 1,
            "forecast_days": 1,
            "timezone": "America/Edmonton",
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    vals = r.json()["hourly"]["snowfall"]  # starts yesterday 00:00 local
    now_idx = 24 + datetime.now(TZ).hour
    window = vals[max(0, now_idx - 23): now_idx + 1]
    return round(sum(v for v in window if v is not None), 1)


# ---------------------------------------------------------------------------
def get_resort(resort):
    # 1) resort's own report
    if resort["url"]:
        try:
            resp = requests.get(resort["url"], headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            cm = resort["extract"](resp)
            if cm is not None:
                return cm, "resort"
        except Exception as e:
            print(f"[{resort['name']}] resort scrape failed: {e}", file=sys.stderr)

    # 2) model fallback
    try:
        return open_meteo_24h_cm(resort["lat"], resort["lon"]), "model"
    except Exception as e:
        print(f"[{resort['name']}] model fallback failed: {e}", file=sys.stderr)
        return None, "none"


def main(out_path="snow.json"):
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "resorts": []}
    for r in RESORTS:
        cm, src = get_resort(r)
        out["resorts"].append({"name": r["name"], "cm": cm, "src": src})
        print(f"{r['name']:<14} {cm} cm ({src})")
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:])
