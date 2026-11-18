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
import time
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
        "render_wait_js": ("() => [...document.querySelectorAll("
                           "'.rcr-element-box-quantity-snowreport')]"
                           ".some(e => /\\d/.test(e.textContent))"),
        "extract": lambda r: (
            extract_rcr_snow(r, "LAST 24 HOURS", "snowReportNewSnowFall24"),
            extract_rcr_snow(r, "LAST 7 DAYS", "snowReportNewSnowFall7days")),
        "season": lambda r: (extract_rcr_snow(r, "THIS SEASON", "snowReportSeasonSnowFall")
                             or extract_rcr_snow(r, "SEASON TOTAL")),
    }


RESORTS = [
    # Lake Louise publishes two stations. Change this to "Back Bowls" if you'd rather
    # show the higher-elevation reading.
    {"name": "Lake Louise",   "lat": 51.4536, "lon": -116.1388,
     # skilouise.com/snow-conditions/ is plain HTML with Upper Mountain and Mid Mountain readings
     # (reports.skilouise.com kept timing out from GitHub's servers).
     "url": "https://www.skilouise.com/snow-conditions/",
     # the station readings are filled in by JavaScript, so load the page in a browser
     "render_wait_js": r"""() => /mid[\s-]*mountain/i.test(document.body.innerText) &&
         /last\s*24\s*hours/i.test(document.body.innerText) && /\d\s*cm/i.test(document.body.innerText)""",
     "extract": lambda r: (first_station_value(r, ["Last 24 Hours", "24 Hours"], "Mid Mountain"),
                           first_station_value(r, ["Last 7 Days", "7 Days"], "Mid Mountain")),
     "season": lambda r: first_station_value(r, ["This Season", "Season"], "Mid Mountain")},
    # Labels on this page include "24hr", "5 Day", "Base", "Settled Base", "Cumulative".
    {"name": "Sunshine",      "lat": 51.0786, "lon": -115.7720,
     "url": "https://www.skibanff.com/the-mountain/mountain-report/current-conditions",
     # This page's "week" figure is a 5-day total, labelled "5 Day".
     "extract": lambda r: (first_after_label(r, ["24hr", "24 hr", "24 hours"]),
                           first_after_label(r, ["5 Day", "5 Days", "7 Day", "7 Days"])),
     "season": lambda r: first_after_label(r, ["Cumulative"])},
    # Norquay shows "Overnight", "Last 24 Hours" and "Last 7 Days".
    {"name": "Norquay",       "lat": 51.2018, "lon": -115.5940,
     "url": "https://banffnorquay.com/conditions/",
     "extract": lambda r: (extract_before_label(r, "Last 24 Hours"),
                           extract_before_label(r, "Last 7 Days")),
     "season": lambda r: season_near_label(r, ["Year to Date Snowfall", "Year to Date"])},
    rcr("Nakiska", 50.9400, -115.1500, "https://skinakiska.com/conditions/snow-report/"),
    # Castle's report is a table: "Fresh Snow (24 hrs)" and "Last 7 days".
    {"name": "Castle",        "lat": 49.3167, "lon": -114.4167,
     "url": "https://www.skicastle.ca/snowreport/",
     "extract": lambda r: (extract_table_value(r, "Fresh Snow (24 hrs)"),
                           extract_table_value(r, "Last 7 days")),
     "season": lambda r: extract_table_value(r, "Total Snowfall this season")},
    rcr("Fernie", 49.4636, -115.0883, "https://skifernie.com/conditions/snow-report/"),
    rcr("Kicking Horse", 51.2985, -117.0486, "https://kickinghorseresort.com/conditions/snow-report/"),
    # Revelstoke's page has H2 labels with the value on the next line.
    # (Add "web_only": True to an entry to show a resort on the web page only; it is then
    # written to "web_only" in snow.json instead of "resorts", which the ESP32s read.)
    {"name": "Revelstoke", "lat": 51.0, "lon": -118.1630,
     "url": "https://www.revelstokemountainresort.com/mountain/conditions/snow-report/",
     "extract": lambda r: (first_after_label(r, ["24 Hours", "24 hrs"], tag="h2"),
                           first_after_label(r, ["7 Days", "7 days"], tag="h2")),
     "season": lambda r: first_after_label(r, ["Season Total", "Season"], tag="h2")},
    rcr("Kimberley", 49.6800, -116.0000, "https://skikimberley.com/conditions/snow-report/"),
    # Panorama: numbers load via JavaScript. Wait until the "24 Hours" box has a digit
    # (the page also shows other numbers, so a plain "any text" wait would be too early).
    {"name": "Panorama",      "lat": 50.4600, "lon": -116.2400,
     "url": "https://www.panoramaresort.com/panorama-today/daily-snow-report",
     "render_wait_js": r"""() => [...document.querySelectorAll('.summary-facilities__stat')]
         .some(b => /24\s*hours/i.test(b.textContent) &&
                    /\d/.test((b.querySelector('h4') || {}).textContent || ''))""",
     "extract": lambda r: (extract_stat_box(r, "24 Hours"),
                           extract_stat_box(r, "7 Days")),
     "season": lambda r: extract_stat_box(r, "Season")},
]

