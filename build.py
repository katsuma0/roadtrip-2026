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

# A stay is a run of fixes that never leaves this radius of its first fix.
# One that lasts this long and spans three in the morning at home is a
# night. Three is late enough to be night in BC too, where it is midnight.
# A stop this long also splits the moving steps into drives.
STAY_RADIUS_M = 300.0
STAY_MIN_H = 6.0
NIGHT_HOUR = 3
# A drive shorter than this is the fix jumping at a stop, so it does not
# count as the start of a day.
MIN_DRIVE_H = 0.5

# Province borders, simplified to where the road crosses them. The prairie
# borders are a meridian each, the Ontario line is the one west of Kenora,
# the Manitoba line steps west as it goes north, and the BC line follows
# the passes the highways use until it meets the 120th meridian. A crossing
# can land a few kilometres off, which is a few minutes of a day.
PROVINCES = [("ON", "Ontario"), ("MB", "Manitoba"), ("SK", "Saskatchewan"),
             ("AB", "Alberta"), ("BC", "British Columbia")]
ON_MB_LON = -95.153
AB_SK_LON = -110.0
MB_SK_LINE = [(49.0, -101.36), (50.9, -101.45), (52.0, -101.6),
              (53.0, -101.75), (55.0, -102.0), (60.0, -102.0)]
AB_BC_LINE = [(49.0, -114.07), (49.63, -114.68), (50.6, -115.3), (51.45, -116.3),
              (52.3, -117.6), (52.9, -118.4), (53.8, -120.0), (60.0, -120.0)]

# The whole trip is grouped by Toronto dates even though it crossed three
# time zones. One clock keeps the day table honest about calendar days
# at home; the README says so.
TZ_NAME = "America/Toronto"

DEFAULT_TITLE = "Road trip to the Rockies"

# The car's odometer at the end of the trip. A fix every ten minutes cuts
# every bend into a chord, so the summed track comes out short; every
# distance is scaled so the total lands on this. Zero turns it off.
ODOMETER_KM = 12613.7

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# Spelled out rather than strftime("%a"), which follows the machine's
# locale and would put another language on the page.
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


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


def lon_on_line(line, lat):
    """Longitude of a north to south border line at this latitude."""
    if lat <= line[0][0]:
        return line[0][1]
    for (la1, lo1), (la2, lo2) in zip(line, line[1:]):
        if la1 <= lat <= la2:
            return lo1 + (lo2 - lo1) * (lat - la1) / (la2 - la1)
    return line[-1][1]


def province(lat, lon):
    if lon > ON_MB_LON:
        return "ON"
    if lon > lon_on_line(MB_SK_LINE, lat):
        return "MB"
    if lon > AB_SK_LON:
        return "SK"
    if lon > lon_on_line(AB_BC_LINE, lat):
        return "AB"
    return "BC"


def time_by_province(pts, steps):
    """Hours per province in the order they were entered. A step counts
    for the province its first fix is in, nights and gaps included, since
    the car was there for those too."""
    hours = {}
    order = []
    for i, s in enumerate(steps):
        code = province(pts[i]["lat"], pts[i]["lon"])
        if code not in hours:
            hours[code] = 0.0
            order.append(code)
        hours[code] += s["dt_h"]
    names = dict(PROVINCES)
    return [{"code": c, "name": names[c], "hours": round(hours[c], 1)} for c in order]


