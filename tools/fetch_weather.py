#!/usr/bin/env python3
"""Fetch archived weather along the trip from Environment and Climate Change
Canada and write data/weather.json, which build.py reads.

Run once with network access: python tools/fetch_weather.py
The page never calls the API; it only reads the committed file, so the build
stays offline and the result never changes under me.

Data: Environment and Climate Change Canada, Open Government Licence - Canada.
"""

import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build  # noqa: E402  same points as the page, so epochs line up

API = "https://api.weather.gc.ca/collections/"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".weather-cache")
# Nearest station within this many km of a fix, and a reading within this
# many minutes of it; past that the number says more about the station than
# about the road.
MAX_KM = 80.0
MAX_MIN = 60


def get(collection, params):
    """GET with an on-disk cache so a rerun after a failure resumes."""
    query = urllib.parse.urlencode(params)
    os.makedirs(CACHE, exist_ok=True)
    key = os.path.join(CACHE, collection + "_" + str(abs(hash(query)) % 10 ** 12) + ".json")
    if os.path.exists(key):
        with open(key) as f:
            return json.load(f)
    url = API + collection + "/items?" + query
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "roadtrip-2026 weather fetch"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            with open(key, "w") as f:
                json.dump(data, f)
            time.sleep(0.3)
            return data
        except Exception as e:  # retry 5xx, timeouts and rate limits alike
            wait = 2 ** attempt
            sys.stderr.write("retry %s in %ss: %s\n" % (collection, wait, e))
            time.sleep(wait)
    raise SystemExit("gave up on " + url)


def num(v):
    return None if v is None or v == "" else float(v)


def main():
    pts = build.read_gpx("data/inreach-sep-oct-2026.gpx")
    pts, _ = build.drop_duplicate_times(pts)
    pts, _ = build.trim_home(pts, 1.5)
    t0 = pts[0]["t"].strftime("%Y-%m-%dT00:00:00Z")
    t1 = pts[-1]["t"].strftime("%Y-%m-%dT23:59:59Z")

    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    bbox = "%.2f,%.2f,%.2f,%.2f" % (min(lons) - 1, min(lats) - 1, max(lons) + 1, max(lats) + 1)
    st = get("climate-stations", {"f": "json", "limit": 10000, "bbox": bbox, "HAS_HOURLY_DATA": "Y"})
    stations = []
    for f in st["features"]:
        p = f["properties"]
        last = p.get("HLY_LAST_DATE") or ""
        if last[:10] < pts[-1]["t"].strftime("%Y-%m-%d"):
            continue
        stations.append({"id": p["CLIMATE_IDENTIFIER"], "name": p["STATION_NAME"].title(),
                         "province": p.get("PROV_STATE_TERR_CODE"),
                         "lat": float(p["LATITUDE"]) / 1e7 if abs(float(p["LATITUDE"])) > 1000 else float(p["LATITUDE"]),
                         "lon": float(p["LONGITUDE"]) / 1e7 if abs(float(p["LONGITUDE"])) > 1000 else float(p["LONGITUDE"])})
    print("%d hourly stations active through the trip" % len(stations))

    def near(lat, lon, k=3):
        here = {"lat": lat, "lon": lon}
        ranked = sorted(((build.haversine_m(here, s) / 1000.0, i) for i, s in enumerate(stations)))
        return [(d, i) for d, i in ranked[:k] if d <= MAX_KM]

    cand = [near(p["lat"], p["lon"]) for p in pts]
    needed = sorted({i for c in cand for _, i in c})
    print("fetching hourly data for %d stations" % len(needed))

    hourly = {}
    for n, i in enumerate(needed):
        data = get("climate-hourly", {"f": "json", "limit": 10000, "CLIMATE_IDENTIFIER": stations[i]["id"],
                                      "datetime": t0 + "/" + t1})
        rows = {}
        for f in data["features"]:
            p = f["properties"]
            t = datetime.strptime(p["UTC_DATE"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            rows[int(t.timestamp())] = (num(p.get("TEMP")), num(p.get("WIND_SPEED")), num(p.get("PRECIP_AMOUNT")),
                                        num(p.get("RELATIVE_HUMIDITY")), num(p.get("HUMIDEX")), num(p.get("WINDCHILL")),
                                        p.get("WEATHER_ENG_DESC") or None)
        hourly[i] = rows
        sys.stdout.write("\r%d/%d %s          " % (n + 1, len(needed), stations[i]["name"][:30]))
    print()

    used = {}
    fixes = {}
    missing = 0
    for p, c in zip(pts, cand):
        epoch = int(p["t"].timestamp())
        hour = epoch - epoch % 3600
        pick = None
        for d, i in c:
            for h in (hour, hour + 3600, hour - 3600):
                r = hourly.get(i, {}).get(h)
                if r and r[0] is not None and abs(h - epoch) <= MAX_MIN * 60:
                    pick = (d, i, r, h)
                    break
            if pick:
                break
        if not pick:
            missing += 1
            continue
        d, i, r, h = pick
        si = used.setdefault(i, len(used))
        fixes[str(epoch)] = [si, round(d, 1), round((h - epoch) / 60.0)] + list(r)

    out = {
        "source": "Environment and Climate Change Canada, hourly climate data",
        "licence": "Open Government Licence - Canada",
        "fields": ["station", "km", "minutes_off", "temp_c", "wind_kmh", "precip_mm",
                   "rh_pct", "humidex", "windchill", "desc"],
        "stations": [None] * len(used),
        "fixes": fixes,
    }
    for i, si in used.items():
        out["stations"][si] = stations[i]
    with open("data/weather.json", "w") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)
    print("%d of %d fixes have weather, %d without; %d stations; %d KB" % (
        len(fixes), len(pts), missing, len(used), os.path.getsize("data/weather.json") // 1024))


if __name__ == "__main__":
    main()