# Order the display (and the knob) follows. Any resort not listed here goes to the end.
ORDER = ["Norquay", "Sunshine", "Lake Louise", "Castle", "Kicking Horse",
         "Panorama", "Kimberley", "Fernie", "Nakiska", "Revelstoke"]
RESORTS.sort(key=lambda r: ORDER.index(r["name"]) if r["name"] in ORDER else len(ORDER))


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


def first_after_label(resp, labels, **kw):
    """Try several label spellings (e.g. "5 Day" / "5 Days"), return the first hit."""
    for label in labels:
        v = extract_after_label(resp, label, **kw)
        if v is not None:
            return v
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


def extract_rcr_snow(resp, label, elem_id=None):
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
    if elem_id:  # backup: read the box by its element id
        num = soup.find(id=elem_id)
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


def extract_station_value(resp, label, station, stations=("Back Bowls", "Upper Mountain", "Mid Mountain")):
    """Lake Louise reports two stations side by side (Back Bowls, Mid Mountain).
    Strategy 1: a table row "<label> | v1 | v2" with the station names in a header
    row, so the column is chosen by name (never guessed).
    Strategy 2: a section headed by the station name, followed by "<label>" and a number
    (also accepts "<label>: 5 cm" in one string). Returns None rather than guess."""
    soup = BeautifulSoup(resp.text, "html.parser")
    want = label.lower()

    def num(text):
        m = re.search(r"(\d+(?:\.\d+)?)", text)
        return float(m.group(1)) if m else None

    # Strategy 1: table with named columns
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2 or cells[0].get_text(" ", strip=True).lower() != want:
            continue
        table = tr.find_parent("table")
        order = []
        for c in (table.find_all(["th", "td"]) if table else []):
            t = c.get_text(" ", strip=True).lower()
            for st in stations:
                if t == st.lower() and st not in order:
                    order.append(st)
        if station in order:
            idx = order.index(station)
            vals = [num(c.get_text(" ", strip=True)) for c in cells[1:]]
            if idx < len(vals) and vals[idx] is not None:
                return vals[idx]

    others = {st.lower() for st in stations if st != station}
    # Strategy 2 (layout-aware): read the station's block as a flat run of text pieces. If a number sits
    # just before the block's first label, the page puts numbers BEFORE labels; otherwise after.
    known = {"overnight", "last 24 hours", "24 hours", "last 48 hours", "48 hours", "last 7 days",
             "7 days", "base", "this season", "season", "season total", "settled base"}

    def clean(t):
        return " ".join(t.split()).lower()

    def isnum(t):
        return re.match(r"^\s*\d+(?:\.\d+)?(?!\d)", t) is not None and len(t) <= 24

    flat = [" ".join(e.split()) for e in soup.find_all(string=True)
            if e.strip() and e.parent.name not in ("script", "style")]

    def station_of(t):  # "Mid Mountain", "Mid-Mountain", "MID MOUNTAIN (1,800 m)" all count
        if len(t) > 60:
            return None
        for st in stations:
            if re.search(r"\b" + r"[\s-]*".join(map(re.escape, st.split())) + r"\b", t, re.I):
                return st
        return None

    marks = [(i, station_of(t)) for i, t in enumerate(flat)]
    marks = [(i, st) for i, st in marks if st]
    for n_, (mi, st) in enumerate(marks):
        if st != station:
            continue
        end = next((j for j, st2 in marks[n_ + 1:] if st2 != station), mi + 61)
        toks = flat[mi + 1:min(end, mi + 61)]
        idx = [i for i, t in enumerate(toks) if clean(t) in known]
        if not idx or want not in [clean(toks[i]) for i in idx]:
            continue
        def back(k):  # step back over bare unit pieces such as "cm" or "(3.9 in)"
            k -= 1
            while k >= 0 and re.match(r"^(cm|in|\(.*\))$", toks[k].strip().lower()):
                k -= 1
            return k

        first = idx[0]
        before = first > 0 and back(first) >= 0 and isnum(toks[back(first)])
        i = [i for i in idx if clean(toks[i]) == want][0]
        j = back(i) if before else i + 1
        if 0 <= j < len(toks) and isnum(toks[j]):
            return num(toks[j])

    # Strategy 3: a section that starts at the station name
    others = {st.lower() for st in stations if st != station}
    for head in [e for e in soup.find_all(string=True) if e.strip().lower() == station.lower()]:
        seen = False
        for s_ in head.find_all_next(string=True):
            t = s_.strip()
            if not t:
                continue
            tl = t.lower()
            if tl in others:
                break                      # reached the other station: stop
            if tl == want:
                seen = True
                continue
            if tl.startswith(want):        # "24 Hours: 5 cm" in a single string
                v = num(t[len(want):])
                if v is not None:
                    return v
            if seen:
                m = re.match(r"(\d+(?:\.\d+)?)", t)
                if m:
                    return float(m.group(1))

    return None