def find_nights(pts):
    """One entry per spot slept at, with how many nights in a row, and
    the first and last date of each, which the days use to find the spot
    they ended at."""
    n = len(pts)
    stays = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and haversine_m(pts[i], pts[j + 1]) <= STAY_RADIUS_M:
            j += 1
        if (pts[j]["t"] - pts[i]["t"]).total_seconds() >= STAY_MIN_H * 3600:
            stays.append((i, j))
        i = j + 1

    nights = []
    anchor = pts[0]["local"].replace(hour=NIGHT_HOUR, minute=0, second=0, microsecond=0)
    if anchor <= pts[0]["local"]:
        anchor += timedelta(days=1)
    while anchor < pts[-1]["local"]:
        date = (anchor - timedelta(days=1)).date()
        hit = [(a, b) for a, b in stays if pts[a]["local"] <= anchor <= pts[b]["local"]]
        if hit:
            nights.append({"idx": hit[0][0], "date": date, "off": False})
        else:
            # No stay covers the night, so the device was off. The last fix
            # before it is the best guess at where the night was spent.
            k = max(i for i in range(n) if pts[i]["local"] < anchor)
            nights.append({"idx": k, "date": date, "off": True})
        anchor += timedelta(days=1)

    spots = []
    for night in nights:
        last = spots[-1] if spots else None
        same_spot = (last is not None and not night["off"] and not last["off"]
                     and haversine_m(pts[last["idx"]], pts[night["idx"]]) <= STAY_RADIUS_M
                     and night["date"] == last["to"] + timedelta(days=1))
        if same_spot:
            last["nights"] += 1
            last["to"] = night["date"]
        else:
            spots.append({"idx": night["idx"], "nights": 1, "from": night["date"],
                          "to": night["date"], "off": night["off"]})
    return [{
        "idx": sp["idx"],
        "lat": round(pts[sp["idx"]]["lat"], 6),
        "lon": round(pts[sp["idx"]]["lon"], 6),
        "nights": sp["nights"],
        "from_label": day_label(sp["from"]),
        "to_label": day_label(sp["to"]),
        "off": sp["off"],
    } for sp in spots], [(sp["from"], sp["to"]) for sp in spots]


def province_visits(pts, steps):
    """Each stretch spent in one province without leaving it, in order.
    A visit's hours count the steps that start inside it, the same rule
    time_by_province uses, so the visits of a province add up to its total."""
    names = dict(PROVINCES)
    visits = []
    for i, p in enumerate(pts):
        code = province(p["lat"], p["lon"])
        if not visits or visits[-1]["code"] != code:
            visits.append({"code": code, "name": names[code], "from_idx": i, "to_idx": i, "hours": 0.0})
        visits[-1]["to_idx"] = i
        if i < len(steps):
            visits[-1]["hours"] += steps[i]["dt_h"]
    for v in visits:
        v["hours"] = round(v["hours"], 1)
    return visits


def minutes_after(midnight, t):
    """Minutes from a day's midnight, unrounded, so two days whose start or
    finish reads the same on the clock still have one that came first."""
    return (t - midnight).total_seconds() / 60.0


def find_records(days, nights, spans, dates, starts, ends, own, tz):
    """The days that stand out, plus the longest stay. Ties go to the
    earlier day. Speeds are left out: from a fix every ten minutes they
    say more about the gaps between fixes than about the driving."""
    def pick(candidates, key, largest=True):
        best = None
        for d in candidates:
            if best is None or (key(d) > key(best) if largest else key(d) < key(best)):
                best = d
        return best

    moving = [d for d in range(len(days)) if starts[d] is not None]
    # Start and finish only count for days with a drive of their own, so
    # the end of the drive home at midnight is not the earliest start.
    driven = [d for d in moving if own[d]]
    midnight = [datetime(dt.year, dt.month, dt.day, tzinfo=tz) for dt in dates]
    out = []

    def add(key, label, value_text, day=None, idx=None):
        out.append({"key": key, "label": label, "value_text": value_text, "day": day, "idx": idx})

    # A track with no driving or no night stop has nothing to pick for some
    # of these, and the page draws whichever records there are, so those
    # are left out rather than stopping the build.
    d = pick(moving, lambda d: days[d]["km"])
    if d is not None:
        add("longest_day", "Longest day", "%s km" % format(days[d]["km"], ",.1f"), d)
    d = pick(moving, lambda d: days[d]["moving_h"])
    if d is not None:
        add("longest_wheel", "Most hours", "%.1f h" % days[d]["moving_h"], d)
    d = pick(moving, lambda d: days[d]["gain_m"])
    if d is not None:
        add("most_climbing", "Most climbing", "%s m" % format(days[d]["gain_m"], ",d"), d)
    # Picked on the exact minutes, so wall clock rounding cannot tie two.
    d = pick(driven, lambda d: minutes_after(midnight[d], starts[d]), largest=False)
    if d is not None:
        add("earliest_start", "Earliest start", hhmm(starts[d]), d)
    d = pick(driven, lambda d: minutes_after(midnight[d], ends[d]))
    if d is not None:
        add("latest_finish", "Latest finish", hhmm(ends[d]), d)
    d = pick(moving, lambda d: days[d]["km"], largest=False)
    if d is not None:
        add("shortest_day", "Shortest day", "%s km" % format(days[d]["km"], ",.1f"), d)
    return out


