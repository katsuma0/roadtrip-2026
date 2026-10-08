#!/usr/bin/env python3
"""Build docs/index.html from the inReach GPX track.

Reads the GPX, works out the trip stats, prints a summary table, and
fills template.html with the data embedded as JSON. Standard library
only, so it runs anywhere Python 3.8 or newer does.

    python build.py
    python build.py --trim-home-km 2
"""

import argparse
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone, tzinfo
from html import escape

GPX_NS = "{http://www.topografix.com/GPX/1/1}"

# Mean earth radius in metres. The track is a road trip, so the few
# metres of error from the sphere model are far below GPS noise.
EARTH_RADIUS_M = 6371008.8

# A parked device drifts a few metres between fixes. Steps shorter than
# this are jitter, not travel.
MIN_STEP_M = 20.0

# A step counts as moving when the implied speed clears this and the two
# fixes are close enough in time for the speed to mean anything.
MOVING_KMH = 5.0
MOVING_MAX_GAP_H = 3.0

# Past this much silence the device was off or blocked. No line is drawn
# across it and the distance between the two fixes is not counted.
GAP_H = 6.0

# Raw inReach elevation jumps around by tens of metres. A rolling median
# and a threshold stop that noise from being summed as climbing.
MEDIAN_WINDOW = 5
ELEV_THRESHOLD_M = 10.0

# The whole trip is grouped by Toronto dates even though it crossed three
# time zones. One clock keeps the day table honest about calendar days
# at home; the README says so.
TZ_NAME = "America/Toronto"

DEFAULT_TITLE = "Markham to British Columbia and back, Sep 5 to Oct 8, 2026"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class Eastern(tzinfo):
    """America/Toronto rules since 2007, for systems without a tz database.

    zoneinfo needs the system tz data, which stock Windows Python lacks.
    This fallback keeps the build runnable there.
    """

    STANDARD = timedelta(hours=-5)
    DAYLIGHT = timedelta(hours=-4)

    @staticmethod
    def _bounds_utc(year):
        # DST starts the second Sunday of March at 02:00 EST (07:00 UTC)
        # and ends the first Sunday of November at 02:00 EDT (06:00 UTC).
        mar1 = datetime(year, 3, 1)
        start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=7)
        nov1 = datetime(year, 11, 1)
        end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=6)
        return start, end

    def fromutc(self, dt):
        start, end = self._bounds_utc(dt.year)
        naive = dt.replace(tzinfo=None)
        return dt + (self.DAYLIGHT if start <= naive < end else self.STANDARD)

    def utcoffset(self, dt):
        if dt is None:
            return self.STANDARD
        start, end = self._bounds_utc(dt.year)
        # dt is wall time here. Shifting by the standard offset is close
        # enough to pick a side, and only matters at the two switch hours.
        naive = dt.replace(tzinfo=None) - self.STANDARD
        return self.DAYLIGHT if start <= naive < end else self.STANDARD

    def dst(self, dt):
        return self.utcoffset(dt) - self.STANDARD

    def tzname(self, dt):
        return "EDT" if self.dst(dt) else "EST"


def local_zone():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(TZ_NAME)
    except Exception:
        return Eastern()


def parse_time(text):
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def read_gpx(path):
    root = ET.parse(path).getroot()
    pts = []
    for tp in root.iter(GPX_NS + "trkpt"):
        ele = tp.find(GPX_NS + "ele")
        when = tp.find(GPX_NS + "time")
        if ele is None or when is None:
            continue
        pts.append({
            "t": parse_time(when.text),
            "lat": float(tp.get("lat")),
            "lon": float(tp.get("lon")),
            "ele": float(ele.text),
        })
    pts.sort(key=lambda p: p["t"])
    return pts


def haversine_m(a, b):
    la1, lo1 = math.radians(a["lat"]), math.radians(a["lon"])
    la2, lo2 = math.radians(b["lat"]), math.radians(b["lon"])
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def drop_duplicate_times(pts):
    """The inReach feed repeats a handful of fixes verbatim. A zero
    second step has no speed, so keep the first of each pair."""
    kept, dropped = [], 0
    for p in pts:
        if kept and p["t"] == kept[-1]["t"]:
            dropped += 1
            continue
        kept.append(p)
    return kept, dropped


