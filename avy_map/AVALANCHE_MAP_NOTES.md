Avalanche zone map: side project notes

Status: paused until Avalanche Canada publishes its regular regional forecasts.

The idea

A laser-cut map of Canadian Rockies avalanche zones. Each zone is a backlit window with one LED under it. A scraper updates the danger level every day at 5 pm. LED colours: green = Low, yellow = Moderate, orange = Considerable, red = High, flashing red = Extreme, off = no rating.

What exists
tools/build_avy_map.py builds the laser files (front panel, light dividers, back panel) plus a zone table (avy_zones.csv / .json) that gives the LED number for each zone.
.github/workflows/build_avy_map.yml runs it from the Actions tab (Run workflow).
The tool was tested only on shapes traced from a screenshot, not on live data.
Build plan: front panel + divider layers stacked about 18 mm + white diffuser + back panel with LEDs.
What we found (Nov 2026)
Avalanche Canada's areas feed currently returns 8 areas, including one giant early-season region.
Their regions are dynamic: boundaries change with conditions, so fixed cut-out copies of their boundaries would drift out of date.
Regular regional forecasts were due to start around Nov 25 (their blog post: avalanche.ca/blogs/early-season-regions).
Decision to make next

Use FIXED zones (Cariboos, North Columbia, South Columbia, Purcells, Kootenay-Boundary, Jasper, Kananaskis, South Rockies, Sea to Sky, South Coast, South Coast Inland, Vancouver Island, Banff-Yoho-Kootenay ...) and look up each zone's danger rating by its centre point with the point lookup in Avalanche Canada's API: https://avcan-services-api.prod.avalanche.ca/forecasts/en/products/point?lat=...&long=... Zone shapes come from the screenshot; zone names still need confirming.

To do
Once all zones are reporting, check the map on avalanche.ca and the Actions run output (avy_debug.txt).
Settle the zone list and names; build the zone shapes.
Write the 5 pm scraper step (one rating per zone point) and add the ratings to snow.json.
Write the ESP32 firmware (one LED per zone, flashing red for Extreme, off for no rating).
Choose the board size (600 x 400 mm planned) and cut a test piece.
