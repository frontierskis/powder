#!/usr/bin/env python3
"""
Build laser-cutter files for an Avalanche Canada zone map: one backlit window and one LED per zone.

It downloads the official forecast-zone polygons and zone names from Avalanche Canada, fits them onto a
board, insets every zone so thin wooden bridges remain between neighbours, and writes:

  avy_front_panel.svg   CUT (red): outline, zone windows, callout lights, screw holes.   ENGRAVE (black): title, legend, numbers.
  avy_baffle.svg        CUT (red): outline + the same zone windows. Cut it several times and stack it as the light dividers.
  avy_back_panel.svg    CUT (red): outline, screw holes, one wire hole per LED.  ENGRAVE (black): LED position marks + numbers.
  avy_zones.csv / avy_zones.json   LED number <-> zone id/name table (the order your ESP32 and scraper must share)
  avy_preview.svg       coloured preview of the front panel (not for cutting)

Usage:   python tools/build_avy_map.py --out avy_map
Options: --bbox MINLON MINLAT MAXLON MAXLAT   zones whose centre is inside this box are used
         --width 600 --height 400             board size in mm
         --inset 0.9                          half the wooden bridge between zones, in mm
         --areas FILE --metadata FILE         use saved JSON instead of downloading (testing)
         --names FILE                         JSON {zone id or name: "SHORT NAME"} for the legend
Needs: numpy opencv-python-headless fonttools requests (and matplotlib, only to find a font).
"""
import argparse, csv, json, math, os, sys
import numpy as np, cv2
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.misc.transform import Transform

API = 'https://avcan-services-api.prod.avalanche.ca/forecasts/en/'
RES, OCC = 8, 4          # raster pixels per mm for shapes / for the free-space map

# ------------------------------------------------------------------ fonts
def find_font():
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get('AVY_FONT', ''), os.path.join(here, 'fonts', 'Inter-ExtraBold.otf'),
             '/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf']
    try:
        import matplotlib; cands.append(os.path.join(os.path.dirname(matplotlib.__file__), 'mpl-data/fonts/ttf/DejaVuSans-Bold.ttf'))
    except Exception: pass
    for c in cands:
        if c and os.path.exists(c): return c
    sys.exit('No font found. Set AVY_FONT=/path/to/a/bold.ttf')
_font = TTFont(find_font()); _gs = _font.getGlyphSet(); _cm = _font.getBestCmap()
_upm = _font['head'].unitsPerEm
try: _cap = _font['OS/2'].sCapHeight or 0.73 * _upm
except Exception: _cap = 0.73 * _upm
def _g(ch): return _gs[_cm.get(ord(ch), _cm.get(ord('?')))]
def text_w(s, cap, tr=0.0): return sum(_g(c).width for c in s) * cap / _cap + tr * max(len(s) - 1, 0)
def text_d(s, x, y, cap, anchor='start', tr=0.0):
    """SVG path data for text with its baseline at (x, y); y grows downward (SVG)."""
    k = cap / _cap; w = text_w(s, cap, tr); pos = {'start': 0, 'middle': -w / 2, 'end': -w}[anchor]
    pen = SVGPathPen(_gs, ntos=lambda v: '%.3f' % v)
    for ch in s:
        g = _g(ch); g.draw(TransformPen(pen, Transform(k, 0, 0, -k, x + pos, y))); pos += g.width * k + tr
    return pen.getCommands()

# ------------------------------------------------------------------ data
def load(path, url):
    if path:
        return json.load(open(path))
    import requests
    r = requests.get(url, timeout=60, headers={'User-Agent': 'avy-zone-map-builder'}); r.raise_for_status(); return r.json()

def ring_mean(poly):
    pts = poly[0]; return [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]