def trim_home(pts, km):
    """Drop every fix within km of the first one. The first fix is the
    driveway, so this takes the home address off the public page."""
    if km <= 0 or not pts:
        return pts, 0
    home = pts[0]
    kept = [p for p in pts if haversine_m(home, p) >= km * 1000.0]
    return kept, len(pts) - len(kept)


def rolling_median(values, window):
    half = window // 2
    out = []
    for i in range(len(values)):
        chunk = sorted(values[max(0, i - half):i + half + 1])
        n = len(chunk)
        mid = n // 2
        out.append(chunk[mid] if n % 2 else (chunk[mid - 1] + chunk[mid]) / 2.0)
    return out


def oklch_to_hex(L, C, h):
    """Constant lightness keeps every day readable on both the light and
    the inverted dark tiles. Chroma is pulled in until sRGB can hold it."""
    def convert(L, C, h):
        a = C * math.cos(math.radians(h))
        b = C * math.sin(math.radians(h))
        l_ = L + 0.3963377774 * a + 0.2158037573 * b
        m_ = L - 0.1055613458 * a - 0.0638541728 * b
        s_ = L - 0.0894841775 * a - 1.2914855480 * b
        l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
        return (
            4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
        )

    def gamma(c):
        c = min(1.0, max(0.0, c))
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055

    rgb = convert(L, C, h)
    while C > 0 and (min(rgb) < -0.002 or max(rgb) > 1.002):
        C -= 0.005
        rgb = convert(L, C, h)
    return "#%02x%02x%02x" % tuple(int(round(gamma(c) * 255)) for c in rgb)


def day_colours(n):
    """Blue on the first day through green and yellow to red on the last,
    so the way out reads cool and the way back reads warm."""
    if n == 1:
        return [oklch_to_hex(0.66, 0.16, 250)]
    return [oklch_to_hex(0.66, 0.16, 250 - 250.0 * i / (n - 1)) for i in range(n)]


def day_label(d):
    return "%s %d" % (MONTHS[d.month - 1], d.day)