def station_snippet(resp, station):
    """A short piece of the page around a station name, for diagnosis."""
    soup = BeautifulSoup(resp.text, "html.parser")
    flat = [" ".join(e.split()) for e in soup.find_all(string=True)
            if e.strip() and e.parent.name not in ("script", "style")]
    for i, t in enumerate(flat):
        if len(t) <= 60 and re.search(r"mid[\s-]*mountain", t, re.I):
            return "text after '" + t + "': " + " | ".join(flat[i + 1:i + 25])[:500]
    for node in soup.find_all(string=lambda t: t and t.strip().lower() == station.lower()):
        box = node.parent.parent.parent if node.parent and node.parent.parent and node.parent.parent.parent else node.parent
        return re.sub(r"\s+", " ", str(box))[:600]
    return ""


def first_station_value(resp, labels, station):
    for label in labels:
        v = extract_station_value(resp, label, station)
        if v is not None:
            return v
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
# Season total (year to date snowfall). Shown on the web page only.
# Every resort page labels it a little differently ("Season", "Season Total",
# "Cumulative", "YTD"...), so one tolerant finder serves them all. It looks for a short
# label and takes the single number that sits with it. If it finds nothing, the note in
# snow.json lists the season-like words it did see, so the labels can be added here.
# ---------------------------------------------------------------------------
SEASON_LABEL = re.compile(
    r"^(total\s+snowfall\s+this\s+season|this\s+season|year[\s-]*to[\s-]*date\s+snowfall)\s*:?$|"
    r"^(total\s+)?(season(al)?(\s+(total|to\s+date|snow(fall)?|snow\s+total))?|"
    r"year[\s-]*to[\s-]*date(\s+snow(fall)?)?|ytd(\s+snow(fall)?)?|"
    r"cumulative(\s+snow(fall)?)?|total\s+snow(fall)?)\s*(\(.*?\))?\s*:?$", re.I)
_NUM = r"\d+(?:\.\d+)?"


def _season_numbers(text):
    """Numbers in a short piece of text, ignoring years like 2026 and 2026/27."""
    out = []
    for m in re.finditer(_NUM, text):
        v = float(m.group(0))
        if "." not in m.group(0) and 1990 <= v <= 2100:
            continue
        out.append(v)
    return out