def read_zones(areas, meta, bbox, names):
    mm = {}
    for m in meta if isinstance(meta, list) else []:
        a = m.get('area') or {}
        if a.get('id'): mm[a['id']] = m
    zones = []
    for f in areas.get('features', []):
        g = f.get('geometry') or {}
        polys = [g['coordinates']] if g.get('type') == 'Polygon' else g.get('coordinates', []) if g.get('type') == 'MultiPolygon' else []
        if not polys: continue
        zid = f.get('id') or (f.get('properties') or {}).get('id')
        m = mm.get(zid, {}); c = m.get('centroid') or {}
        cen = [c['longitude'], c['latitude']] if 'longitude' in c else (f.get('properties') or {}).get('centroid') or ring_mean(polys[0])
        if not (bbox[0] <= cen[0] <= bbox[2] and bbox[1] <= cen[1] <= bbox[3]): continue
        name = ((m.get('area') or {}).get('name')) or ((m.get('product') or {}).get('title')) or str(zid)[:8]
        short = names.get(zid) or names.get(name) or name
        zones.append(dict(id=zid, name=name, short=short, owner=((m.get('owner') or {}).get('display') or ''), polys=polys, cen=cen,
                          danger=((m.get('highestDanger') or {}).get('value') or 'norating')))
    return zones

# ------------------------------------------------------------------ geometry helpers
def fit(zones, W, H, margin):
    lons = [p[0] for z in zones for poly in z['polys'] for ring in poly for p in ring]
    lats = [p[1] for z in zones for poly in z['polys'] for ring in poly for p in ring]
    lon0, lon1, lat0, lat1 = min(lons), max(lons), min(lats), max(lats)
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    S = min((W - 2 * margin) / ((lon1 - lon0) * kx), (H - 2 * margin) / (lat1 - lat0))
    ox = margin + ((W - 2 * margin) - (lon1 - lon0) * kx * S) / 2
    oy = margin + ((H - 2 * margin) - (lat1 - lat0) * S) / 2
    return lambda lon, lat: (ox + (lon - lon0) * kx * S, H - (oy + (lat - lat0) * S))      # mm, y down

def fill_rings(mask, polys, proj, scale):
    for poly in polys:
        for i, ring in enumerate(poly):
            pts = np.array([[proj(*p)[0] * scale * 16, proj(*p)[1] * scale * 16] for p in ring]).round().astype(np.int32)
            cv2.fillPoly(mask, [pts.reshape(-1, 1, 2)], 255 if i == 0 else 0, lineType=cv2.LINE_8, shift=4)

def contours_mm(mask, eps_px=0.8):
    cs, _ = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in cs:
        if len(c) < 4: continue
        c = cv2.approxPolyDP(c, eps_px, True).reshape(-1, 2)
        if len(c) >= 3: out.append([(x / RES, y / RES) for x, y in c])
    return out