def hhmm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def compute(pts, tz):
    n = len(pts)
    if n < 2:
        raise SystemExit("need at least two points after trimming")

    for p in pts:
        p["local"] = p["t"].astimezone(tz)

    # Steps live between point i and i+1 and belong to the day of point i.
    steps = []
    for i in range(n - 1):
        a, b = pts[i], pts[i + 1]
        d = haversine_m(a, b)
        dt_h = (b["t"] - a["t"]).total_seconds() / 3600.0
        gap = dt_h > GAP_H
        counted = (not gap) and d >= MIN_STEP_M
        speed = (d / 1000.0) / dt_h if dt_h > 0 else 0.0
        moving = counted and dt_h < MOVING_MAX_GAP_H and speed > MOVING_KMH
        steps.append({"d": d, "dt_h": dt_h, "gap": gap, "counted": counted,
                      "speed": speed, "moving": moving})

    cum = [0.0]
    for s in steps:
        cum.append(cum[-1] + (s["d"] if s["counted"] else 0.0))

    # Smooth each run between gaps on its own so a 40 km jump does not
    # bleed into the median of the points beside it.
    smooth = []
    run_start = 0
    for i in range(n):
        if i == n - 1 or steps[i]["gap"]:
            smooth += rolling_median([p["ele"] for p in pts[run_start:i + 1]], MEDIAN_WINDOW)
            run_start = i + 1
    for p, e in zip(pts, smooth):
        p["ele_s"] = e

    dates = []
    for p in pts:
        d = p["local"].date()
        if not dates or dates[-1] != d:
            dates.append(d)
    day_index = {d: i for i, d in enumerate(dates)}
    for p in pts:
        p["day"] = day_index[p["local"].date()]

    days = []
    for i, d in enumerate(dates):
        days.append({"date": d.isoformat(), "label": day_label(d), "km": 0.0,
                     "moving_h": 0.0, "max_kmh": 0.0, "gain_m": 0.0, "loss_m": 0.0,
                     "points": 0, "first": None, "last": None, "segments": []})

    for i, p in enumerate(pts):
        day = days[p["day"]]
        day["points"] += 1
        if day["first"] is None:
            day["first"] = i
        day["last"] = i

    for i, s in enumerate(steps):
        day = days[pts[i]["day"]]
        if s["counted"]:
            day["km"] += s["d"] / 1000.0
        if s["moving"]:
            day["moving_h"] += s["dt_h"]
            day["max_kmh"] = max(day["max_kmh"], s["speed"])

    # Gain and loss only move when the smoothed line gets more than the
    # threshold away from the last counted level. A gap resets the level
    # because the distance across it is not travel either.
    ref = pts[0]["ele_s"]
    for i in range(1, n):
        e = pts[i]["ele_s"]
        if steps[i - 1]["gap"]:
            ref = e
            continue
        if e - ref > ELEV_THRESHOLD_M:
            days[pts[i]["day"]]["gain_m"] += e - ref
            ref = e
        elif ref - e > ELEV_THRESHOLD_M:
            days[pts[i]["day"]]["loss_m"] += ref - e
            ref = e

    # Polyline ranges per day, split at gaps. A range runs one point into
    # the next day when the step across midnight is real travel, so the
    # line stays joined and matches where that step's distance went.
    for di, day in enumerate(days):
        start = day["first"]
        for i in range(day["first"], day["last"] + 1):
            last_of_day = i == day["last"]
            if i < n - 1 and steps[i]["gap"]:
                day["segments"].append([start, i])
                start = i + 1
            elif last_of_day:
                end = i + 1 if i < n - 1 else i
                day["segments"].append([start, end])

    gaps = []
    for i, s in enumerate(steps):
        if s["gap"]:
            gaps.append({
                "from": int(pts[i]["t"].timestamp()),
                "to": int(pts[i + 1]["t"].timestamp()),
                "from_label": "%s %s" % (day_label(pts[i]["local"]), hhmm(pts[i]["local"])),
                "to_label": "%s %s" % (day_label(pts[i + 1]["local"]), hhmm(pts[i + 1]["local"])),
                "hours": round(s["dt_h"], 1),
                "jump_km": round(s["d"] / 1000.0, 1),
            })

    colours = day_colours(len(days))
    for di, day in enumerate(days):
        f, l = pts[day["first"]], pts[day["last"]]
        day["first_local"] = hhmm(f["local"])
        day["last_local"] = hhmm(l["local"])
        day["first"] = int(f["t"].timestamp())
        day["last"] = int(l["t"].timestamp())
        day["avg_kmh"] = round(day["km"] / day["moving_h"], 1) if day["moving_h"] > 0 else None
        day["km"] = round(day["km"], 1)
        day["moving_h"] = round(day["moving_h"], 2)
        day["max_kmh"] = round(day["max_kmh"], 1)
        day["gain_m"] = int(round(day["gain_m"]))
        day["loss_m"] = int(round(day["loss_m"]))
        day["color"] = colours[di]

    hi = max(range(n), key=lambda i: pts[i]["ele_s"])
    lo = min(range(n), key=lambda i: pts[i]["ele_s"])
    far = max(range(n), key=lambda i: haversine_m(pts[0], pts[i]))
    longest = max(steps, key=lambda s: s["dt_h"])
    totals = {
        "km": round(cum[-1] / 1000.0, 1),
        "days": len(days),
        "points": n,
        "first": int(pts[0]["t"].timestamp()),
        "last": int(pts[-1]["t"].timestamp()),
        "first_label": day_label(pts[0]["local"]),
        "last_label": day_label(pts[-1]["local"]),
        "high_m": int(round(pts[hi]["ele_s"])),
        "high_idx": hi,
        "low_m": int(round(pts[lo]["ele_s"])),
        "low_idx": lo,
        "farthest_km": int(round(haversine_m(pts[0], pts[far]) / 1000.0)),
        "farthest_idx": far,
        "longest_gap_h": round(longest["dt_h"], 1),
        "gain_m": sum(d["gain_m"] for d in days),
        "loss_m": sum(d["loss_m"] for d in days),
        "moving_h": round(sum(d["moving_h"] for d in days), 1),
        "start_end_km": round(haversine_m(pts[0], pts[-1]) / 1000.0, 1),
    }

    points = [[round(p["lat"], 6), round(p["lon"], 6), int(round(p["ele_s"])),
               int(p["t"].timestamp()), round(c / 1000.0, 2), p["day"]]
              for p, c in zip(pts, cum)]

    return {"points": points, "days": days, "gaps": gaps, "totals": totals}