def extract_season(resp):
    soup = BeautifulSoup(resp.text, "html.parser")
    for node in soup.find_all(string=True):
        label = " ".join(node.split())
        m_inline = re.match(r"^(?:season(?:\s+total|\s+snow(?:fall)?)?|year[\s-]*to[\s-]*date|ytd|cumulative|total\s+snow(?:fall)?)"
                            r"\s*:?\s*(" + _NUM + r")\s*cm\b", label, re.I)
        if m_inline:  # "Year to date: 41 cm" in one piece of text
            return float(m_inline.group(1))
        if not label or len(label) > 40 or not SEASON_LABEL.match(label):
            continue
        # a) a container that holds just the label and one number, e.g. "85 cm Season"
        box = node.parent
        for _ in range(3):
            if box is None:
                break
            text = " ".join(box.get_text(" ", strip=True).split())
            if len(text) > 60:
                break
            nums = _season_numbers(text.replace(label, " "))
            if len(nums) == 1:
                return nums[0]
            box = box.parent
        # b) the next piece of text after the label, then the one before it
        for gen in (node.find_all_next(string=True, limit=4), node.find_all_previous(string=True, limit=4)):
            for t in gen:
                t = t.strip()
                if not t:
                    continue
                nums = _season_numbers(t)
                if nums and len(t) <= 20:
                    return nums[0]
    return None


def season_near_label(resp, labels, tags=("p", "h3", "h4", "h5", "span", "div", "strong", "b")):
    """Find the number that sits next to a labelled element, whichever tag the page uses and
    whether the number comes before or after the label (e.g. "<h3>85cm</h3><p>Label</p>")."""
    for label in labels:
        for tag in tags:
            v = extract_before_label(resp, label, tag=tag)
            if v is not None:
                return v
            v = extract_after_label(resp, label, tag=tag)
            if v is not None:
                return v
    return None


def season_snippet(resp):
    """A short piece of the page's HTML around the best season-like label, for diagnosis."""
    soup = BeautifulSoup(resp.text, "html.parser")
    for pat in (r"year.to.date", r"cumulative", r"this season|total snowfall", r"^\s*season\s*$"):
        node = soup.find(string=re.compile(pat, re.I))
        if node is not None:
            box = node.parent.parent if node.parent and node.parent.parent else node.parent
            return re.sub(r"\s+", " ", str(box))[:400]
    return ""


def season_hints(resp):
    """Short texts mentioning season / total / year-to-date, to learn a page's wording."""
    soup = BeautifulSoup(resp.text, "html.parser")
    seen = []
    for node in soup.find_all(string=True):
        t = " ".join(node.split())
        if 2 < len(t) <= 40 and re.search(r"season|ytd|year.to.date|cumulative|total", t, re.I) and t not in seen:
            seen.append(t)
    return " | ".join(seen[:6])


# ---------------------------------------------------------------------------
# Fallback: model-estimated snowfall over the last 24h and last 7 days
# ---------------------------------------------------------------------------
def _totals(vals):
    now_idx = 7 * 24 + datetime.now(TZ).hour

    def total(hours):
        window = vals[max(0, now_idx - hours + 1): now_idx + 1]
        return round(sum(v for v in window if v is not None), 1)

    return total(24), total(7 * 24)


def prefetch_model(resorts):
    """One Open-Meteo request for every resort (avoids rate limits and timeouts)."""
    pts = [(r["lat"], r["lon"]) for r in resorts]
    for attempt in range(3):
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": ",".join(str(a) for a, _ in pts),
                        "longitude": ",".join(str(b) for _, b in pts),
                        "hourly": "snowfall", "past_days": 7, "forecast_days": 1,
                        "timezone": "America/Edmonton"},
                headers=HEADERS, timeout=40)
            resp.raise_for_status()
            data = resp.json()
            data = data if isinstance(data, list) else [data]
            for pt, d in zip(pts, data):
                MODEL_CACHE[pt] = _totals(d["hourly"]["snowfall"])
            return
        except Exception as e:
            print(f"model prefetch attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(4 * (attempt + 1))


def open_meteo_cm(lat, lon):
    """Return (cm_last_24h, cm_last_7_days) from the Open-Meteo model."""
    r = None
    for attempt in range(4):  # retry: Open-Meteo returns 429 if called too fast
        try:
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
                timeout=30,
            )
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code not in (429, 500, 502, 503, 504):
            break
        time.sleep(3 * (attempt + 1))
    r.raise_for_status()
    vals = r.json()["hourly"]["snowfall"]  # starts 7 days ago, 00:00 local
    now_idx = 7 * 24 + datetime.now(TZ).hour

    def total(hours):
        window = vals[max(0, now_idx - hours + 1): now_idx + 1]
        return round(sum(v for v in window if v is not None), 1)

    return total(24), total(7 * 24)