def compute(pts, tz, odometer_km=ODOMETER_KM):
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
    # Only the distances scale. Speeds keep the raw chord lengths so the
    # moving test and the max speed do not inflate with them.
    scale = odometer_km * 1000.0 / cum[-1] if odometer_km > 0 and cum[-1] > 0 else 1.0
    cum = [c * scale for c in cum]

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
            day["km"] += s["d"] * scale / 1000.0
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
        day["weekday"] = WEEKDAYS[dates[di].weekday()]

    # When each day's driving started and stopped, as its first and last
    # moving step on that Toronto date. Not the first and last fix, since a
    # night stay carries fixes past midnight. Moving steps are joined into
    # drives wherever the car stopped for less than a night, and only a
    # drive of half an hour or more counts: a few minutes of "driving" at a
    # campsite at night is the fix jumping. A day starts with the first
    # drive that starts on it, so an evening in BC that runs past midnight
    # Toronto time does not start the next day at 00:16. A day finishes at
    # the last fix on its own date that one of those drives touched, so the
    # times cover the same date as the day's km and two days never claim
    # the same hours. A day with no drive of its own (the end of the drive
    # home) falls back to its plain moving steps.
    drives = []
    for i, s in enumerate(steps):
        if not s["moving"]:
            continue
        if drives and (pts[i]["t"] - pts[drives[-1][1] + 1]["t"]).total_seconds() < STAY_MIN_H * 3600:
            drives[-1][1] = i
        else:
            drives.append([i, i])
    starts = [None] * len(days)
    ends = [None] * len(days)
    own = [False] * len(days)
    in_drive = [False] * len(steps)
    for a, b in drives:
        if (pts[b + 1]["t"] - pts[a]["t"]).total_seconds() < MIN_DRIVE_H * 3600:
            continue
        for i in range(a, b + 1):
            in_drive[i] = steps[i]["moving"]
        d = pts[a]["day"]
        if starts[d] is None:
            starts[d] = pts[a]["local"]
        own[d] = True
    for i, s in enumerate(steps):
        d = pts[i]["day"]
        if not (in_drive[i] if own[d] else s["moving"]):
            continue
        if starts[d] is None:
            starts[d] = pts[i]["local"]
        # A step across midnight counts towards this day's km but ends on
        # the next date; its first fix is the last one this day has.
        ends[d] = pts[i + 1]["local"] if pts[i + 1]["day"] == d else pts[i]["local"]
    for di, day in enumerate(days):
        day["start_local"] = hhmm(starts[di]) if starts[di] is not None else None
        day["end_local"] = hhmm(ends[di]) if ends[di] is not None else None
        # Provinces in the order the day passed through them. A day that
        # crosses a border and comes back lists the first one twice.
        codes = []
        for i in range(n):
            if pts[i]["day"] != di:
                continue
            code = province(pts[i]["lat"], pts[i]["lon"])
            if not codes or codes[-1] != code:
                codes.append(code)
        day["provinces"] = codes

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
        "scale": round(scale, 4),
    }

    points = [[round(p["lat"], 6), round(p["lon"], 6), int(round(p["ele_s"])),
               int(p["t"].timestamp()), round(c / 1000.0, 2), p["day"]]
              for p, c in zip(pts, cum)]

    nights, spans = find_nights(pts)
    totals["nights"] = sum(sp["nights"] for sp in nights)
    totals["night_spots"] = len(nights)
    provinces = time_by_province(pts, steps)

    # The spot each day ended at: the night of a date belongs to that date.
    # The drive home ran past midnight, so the last two days have none.
    for di, dt in enumerate(dates):
        days[di]["night"] = next((k for k, (a, b) in enumerate(spans) if a <= dt <= b), None)
    records = find_records(days, nights, spans, dates, starts, ends, own, tz)

    return {"points": points, "days": days, "gaps": gaps, "nights": nights,
            "provinces": provinces, "province_visits": province_visits(pts, steps),
            "records": records, "totals": totals}


DEG = "\u00b0C"


