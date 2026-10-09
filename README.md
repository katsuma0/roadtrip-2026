# Road trip 2026

A map of the drive from Markham to British Columbia and back, Sep 5 to Oct 8, 2026, drawn from the inReach track. One HTML page, Leaflet for the map, no framework and no bundler. The page lives at https://katsuma0.github.io/roadtrip-2026/ once Pages serves the docs folder from main.

## Rebuild

    python build.py

That reads data/inreach-sep-oct-2026.gpx, prints a table of days and the trip totals, and writes docs/index.html with the stats and every point left after the trim embedded as JSON. Python 3.8 or newer, standard library only. The flags are `--trim-home-km` (default 1.5), `--title`, `--gpx`, `--template` and `--out`. The page is template.html; build.py swaps in the title, the subtitle and the data.

## Deploy

GitHub Pages serves the docs folder from main. In the repo settings under Pages, set the source to "Deploy from a branch", pick main and the /docs folder. Nothing runs on GitHub's side, so commit docs/index.html after every rebuild. The only files the page fetches are Leaflet 1.9.4 from cdnjs and OpenStreetMap tiles.

## How the numbers are made

Distance is haversine between consecutive fixes, and any step under 20 m is ignored so a parked device does not add jitter. A step counts as moving when the implied speed is over 5 km/h and the two fixes are under 3 h apart; average moving speed is the day's distance over its moving time. Elevation goes through a rolling median over 5 points first, and gain and loss only move when the smoothed line gets more than 10 m from the last counted level. The highest and lowest points come from that smoothed line too, which is why the lowest point reads a few metres below sea level near the coast.

Any silence over 6 h is a gap. The line breaks there, the distance between the two fixes is not counted, and the gap shows in the stats. There are four, the longest 15.7 h.

Days are America/Toronto dates. The trip crossed into Central, Mountain and Pacific time, so a day in the table is a Toronto calendar day even where the clock on the dash said something else. That is a deliberate simplification; one clock keeps the table honest about calendar days at home.

After the 1.5 km trim the start and end are 2.8 km apart, not 1.5 km. The first surviving fix is already 6.9 km from home and the last one is 5.1 km out, because the device logged every 10 minutes and I was on the highway. The threshold stayed at 1.5 km.

## Privacy

The page is public. The first and last fixes are at home, so build.py drops every point within `--trim-home-km` of the first one before writing the page. That trim happens at build time only. The GPX in data/ still holds the untrimmed track, and the repo is public, so anyone can open it on GitHub and read the home coordinates from the first point.

Each point in the GPX carries lat, lon, ele and time and nothing else. The inReach KML export has the device IMEI and my name on every point, so the KML stays out of git and tools/kml_to_gpx.py strips those fields when it writes the GPX:

    python tools/kml_to_gpx.py feed.kml data/inreach-sep-oct-2026.gpx "inReach track, Sep to Oct 2026"

The GPX holds 1,557 points over 34 days, six of them repeats, and most of the rest are ten minutes apart.