def print_summary(data, dropped_dup, dropped_home, trim_km):
    t = data["totals"]
    head = "%-7s %8s %9s %9s %9s %7s %7s %5s" % (
        "day", "km", "moving h", "avg km/h", "max km/h", "gain m", "loss m", "pts")
    print(head)
    print("-" * len(head))
    for d in data["days"]:
        avg = "%.1f" % d["avg_kmh"] if d["avg_kmh"] is not None else "-"
        print("%-7s %8.1f %9.2f %9s %9.1f %7d %7d %5d" % (
            d["label"], d["km"], d["moving_h"], avg, d["max_kmh"],
            d["gain_m"], d["loss_m"], d["points"]))
    print("-" * len(head))
    print("%-7s %8.1f %9.2f %9s %9s %7d %7d %5d" % (
        "total", t["km"], t["moving_h"], "", "", t["gain_m"], t["loss_m"], t["points"]))
    print()
    print("%s km over %d days with data, %s to %s" % (
        format(t["km"], ",.1f"), t["days"], t["first_label"], t["last_label"]))
    print("highest %d m, lowest %d m (smoothed), farthest from start %s km" % (
        t["high_m"], t["low_m"], format(t["farthest_km"], ",d")))
    print("longest gap with no points %.1f h, start and end %.1f km apart" % (
        t["longest_gap_h"], t["start_end_km"]))
    print("dropped %d duplicate timestamps and %d points within %.1f km of home" % (
        dropped_dup, dropped_home, trim_km))
    if data["gaps"]:
        print("gaps over %d h, not counted as travel:" % GAP_H)
        for g in data["gaps"]:
            print("  %s to %s, %.1f h, %.1f km between the two fixes" % (
                g["from_label"], g["to_label"], g["hours"], g["jump_km"]))


def render(template_path, out_path, title, data):
    with open(template_path, encoding="utf-8") as f:
        html = f.read()
    t = data["totals"]
    subtitle = "%s km, %d days" % (format(t["km"], ",.0f"), t["days"])
    # A literal </script> inside the JSON would end the script block early.
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    for token, value in (("__TRIP_TITLE__", escape(title)),
                         ("__TRIP_SUBTITLE__", escape(subtitle)),
                         ("__TRIP_DATA__", blob)):
        if token not in html:
            raise SystemExit("template is missing %s" % token)
        html = html.replace(token, value)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--gpx", default="data/inreach-sep-oct-2026.gpx")
    ap.add_argument("--template", default="template.html")
    ap.add_argument("--out", default="docs/index.html")
    ap.add_argument("--title", default=DEFAULT_TITLE)
    ap.add_argument("--trim-home-km", type=float, default=1.5,
                    help="drop points within this distance of the first fix (default 1.5)")
    args = ap.parse_args(argv)

    pts = read_gpx(args.gpx)
    pts, dropped_dup = drop_duplicate_times(pts)
    pts, dropped_home = trim_home(pts, args.trim_home_km)
    data = compute(pts, local_zone())
    data["meta"] = {
        "title": args.title,
        "tz": TZ_NAME,
        "trim_home_km": args.trim_home_km,
        "min_step_m": MIN_STEP_M,
        "gap_h": GAP_H,
        "moving_kmh": MOVING_KMH,
        "moving_max_gap_h": MOVING_MAX_GAP_H,
        "elev_threshold_m": ELEV_THRESHOLD_M,
        "median_window": MEDIAN_WINDOW,
    }
    print_summary(data, dropped_dup, dropped_home, args.trim_home_km)

    if not os.path.exists(args.template):
        print("\nno %s yet, so nothing was written to %s" % (args.template, args.out))
        return 0
    render(args.template, args.out, args.title, data)
    print("\nwrote %s (%d KB)" % (args.out, os.path.getsize(args.out) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
