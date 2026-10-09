#!/usr/bin/env python3
"""Convert a Garmin inReach KML feed export into a plain GPX track.

The inReach export carries the owner's name, the device IMEI and a
Garmin point id on every placemark. None of that belongs in a repo or
on a public page, so only latitude, longitude, elevation and time make
it through. Run with: python tools/kml_to_gpx.py feed.kml data/track.gpx
"""

import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape

KML_NS = "http://www.opengis.net/kml/2.2"


def read_points(path):
    root = ET.parse(path).getroot()
    pts = []
    for pm in root.iter("{%s}Placemark" % KML_NS):
        point = pm.find("{%s}Point" % KML_NS)
        when = pm.find("{%s}TimeStamp/{%s}when" % (KML_NS, KML_NS))
        # The export also holds one LineString placemark with no timestamp.
        # Skipping it keeps the track to the timed points only.
        if point is None or when is None or when.text is None:
            continue
        coords = point.find("{%s}coordinates" % KML_NS).text.strip()
        lon, lat, ele = (float(v) for v in coords.split(","))
        t = datetime.strptime(when.text.strip(), "%Y-%m-%dT%H:%M:%SZ")
        t = t.replace(tzinfo=timezone.utc)
        pts.append((t, lat, lon, ele))
    # The feed is already time ordered but the GPX must be, so sort anyway.
    pts.sort(key=lambda p: p[0])
    return pts


def write_gpx(pts, path, name):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="roadtrip-2026 kml_to_gpx"',
        '     xmlns="http://www.topografix.com/GPX/1/1">',
        "  <trk>",
        "    <name>%s</name>" % escape(name),
        "    <trkseg>",
    ]
    for t, lat, lon, ele in pts:
        lines.append('      <trkpt lat="%.6f" lon="%.6f">' % (lat, lon))
        lines.append("        <ele>%.2f</ele>" % ele)
        lines.append("        <time>%s</time>" % t.strftime("%Y-%m-%dT%H:%M:%SZ"))
        lines.append("      </trkpt>")
    lines += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main(argv):
    if len(argv) < 3:
        sys.stderr.write("usage: kml_to_gpx.py FEED.kml OUT.gpx [TRACK NAME]\n")
        return 2
    name = argv[3] if len(argv) > 3 else "inReach track"
    pts = read_points(argv[1])
    write_gpx(pts, argv[2], name)
    print("%d points, %s to %s, written to %s" % (
        len(pts), pts[0][0].strftime("%Y-%m-%d"), pts[-1][0].strftime("%Y-%m-%d"), argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