def add_weather(data, pts, path):
    """Join the archived readings from tools/fetch_weather.py onto the
    fixes, days and nights. A missing file just means no weather on the
    page, so the build still works for anyone without it."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        wx = json.load(f)
    fixes = wx["fixes"]
    descs = []
    desc_at = {}
    per_point = []
    station_hour = []
    for p in pts:
        r = fixes.get(str(int(p["t"].timestamp())))
        if r is None:
            per_point.append(None)
            station_hour.append(None)
            continue
        station, _km, off, temp, wind, precip, _rh, _hx, _wc, desc = r
        di = None
        # Stations without an observer report "NA" rather than leaving it empty.
        if desc and desc.strip().upper() not in ("NA", "N/A"):
            di = desc_at.setdefault(desc, len(descs))
            if di == len(descs):
                descs.append(desc)
        per_point.append([temp, wind, precip, di])
        # One station hour can sit under several fixes ten minutes apart;
        # keyed so its rain is counted once.
        station_hour.append((station, int(p["t"].timestamp()) // 3600 + round(off / 60.0)))
    data["wx"] = per_point
    data["wx_desc"] = descs
    data["wx_source"] = {"name": "Environment and Climate Change Canada",
                         "url": "https://climate.weather.gc.ca/", "licence": wx.get("licence")}

    days = data["days"]
    for d, day in enumerate(days):
        idx = [i for i, p in enumerate(data["points"]) if p[5] == d and per_point[i]]
        temps = [per_point[i][0] for i in idx]
        winds = [per_point[i][1] for i in idx if per_point[i][1] is not None]
        rain = {}
        for i in idx:
            if per_point[i][2] is not None:
                rain[station_hour[i]] = per_point[i][2]
        names = [descs[per_point[i][3]] for i in idx if per_point[i][3] is not None]
        day["wx"] = None if not temps else {
            "hi": round(max(temps)), "lo": round(min(temps)),
            "wind_max": round(max(winds)) if winds else None,
            "precip_mm": round(sum(rain.values()), 1) if rain else None,
            "desc": max(set(names), key=names.count) if names else None,
        }

    # Overnight low: the coldest reading while parked at the spot between
    # six in the evening and nine in the morning, Toronto time.
    for spot in data["nights"]:
        home = pts[spot["idx"]]
        temps = []
        for i in range(spot["idx"], len(pts)):
            if haversine_m(home, pts[i]) > STAY_RADIUS_M:
                break
            h = pts[i]["local"].hour
            if per_point[i] and (h >= 18 or h < 9):
                temps.append(per_point[i][0])
        spot["low"] = round(min(temps)) if temps else None

    recs = data["records"]

    def add(key, label, value_text, day, idx):
        recs.append({"key": key, "label": label, "value_text": value_text, "day": day, "idx": idx})

    def day_of_night(k):
        return next((d for d, day in enumerate(days) if day.get("night") == k), None)

    lows = [(s["low"], k) for k, s in enumerate(data["nights"]) if s["low"] is not None]
    if lows:
        low, k = min(lows)
        add("coldest_night", "Coldest night", "%d %s" % (low, DEG), day_of_night(k), data["nights"][k]["idx"])
    have = [i for i, w in enumerate(per_point) if w]
    if have:
        i = max(have, key=lambda i: per_point[i][0])
        add("warmest", "Warmest reading", "%d %s" % (round(per_point[i][0]), DEG), data["points"][i][5], i)
        frost = next((i for i in have if per_point[i][0] <= 0), None)
        if frost is not None:
            add("first_frost", "First frost", "%d %s" % (round(per_point[frost][0]), DEG),
                data["points"][frost][5], frost)
        windy = [i for i in have if per_point[i][1] is not None]
        if windy:
            i = max(windy, key=lambda i: per_point[i][1])
            add("windiest", "Windiest reading", "%d km/h" % round(per_point[i][1]), data["points"][i][5], i)
    wet = [d for d, day in enumerate(days) if day["wx"] and day["wx"]["precip_mm"]]
    if wet:
        d = max(wet, key=lambda d: days[d]["wx"]["precip_mm"])
        add("wettest", "Wettest day", "%.1f mm" % days[d]["wx"]["precip_mm"], d, None)
    data["totals"]["wx_fixes"] = len(have)
    data["totals"]["wx_stations"] = len(wx["stations"])


def print_summary(data, dropped_dup, dropped_home, trim_km):
    t = data["totals"]
    head = "%-11s %8s %9s %9s %9s %7s %7s %5s  %-13s %5s  %s" % (
        "day", "km", "moving h", "avg km/h", "max km/h", "gain m", "loss m", "pts",
        "moving", "night", "provinces")
    print(head)
    print("-" * len(head))
    for d in data["days"]:
        avg = "%.1f" % d["avg_kmh"] if d["avg_kmh"] is not None else "-"
        span = "%s-%s" % (d["start_local"], d["end_local"]) if d["start_local"] else "-"
        night = "%d" % d["night"] if d["night"] is not None else "-"
        print("%-11s %8.1f %9.2f %9s %9.1f %7d %7d %5d  %-13s %5s  %s" % (
            d["weekday"] + " " + d["label"], d["km"], d["moving_h"], avg, d["max_kmh"],
            d["gain_m"], d["loss_m"], d["points"], span, night, " ".join(d["provinces"])))
    print("-" * len(head))
    print("%-11s %8.1f %9.2f %9s %9s %7d %7d %5d" % (
        "total", t["km"], t["moving_h"], "", "", t["gain_m"], t["loss_m"], t["points"]))
    print()
    print("%s km over %d days with data, %s to %s" % (
        format(t["km"], ",.1f"), t["days"], t["first_label"], t["last_label"]))
    print("highest %d m, lowest %d m (smoothed), farthest from start %s km" % (
        t["high_m"], t["low_m"], format(t["farthest_km"], ",d")))
    print("longest gap with no points %.1f h, start and end %.1f km apart" % (
        t["longest_gap_h"], t["start_end_km"]))
    print("%d nights at %d spots" % (t["nights"], t["night_spots"]))
    print("time by province: " + ", ".join(
        "%s %.0f h" % (p["name"], p["hours"]) for p in data["provinces"]))
    print("province visits in order: " + ", ".join(
        "%s %.0f h" % (v["code"], v["hours"]) for v in data["province_visits"]))
    print("records:")
    for r in data["records"]:
        when = data["days"][r["day"]]["label"] if r["day"] is not None else "-"
        print("  %-26s %-15s %s" % (r["label"], r["value_text"], when))
    if "wx_fixes" in t:
        print("weather for %d of %d fixes from %d Environment Canada stations" % (
            t["wx_fixes"], t["points"], t["wx_stations"]))
    print("dropped %d duplicate timestamps and %d points within %.1f km of home" % (
        dropped_dup, dropped_home, trim_km))
    if t["scale"] != 1:
        print("distances scaled by %.4f to match the odometer" % t["scale"])
    if data["gaps"]:
        print("gaps over %d h, not counted as travel:" % GAP_H)
        for g in data["gaps"]:
            print("  %s to %s, %.1f h, %.1f km between the two fixes" % (
                g["from_label"], g["to_label"], g["hours"], g["jump_km"]))


def render(template_path, out_path, title, data):
    with open(template_path, encoding="utf-8") as f:
        html = f.read()
    # A literal </script> inside the JSON would end the script block early.
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    for token, value in (("__TRIP_TITLE__", escape(title)),
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
    ap.add_argument("--weather", default="data/weather.json",
                    help="archived readings from tools/fetch_weather.py (skipped if missing)")
    ap.add_argument("--trim-home-km", type=float, default=1.5,
                    help="drop points within this distance of the first fix (default 1.5)")
    ap.add_argument("--odometer-km", type=float, default=ODOMETER_KM,
                    help="scale distances so the total matches this (default %s, 0 to skip)" % ODOMETER_KM)
    args = ap.parse_args(argv)
    if not math.isfinite(args.odometer_km) or args.odometer_km < 0:
        ap.error("--odometer-km must be 0 or a distance in km")

    pts = read_gpx(args.gpx)
    pts, dropped_dup = drop_duplicate_times(pts)
    pts, dropped_home = trim_home(pts, args.trim_home_km)
    data = compute(pts, local_zone(), args.odometer_km)
    add_weather(data, pts, args.weather)
    # The template reads these as meta.thresholds.*, so they stay nested
    # rather than mixed in with the title and zone.
    data["meta"] = {
        "title": args.title,
        "tz": TZ_NAME,
        "thresholds": {
            "trim_home_km": args.trim_home_km,
            "min_step_m": MIN_STEP_M,
            "gap_h": GAP_H,
            "moving_kmh": MOVING_KMH,
            "moving_max_gap_h": MOVING_MAX_GAP_H,
            "elev_threshold_m": ELEV_THRESHOLD_M,
            "median_window": MEDIAN_WINDOW,
            "odometer_km": args.odometer_km,
        },
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