# ---------------------------------------------------------------------------
_BROWSER_INSTALL_TRIED = False


def launch_browser(p):
    """Launch headless Chromium. If the browser isn't installed yet (the workflow's
    install step missing or skipped), install it once and retry."""
    global _BROWSER_INSTALL_TRIED
    try:
        return p.chromium.launch()
    except Exception as e:
        if _BROWSER_INSTALL_TRIED or "Executable doesn't exist" not in str(e):
            raise
        _BROWSER_INSTALL_TRIED = True
        import subprocess
        print("Chromium missing: installing it now...", file=sys.stderr)
        for cmd in ([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
                    [sys.executable, "-m", "playwright", "install", "chromium"]):
            if subprocess.run(cmd).returncode == 0:
                break
        return p.chromium.launch()


def fetch_rendered(url, wait_selector=None, wait_js=None, timeout_ms=45000):
    """Load a page in headless Chrome and return an object with .text (the
    rendered HTML), like a requests response.
    Waits until `wait_js` (a JS function string returning true) passes, or, if
    that isn't given, until `wait_selector` has some text.
    Needs: pip install playwright && python -m playwright install --with-deps chromium"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = launch_browser(p)
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except Exception as e:
                if "Timeout" not in type(e).__name__:
                    raise
                # never went fully idle (webcams, trackers): carry on with whatever has loaded
            timed_out = False
            try:  # a bot-check page ("verifying your request") often clears itself in a few seconds
                page.wait_for_function(
                    "() => !/please wait while your request is being verified|one moment, please/i"
                    ".test(document.body ? document.body.innerText : '')", timeout=20000)
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            try:
                if wait_js:
                    page.wait_for_function(wait_js, timeout=20000)
                elif wait_selector:
                    page.wait_for_function(
                        "sel => { const e = document.querySelector(sel); "
                        "return e && e.textContent.trim() !== ''; }",
                        arg=wait_selector, timeout=20000)
            except Exception as e:  # numbers never appeared: keep the page for diagnosis
                if "Timeout" not in type(e).__name__:
                    raise
                timed_out = True
            return SimpleNamespace(text=page.content(), timed_out=timed_out)
        finally:
            browser.close()


def diagnose(resp):
    """Short description of what the page actually showed, for the note in snow.json."""
    soup = BeautifulSoup(resp.text, "html.parser")
    boxes = [" ".join(e.get_text(" ", strip=True).split())
             for e in soup.select(".rcr-element-box-fl16, .summary-facilities__stat")]
    if boxes:
        return "snow boxes: " + " | ".join(boxes)[:350]
    text = " ".join(soup.get_text(" ", strip=True).split())
    m = re.search(r"snow|hrs|hours", text, re.I)
    i = max(0, (m.start() if m else 0) - 40)
    return "page text: " + text[i:i + 250]


SEASON = {}        # resort name -> season total in cm, when its page gave one
SEASON_NOTES = {}  # resort name -> why not
MODEL_CACHE = {}  # (lat, lon) -> (cm24, cm_week), filled by one batched request


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
            try:  # season total is a bonus: a failure here must never lose the 24 h / week numbers
                sv = resort["season"](resp) if resort.get("season") else extract_season(resp)
                if sv is None:
                    sv = extract_season(resp)
                SEASON[resort["name"]] = sv
                if sv is None:
                    SEASON_NOTES[resort["name"]] = ("season total not found. words seen: " + season_hints(resp)
                                                    + " || html: " + season_snippet(resp))
            except Exception as e:
                SEASON_NOTES[resort["name"]] = f"season extractor failed: {type(e).__name__}: {e}"[:200]
            print(f"[{resort['name']}] fetched {len(resp.text)} chars, "
                  f"extracted 24h={cm24} week={week}", file=sys.stderr)
            if cm24 is None or week is None:
                waited = " (numbers never appeared)" if getattr(resp, "timed_out", False) else ""
                NOTES[resort["name"]] = (f"page loaded ({len(resp.text)} chars){waited}, extractor "
                                         f"found 24h={cm24} week={week}. {diagnose(resp)}")[:600]
                if resort["name"] == "Lake Louise":
                    NOTES["Lake Louise"] = ("html: " + station_snippet(resp, "Mid Mountain")
                                            + " || " + NOTES["Lake Louise"])[:900]
        except Exception as e:
            msg = f"{type(e).__name__}: {e}".replace("\n", " ")[:300]
            NOTES[resort["name"]] = msg
            print(f"[{resort['name']}] resort scrape failed: {msg}", file=sys.stderr)

    if cm24 is not None and week is not None:
        return cm24, week, "resort"

    # 2) model fills whatever is missing
    try:
        key = (resort["lat"], resort["lon"])
        m24, mweek = MODEL_CACHE.get(key) or open_meteo_cm(*key)
    except Exception as e:
        print(f"[{resort['name']}] model fallback failed: {e}", file=sys.stderr)
        NOTES[resort["name"]] = (NOTES.get(resort["name"], "") + " | model failed: "
                                 f"{type(e).__name__}: {e}")[:400]
        m24 = mweek = None

    got_resort = cm24 is not None or week is not None
    got_model = (cm24 is None and m24 is not None) or (week is None and mweek is not None)
    if cm24 is None:
        cm24 = m24
    if week is None:
        week = mweek
    if cm24 is not None and week is not None and week < cm24:
        week = cm24  # a weekly total can't be below the last 24 hours
    if got_resort and got_model:
        return cm24, week, "mixed"
    if got_resort:
        return cm24, week, "resort"
    return cm24, week, ("model" if cm24 is not None or week is not None else "none")


KEEP_SEASON_HOURS = 72      # a season total only grows, so an older one is safe to show for a few days
KEEP_LAST_GOOD_HOURS = 14   # a blocked page keeps its last real report for up to this long


def load_previous(path):
    """Resort entries from the last snow.json, by name (empty if missing or unreadable)."""
    try:
        with open(path) as f:
            old = json.load(f)
        return {e["name"]: e for e in old.get("resorts", []) + old.get("web_only", [])}, old.get("updated")
    except Exception:
        return {}, None


def main(out_path="snow.json"):
    now = datetime.now(timezone.utc)
    out = {"updated": now.isoformat(timespec="seconds"), "resorts": [], "web_only": []}
    prev, prev_updated = load_previous(out_path)
    prefetch_model(RESORTS)
    for r in RESORTS:
        cm24, week, src = get_resort(r)
        entry = {"name": r["name"], "cm": cm24, "week": week, "src": src}
        if NOTES.get(r["name"]):
            entry["note"] = NOTES[r["name"]]
        season = SEASON.get(r["name"])
        if season is not None and week is not None and season < week:
            season = None  # a season total can't be below this week's snow: probably misread
        if season is None:
            old_s = prev.get(r["name"], {})
            try:
                s_age = (now - datetime.fromisoformat(old_s.get("season_asof"))).total_seconds() / 3600
            except Exception:
                s_age = None
            if old_s.get("season") is not None and s_age is not None and s_age <= KEEP_SEASON_HOURS:
                season = old_s["season"]
                entry["season_asof"] = old_s["season_asof"]
        else:
            entry["season_asof"] = now.isoformat(timespec="seconds")
        entry["season"] = season
        if season is None and SEASON_NOTES.get(r["name"]):
            entry["season_note"] = SEASON_NOTES[r["name"]][:800]
        if src in ("resort", "mixed"):
            entry["asof"] = now.isoformat(timespec="seconds")
        else:
            # The resort page was blocked or empty. Prefer its last real report (if recent)
            # over the weather-model guess, which can say 0 on a day it really snowed.
            old = prev.get(r["name"])
            asof = (old or {}).get("asof") or (prev_updated if (old or {}).get("src") == "resort" else None)
            try:
                age_h = (now - datetime.fromisoformat(asof)).total_seconds() / 3600
            except Exception:
                age_h = None
            if old and old.get("src") in ("resort", "mixed") and age_h is not None \
                    and age_h <= KEEP_LAST_GOOD_HOURS and old.get("cm") is not None:
                entry.update(cm=old["cm"], week=old.get("week", week), src="resort", asof=asof)
                entry["note"] = (f"resort page blocked this run; kept the report from {asof} "
                                 f"({age_h:.0f} h ago). " + entry.get("note", ""))[:600]
                cm24, week, src = entry["cm"], entry["week"], "kept"
        (out["web_only"] if r.get("web_only") else out["resorts"]).append(entry)
        print(f"{r['name']:<14} 24h={cm24} cm  week={week} cm  ({src})")
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:])