def path_d(rings): return ' '.join('M' + ' L'.join('%.3f,%.3f' % p for p in r) + ' Z' for r in rings)
def circ_d(cx, cy, r, n=40): return path_d([[(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]])
def bar_d(a, b, w):
    dx, dy = b[0] - a[0], b[1] - a[1]; L = math.hypot(dx, dy) or 1; nx, ny = -dy / L * w / 2, dx / L * w / 2
    return path_d([[(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]])
def rrect_d(x0, y0, x1, y1, r, n=8):
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(n + 1):
            a = math.radians(a0 + 90 * i / n); pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return path_d([pts])

# ------------------------------------------------------------------ main build
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='avy_map'); ap.add_argument('--width', type=float, default=600); ap.add_argument('--height', type=float, default=400)
    ap.add_argument('--bbox', type=float, nargs=4, default=[-129.5, 48.5, -111.0, 54.6])
    ap.add_argument('--inset', type=float, default=0.9); ap.add_argument('--areas'); ap.add_argument('--metadata'); ap.add_argument('--names')
    a = ap.parse_args(); W, H = a.width, a.height; os.makedirs(a.out, exist_ok=True)
    names = json.load(open(a.names)) if a.names else {}
    areas = load(a.areas, API + 'areas')
    try: meta = load(a.metadata, API + 'metadata')
    except Exception as e: print('metadata unavailable:', e); meta = []
    zones = read_zones(areas, meta, a.bbox, names)
    if not zones: sys.exit('No zones inside the bounding box.')
    print(len(zones), 'zones')
    proj = fit(zones, W, H, 16)
    Wp, Hp = int(W * RES), int(H * RES)

    # 1. windows
    occ = np.zeros((int(H * OCC), int(W * OCC)), np.uint8)
    for z in zones:
        m = np.zeros((Hp, Wp), np.uint8); fill_rings(m, z['polys'], proj, RES)
        d = cv2.distanceTransform(m, cv2.DIST_L2, 5)
        inset = (d >= a.inset * RES).astype(np.uint8) * 255
        n, lab, st, _ = cv2.connectedComponentsWithStats(inset, connectivity=8)
        keep = np.zeros_like(inset); best = (0.0, None)
        for i in range(1, n):
            comp = (lab == i); area_mm = st[i, 4] / RES ** 2
            dd = cv2.distanceTransform(comp.astype(np.uint8), cv2.DIST_L2, 5); r_in = dd.max() / RES
            if area_mm < 6 or r_in < 1.2: continue
            keep[comp] = 255
            if r_in > best[0]: y, x = np.unravel_index(dd.argmax(), dd.shape); best = (r_in, (x / RES, y / RES))
        z['windows'] = contours_mm(keep) if keep.any() else []
        if best[1]: z['led'] = best[1]; z['kind'] = 'window'; z['r_in'] = best[0]
        else:
            y, x = np.unravel_index(d.argmax(), d.shape); z['pole'] = (x / RES, y / RES); z['r_zone'] = d.max() / RES; z['kind'] = 'callout'
        o = np.zeros_like(occ); fill_rings(o, z['polys'], proj, OCC); occ |= o
    occ = cv2.dilate(occ, np.ones((3, 3), np.uint8), iterations=int(0.8 * OCC) or 1)
    z0 = {z['id']: z for z in zones}

    # 2. text blocks go in empty corners
    def free_map(): return cv2.distanceTransform((occ == 0).astype(np.uint8) * 255, cv2.DIST_L2, 5) / OCC
    def reserve_rect(x0, y0, x1, y1): occ[int(y0 * OCC):int(y1 * OCC) + 1, int(x0 * OCC):int(x1 * OCC) + 1] = 255
    def rect_free(x0, y0, x1, y1):
        if x0 < 10 or y0 < 10 or x1 > W - 10 or y1 > H - 10: return False
        return not occ[int(y0 * OCC):int(y1 * OCC) + 1, int(x0 * OCC):int(x1 * OCC) + 1].any()
    def place_block(w, h, order):
        for corner in order:
            for dx in range(0, 120, 6):
                for dy in range(0, 120, 6):
                    x0 = 14 + dx if corner[0] == 'l' else W - 14 - w - dx
                    y0 = 14 + dy if corner[1] == 't' else H - 14 - h - dy
                    if rect_free(x0, y0, x0 + w, y0 + h): reserve_rect(x0 - 2, y0 - 2, x0 + w + 2, y0 + h + 2); return x0, y0
        print('WARNING: no room for a text block of %dx%d mm' % (w, h)); return 14, 14
    eng = []; cut = []
    tw1 = text_w('AVALANCHE DANGER', 12, 0.6); bx, by = place_block(max(tw1, 150), 30, ['lt', 'lb', 'rt', 'rb'])
    eng.append(text_d('AVALANCHE DANGER', bx, by + 12, 12, tr=0.6))
    eng.append(text_d('AVALANCHE CANADA FORECAST ZONES  ·  UPDATED DAILY', bx, by + 21, 3.0, tr=0.9))
    eng.append(bar_d((bx, by + 15.2), (bx + tw1, by + 15.2), 0.7))
    leg = ['GREEN = LOW', 'YELLOW = MODERATE', 'ORANGE = CONSIDERABLE', 'RED = HIGH', 'RED FLASHING = EXTREME', 'LIGHT OFF = NO RATING']
    lw = max(text_w(t, 2.8, 0.4) for t in leg) + 2; lx, ly = place_block(lw, 5 + 4.6 * len(leg), ['rb', 'rt', 'lb', 'lt'])
    eng.append(text_d('DANGER LEVEL', lx, ly + 3.2, 2.8, tr=0.8))
    for i, t in enumerate(leg): eng.append(text_d(t, lx, ly + 9.5 + 4.6 * i, 2.8, tr=0.4))

    # 3. LED order: nearest neighbour, starting bottom-left
    for z in zones:
        if z['kind'] == 'callout':
            z['tmp'] = z['pole']
        else: z['tmp'] = z['led']
    left = list(zones); cur = min(left, key=lambda z: z['tmp'][0] - z['tmp'][1]); order = [cur]; left.remove(cur)
    while left:
        cur = min(left, key=lambda z: math.hypot(z['tmp'][0] - cur['tmp'][0], z['tmp'][1] - cur['tmp'][1])); order.append(cur); left.remove(cur)
    for i, z in enumerate(order, 1): z['n'] = i

    # 4. callouts for tiny zones, then number labels
    def find_spot(cx, cy, r, maxd=70):
        fm = free_map()
        for d in np.arange(0, maxd, 1.5):
            for ang in range(0, 360, 12 if d else 360):
                x = cx + d * math.cos(math.radians(ang)); y = cy + d * math.sin(math.radians(ang))
                if 12 < x < W - 12 and 12 < y < H - 12 and fm[int(y * OCC), int(x * OCC)] >= r: return x, y
        return None
    def mark(x, y, r): cv2.circle(occ, (int(x * OCC), int(y * OCC)), int(r * OCC), 255, -1)
    for z in order:
        if z['kind'] != 'callout': continue
        s = find_spot(*z['pole'], 6.0)
        if not s: print('WARNING: no room for callout', z['name']); z['led'] = z['pole']; continue
        z['led'] = s; mark(s[0], s[1], 4.6); z['windows'] = [[(s[0] + 3.2 * math.cos(2 * math.pi * i / 40), s[1] + 3.2 * math.sin(2 * math.pi * i / 40)) for i in range(40)]]
        eng.append(bar_d(s, z['pole'], 0.7)); eng.append(circ_d(z['pole'][0], z['pole'][1], 1.0, 14))
    for z in order:
        c = z['led'] if z['kind'] == 'callout' else z['led']
        ref = z['led'] if z['kind'] == 'callout' else z['led']
        s = find_spot(ref[0], ref[1], 3.6)
        if not s: print('WARNING: no room for label', z['n']); continue
        mark(s[0], s[1], 3.4); eng.append(text_d(str(z['n']), s[0], s[1] + 1.2, 3.2, 'middle'))
        gap = math.hypot(s[0] - ref[0], s[1] - ref[1]) - (z.get('r_in', 3.2))
        if z['kind'] == 'window' and gap > 5:
            ux, uy = (ref[0] - s[0]), (ref[1] - s[1]); L = math.hypot(ux, uy); ux, uy = ux / L, uy / L
            eng.append(bar_d((s[0] + ux * 3.0, s[1] + uy * 3.0), (ref[0] - ux * (z['r_in'] + 0.4), ref[1] - uy * (z['r_in'] + 0.4)), 0.6))
    # 5. name list
    items = ['%d  %s' % (z['n'], z['short'].upper()[:30]) for z in order]
    nw = max(text_w(t, 2.5, 0.3) for t in items) + 2; nh = 4.1 * len(items) + 6
    nx, ny = place_block(nw, nh, ['rt', 'lb', 'rb', 'lt'])
    eng.append(text_d('ZONES', nx, ny + 3.2, 2.8, tr=0.8))
    for i, t in enumerate(items): eng.append(text_d(t, nx, ny + 9 + 4.1 * i, 2.5, tr=0.3))

    # 6. write files
    scr = [(12, 12), (W - 12, 12), (12, H - 12), (W - 12, H - 12), (W / 2, 12), (W / 2, H - 12)]
    outline = rrect_d(0, 0, W, H, 6)
    def svg(groups, title):
        s = ['<svg xmlns="http://www.w3.org/2000/svg" width="%.1fmm" height="%.1fmm" viewBox="0 0 %.1f %.1f">' % (W, H, W, H), '<title>%s</title>' % title]
        s += groups; s.append('</svg>'); return '\n'.join(s)
    def cutg(paths): return '<g id="CUT" fill="none" stroke="#ff0000" stroke-width="0.1">%s</g>' % ''.join('<path d="%s"/>' % p for p in paths)
    def engg(paths): return '<g id="ENGRAVE" fill="#000000" fill-rule="evenodd" stroke="none">%s</g>' % ''.join('<path d="%s"/>' % p for p in paths)
    wins = [path_d(z['windows']) for z in order if z['windows']]; screws = [circ_d(x, y, 1.9, 16) for x, y in scr]
    open(os.path.join(a.out, 'avy_front_panel.svg'), 'w').write(svg([engg(eng), cutg([outline] + wins + screws)], 'Avalanche zone map front panel %.0f x %.0f mm. Red = cut, black = engrave.' % (W, H)))
    open(os.path.join(a.out, 'avy_baffle.svg'), 'w').write(svg([cutg([outline] + wins + screws)], 'Light dividers: cut this several times and stack. Red = cut.'))
    back_e = []; back_c = [outline] + screws
    for z in order:
        x, y = z['led']; back_e.append(circ_d(x, y, 4.2, 28).replace('Z', 'Z') + ' ' + circ_d(x, y, 3.2, 28)); back_e.append(text_d(str(z['n']), x + 5.2, y + 1.2, 3.0)); back_c.append(circ_d(x - 7.5, y, 1.5, 16))
    open(os.path.join(a.out, 'avy_back_panel.svg'), 'w').write(svg([engg(back_e), cutg(back_c)], 'Back panel. Black rings = LED positions, small holes = wire holes. Red = cut.'))
    rows = [dict(led=z['n'], zone_id=z['id'], name=z['name'], owner=z['owner'], x_mm=round(z['led'][0], 1), y_mm=round(H - z['led'][1], 1), kind=z['kind']) for z in order]
    with open(os.path.join(a.out, 'avy_zones.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, rows[0].keys()); w.writeheader(); w.writerows(rows)
    json.dump({'zones': [dict(led=r['led'], id=r['zone_id'], name=r['name']) for r in rows]}, open(os.path.join(a.out, 'avy_zones.json'), 'w'), indent=1)
    col = {'low': '#52ba4a', 'moderate': '#fff300', 'considerable': '#f79218', 'high': '#ef1c29', 'extreme': '#8a1c8a'}
    demo = ['#52ba4a', '#fff300', '#f79218', '#ef1c29']
    pv = ['<rect width="%.1f" height="%.1f" rx="6" fill="#fbfaf7"/>' % (W, H)]
    for i, z in enumerate(order):
        c = col.get(str(z['danger']).lower(), demo[i % 4])
        if z['windows']: pv.append('<path d="%s" fill="%s" fill-rule="evenodd" opacity=".85"/>' % (path_d(z['windows']), c))
    pv.append('<path d="%s" fill="#b98655" fill-rule="evenodd"/>' % ' '.join(eng))
    open(os.path.join(a.out, 'avy_preview.svg'), 'w').write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %.1f %.1f">%s</svg>' % (W, H, ''.join(pv)))
    for r in rows: print(r['led'], r['kind'], r['name'][:50])
    print('wrote files to', a.out)

if __name__ == '__main__': main()
