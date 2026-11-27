#!/usr/bin/env python3
"""Wisconsin trip safety checker.

Give it a start and a destination; it builds the driving route and checks
everything along it:

  * NWS (api.weather.gov)  - active winter/severe alerts + hourly forecast
  * 511WI WZDx feed         - work zones and lane/road closures (no key)
  * 511WI events            - crashes, incidents, closures   (needs WI511_KEY)
  * 511WI winter roads      - snow/ice covered segments      (needs WI511_KEY)

and prints a verdict: GOOD / CAUTION / BAD, with the reasons.

Standard library only. Usage:

  python3 road_alerts.py "Waukesha, WI" "Racine, WI"
  python3 road_alerts.py waukesha racine --sms        # short text-message form
  python3 road_alerts.py "43.01,-88.23" "Fox Lake, WI" --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CENTRAL = ZoneInfo("America/Chicago")
USER_AGENT = "road-alerts/0.1 (personal trip safety checker)"

# Shortcut names you can type instead of an address.
PLACES = {
    "waukesha": ("Waukesha, WI", 43.0117, -88.2315),
    "racine": ("Racine, WI", 42.7261, -87.7829),
    "fox lake": ("Fox Lake, WI", 43.5655, -88.9065),
    "foxlake": ("Fox Lake, WI", 43.5655, -88.9065),
    "milwaukee": ("Milwaukee, WI", 43.0389, -87.9065),
    "brookfield": ("Brookfield, WI", 43.0606, -88.1065),
    "oconomowoc": ("Oconomowoc, WI", 43.1117, -88.4993),
    "kenosha": ("Kenosha, WI", 42.5847, -87.8212),
    "madison": ("Madison, WI", 43.0731, -89.4012),
}

ON_ROUTE_KM = 0.25     # event this close to the route *and* same road = on route
NEAR_ROUTE_KM = 1.5    # otherwise, this close = "nearby"
LEVELS = {"GOOD": 0, "CAUTION": 1, "BAD": 2}

# NWS alert events and the verdict they force.
NWS_BAD = {
    "Blizzard Warning", "Ice Storm Warning", "Winter Storm Warning",
    "Lake Effect Snow Warning", "Snow Squall Warning", "Extreme Cold Warning",
    "Dense Fog Advisory", "Flash Flood Warning", "Tornado Warning",
    "Severe Thunderstorm Warning", "High Wind Warning", "Freezing Fog Advisory",
}
NWS_CAUTION = {
    "Winter Storm Watch", "Winter Weather Advisory", "Freezing Rain Advisory",
    "Lake Effect Snow Advisory", "Wind Chill Advisory", "Wind Chill Warning",
    "Cold Weather Advisory", "Extreme Cold Watch", "Wind Advisory",
    "Flood Warning", "Flood Advisory", "Special Weather Statement",
    "Blizzard Watch", "Ice Storm Watch", "Lake Effect Snow Watch",
}


# --------------------------------------------------------------------------
# HTTP

def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: int = 30, retries: int = 2):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (400, 401, 403, 404):
                break
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url.split('?')[0]} failed: {last}")


# --------------------------------------------------------------------------
# Geometry helpers

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def decode_polyline(s: str):
    """Google encoded polyline -> [(lat, lon), ...]."""
    coords, i, lat, lon = [], 0, 0, 0
    try:
        while i < len(s):
            for which in (0, 1):
                shift = result = 0
                while True:
                    b = ord(s[i]) - 63
                    i += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                d = ~(result >> 1) if result & 1 else result >> 1
                if which == 0:
                    lat += d
                else:
                    lon += d
            coords.append((lat / 1e5, lon / 1e5))
    except IndexError:
        pass  # some feeds pad the string with junk; keep what decoded
    return [c for c in coords if 40 < c[0] < 50 and -95 < c[1] < -84]


class Corridor:
    """Densified route points in a grid, for fast 'how far from route' checks."""

    CELL = 0.02  # degrees (~2 km)

    def __init__(self, latlons, step_km=0.15):
        pts = []
        for (a, b), (c, d) in zip(latlons, latlons[1:]):
            n = max(1, int(haversine_km(a, b, c, d) / step_km))
            for k in range(n):
                t = k / n
                pts.append((a + (c - a) * t, b + (d - b) * t))
        pts.append(latlons[-1])
        self.points = pts
        self.grid: dict[tuple[int, int], list] = {}
        for p in pts:
            self.grid.setdefault(self._cell(*p), []).append(p)

    def _cell(self, lat, lon):
        return int(lat // self.CELL), int(lon // self.CELL)

    def distance_km(self, lat, lon, max_km=5.0):
        ci, cj = self._cell(lat, lon)
        reach = int(max_km / 2) + 1
        best = math.inf
        for di in range(-reach, reach + 1):
            for dj in range(-reach, reach + 1):
                for p in self.grid.get((ci + di, cj + dj), ()):
                    d = haversine_km(lat, lon, p[0], p[1])
                    if d < best:
                        best = d
        return best

    def min_distance(self, latlons, max_km=5.0):
        return min((self.distance_km(a, b, max_km) for a, b in latlons), default=math.inf)

    def overlap_km(self, latlons, within_km=0.3):
        """Approx. length of a polyline that runs along the route."""
        total = 0.0
        for (a, b), (c, d) in zip(latlons, latlons[1:]):
            mid = ((a + c) / 2, (b + d) / 2)
            if self.distance_km(*mid, max_km=2) <= within_km:
                total += haversine_km(a, b, c, d)
        return total

    def sample(self, every_km=15.0):
        out, acc = [self.points[0]], 0.0
        for p, q in zip(self.points, self.points[1:]):
            acc += haversine_km(*p, *q)
            if acc >= every_km:
                out.append(q)
                acc = 0.0
        if out[-1] != self.points[-1]:
            out.append(self.points[-1])
        return out


# --------------------------------------------------------------------------
# Road-name matching ("I-94 EB" vs OSM "I 94; US 41")

_ROAD_RE = re.compile(
    r"\b(I|IH|INTERSTATE|US|USH|WIS|WI|STH|SH|STATE HWY|CTH|CO|COUNTY|CR|HWY)"
    r"[\s\-]*(?:ROAD\s+|HIGHWAY\s+)?([0-9]{1,3}[A-Z]?|[A-Z]{1,2})\b")
_PREFIX = {"I": "I", "IH": "I", "INTERSTATE": "I", "US": "US", "USH": "US",
           "WIS": "WIS", "WI": "WIS", "STH": "WIS", "SH": "WIS", "STATE HWY": "WIS",
           "HWY": "WIS", "CTH": "CTH", "CO": "CTH", "COUNTY": "CTH", "CR": "CTH"}


def road_keys(text: str | None) -> set[str]:
    if not text:
        return set()
    t = text.upper().replace(";", " ; ")
    keys = set()
    for pre, num in _ROAD_RE.findall(t):
        p = _PREFIX[pre]
        if p != "CTH" and not num[0].isdigit():
            continue
        keys.add(f"{p}-{num}")
    if not keys:  # plain street name, e.g. "Moreland Blvd"
        name = re.sub(r"\b(EB|WB|NB|SB|EASTBOUND|WESTBOUND|NORTHBOUND|SOUTHBOUND)\b", "", t)
        name = re.sub(r"[^A-Z0-9 ]", " ", name).split()
        if name:
            keys.add(" ".join(name))
    return keys


# --------------------------------------------------------------------------
# Geocoding and routing

@dataclass
class Place:
    label: str
    lat: float
    lon: float


def geocode(text: str) -> Place:
    t = text.strip()
    # "Home=43.02,-88.17" -> named coordinates (keeps addresses out of the code)
    m = re.fullmatch(r"\s*(?:([^=]+?)\s*=\s*)?(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", t)
    if m:
        name = m[1] or f"{float(m[2]):.4f},{float(m[3]):.4f}"
        return Place(name, float(m[2]), float(m[3]))
    key = t.lower().removesuffix(", wi").removesuffix(" wi").strip()
    if key in PLACES:
        label, lat, lon = PLACES[key]
        return Place(label, lat, lon)
    res = get_json("https://nominatim.openstreetmap.org/search",
                   {"q": t, "format": "jsonv2", "limit": 1, "countrycodes": "us",
                    "addressdetails": 1, "viewbox": "-92.9,47.1,-86.2,42.0"})  # prefer Wisconsin
    if not res:
        raise SystemExit(f"Could not find a place called {text!r}.")
    r = res[0]
    a = r.get("address", {})
    town = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("county")
    full = r["display_name"].split(", United States")[0]
    state = (a.get("ISO3166-2-lvl4") or "").removeprefix("US-")
    if not town:
        label = full
    elif full.startswith(town):
        label = f"{town}, {state}" if state else town
    else:
        label = f"{town} ({full.split(',')[0]})"
    return Place(label, float(r["lat"]), float(r["lon"]))


@dataclass
class Route:
    latlons: list
    miles: float
    minutes: float
    roads: set           # normalized road keys used by the route
    road_labels: list    # human-readable main roads, in order


def get_route(a: Place, b: Place, via: list[Place] | None = None) -> Route:
    """Driving route a -> (via points, to force the roads you actually take) -> b."""
    stops = [a, *(via or []), b]
    url = ("https://router.project-osrm.org/route/v1/driving/"
           + ";".join(f"{p.lon},{p.lat}" for p in stops))
    d = get_json(url, {"overview": "full", "geometries": "geojson", "steps": "true"})
    if d.get("code") != "Ok" or not d.get("routes"):
        raise RuntimeError(f"Routing failed: {d.get('code')}")
    r = d["routes"][0]
    latlons = [(lat, lon) for lon, lat in r["geometry"]["coordinates"]]
    roads, labels, dist_by_label = set(), [], {}
    for leg in r["legs"]:
        for s in leg["steps"]:
            ref, name = s.get("ref") or "", s.get("name") or ""
            roads |= road_keys(ref) | road_keys(name)
            label = ref.split(";")[0].strip() if ref else name
            label = re.sub(r"^(I|US|WI|CTH) (\w+)$",
                           lambda m: ("WIS" if m[1] == "WI" else m[1]) + ("-" if m[1] in ("I", "US") else " ") + m[2],
                           label)
            if label:
                dist_by_label[label] = dist_by_label.get(label, 0) + s["distance"]
                if label not in labels:
                    labels.append(label)
    cutoff = min(3000, r["distance"] * 0.15)
    main = [l for l in labels if dist_by_label[l] > cutoff][:6]
    return Route(latlons, r["distance"] / 1609.34, r["duration"] / 60, roads, main)


# --------------------------------------------------------------------------
# Findings

@dataclass
class Finding:
    level: str          # GOOD / CAUTION / BAD
    source: str         # NWS, 511WI, WZDx, Forecast
    title: str
    detail: str = ""
    where: str = ""     # on route / nearby

    def short(self):
        return f"{self.title}{' (' + self.where + ')' if self.where else ''}"


@dataclass
class Report:
    origin: Place
    dest: Place
    route: Route | None
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # data-source problems
    checked_at: datetime = field(default_factory=lambda: datetime.now(CENTRAL))

    @property
    def verdict(self):
        lvl = max((LEVELS[f.level] for f in self.findings), default=0)
        return [k for k, v in LEVELS.items() if v == lvl][0]


def _fmt_time(iso: str | None):
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).astimezone(CENTRAL).strftime("%a %-I:%M%p").replace(":00", "")
    except ValueError:
        return iso


def check_nws_alerts(corr: Corridor, rep: Report):
    seen = {}
    for lat, lon in corr.sample(every_km=12):
        try:
            d = get_json("https://api.weather.gov/alerts/active",
                         {"point": f"{lat:.4f},{lon:.4f}"},
                         {"Accept": "application/geo+json"})
        except RuntimeError as e:
            rep.notes.append(f"NWS alerts: {e}")
            return
        for f in d.get("features", []):
            p = f["properties"]
            seen.setdefault(p["id"], p)
    shown = set()
    for p in seen.values():
        ev = p["event"]
        sig = (ev, p.get("ends") or p.get("expires"))
        if sig in shown:  # same alert issued for neighbouring zones
            continue
        shown.add(sig)
        level = "BAD" if ev in NWS_BAD else "CAUTION" if ev in NWS_CAUTION else None
        if not level:
            continue
        desc = re.sub(r"\s+", " ", p.get("description") or "")
        what = re.search(r"\* WHAT\.\.\.(.*?)(?:\* WHERE|$)", desc)
        impacts = re.search(r"\* IMPACTS\.\.\.(.*?)(?:\* |$)", desc)
        detail = " ".join(x.group(1).strip() for x in (what, impacts) if x)
        rep.findings.append(Finding(
            level, "NWS", f"{ev} until {_fmt_time(p.get('ends') or p.get('expires'))}",
            detail[:300] or (p.get("headline") or ""), "on route"))


def check_forecast(rep: Report, hours: int):
    """Hourly forecast at start and destination over the drive window."""
    now = datetime.now(timezone.utc)
    for place in (rep.origin, rep.dest):
        try:
            pt = get_json(f"https://api.weather.gov/points/{place.lat:.4f},{place.lon:.4f}",
                          headers={"Accept": "application/geo+json"})
            fc = get_json(pt["properties"]["forecastHourly"],
                          headers={"Accept": "application/geo+json"})
        except (RuntimeError, KeyError) as e:
            rep.notes.append(f"NWS forecast for {place.label}: {e}")
            continue
        periods = [p for p in fc["properties"]["periods"]
                   if datetime.fromisoformat(p["endTime"]) > now][:hours]
        worst = None
        for p in periods:
            temp = p["temperature"] if p.get("temperatureUnit") == "F" else p["temperature"] * 9 / 5 + 32
            sf = p["shortForecast"].lower()
            pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
            precip = any(w in sf for w in ("snow", "sleet", "freezing", "ice", "flurr", "rain", "drizzle", "wintry"))
            lvl, why = None, None
            if any(w in sf for w in ("freezing rain", "freezing drizzle", "ice", "sleet")):
                lvl, why = "BAD", "freezing rain/sleet"
            elif "heavy snow" in sf or "blizzard" in sf or ("snow" in sf and pop >= 60):
                lvl, why = "BAD", "snow"
            elif precip and temp <= 34 and pop >= 30:
                lvl, why = "CAUTION", "precip near freezing - icy spots likely"
            elif "snow" in sf or "flurr" in sf:
                lvl, why = "CAUTION", "light snow"
            elif "fog" in sf:
                lvl, why = "CAUTION", "fog"
            if lvl and (worst is None or LEVELS[lvl] > LEVELS[worst[0]]):
                worst = (lvl, why, p, temp, pop)
        if worst:
            lvl, why, p, temp, pop = worst
            rep.findings.append(Finding(
                lvl, "Forecast", f"{why.capitalize()} near {place.label.split(',')[0]}",
                f"{_fmt_time(p['startTime'])}: {p['shortForecast']}, {temp:.0f}°F, {pop}% precip", "forecast"))
        elif periods:
            p = periods[0]
            rep.notes.append(f"Forecast {place.label.split(',')[0]}: {p['shortForecast']}, {p['temperature']}°F")


def _road_match(event_roads: set, route_roads: set):
    return bool(event_roads & route_roads)


def check_wzdx(corr: Corridor, route: Route, rep: Report):
    try:
        d = get_json("https://511wi.gov/api/wzdx", timeout=60)
    except RuntimeError as e:
        rep.notes.append(f"511WI work zones: {e}")
        return
    now = datetime.now(timezone.utc)
    for f in d.get("features", []):
        p = f["properties"]
        try:
            start = datetime.fromisoformat(p["start_date"])
            end = datetime.fromisoformat(p["end_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (start <= now <= end):
            continue
        impact = p.get("vehicle_impact") or ""
        if impact not in ("all-lanes-closed", "some-lanes-closed", "flagging", "temporary-traffic-signal"):
            continue
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        pts = [(c[1], c[0]) for c in coords if isinstance(c, list) and len(c) >= 2]
        if not pts:
            continue
        dist = corr.min_distance(pts, max_km=NEAR_ROUTE_KM + 1)
        if dist > NEAR_ROUTE_KM:
            continue
        core = p.get("core_details", {})
        roads = set().union(*(road_keys(r) for r in core.get("road_names", []))) if core.get("road_names") else set()
        on_route = dist <= ON_ROUTE_KM and _road_match(roads, route.roads)
        desc = core.get("description") or ""
        is_ramp = "RAMP" in desc.upper()
        if impact == "all-lanes-closed":
            title = "Ramp closed" if is_ramp else "Road closed"
        else:
            title = {"some-lanes-closed": "Lane closure", "flagging": "Flagging crew",
                     "temporary-traffic-signal": "Temporary signal"}[impact]
        # Long-running work zones are a heads-up, not a reason to stay home.
        # Only a full (non-ramp) closure on the road you're driving raises the verdict.
        level = "CAUTION" if (on_route and impact == "all-lanes-closed" and not is_ramp) else "GOOD"
        road = ", ".join(core.get("road_names", [])[:2])
        rep.findings.append(Finding(
            level, "WZDx", f"{title}: {road}".strip(": "), desc[:220],
            "on route" if on_route else f"{dist:.1f} km from route"))


def _wi511_key():
    k = os.environ.get("WI511_KEY")
    if not k:
        kf = HERE / ".wi511_key"
        if kf.exists():
            k = kf.read_text().strip()
    return k or None


def check_511_events(corr: Corridor, route: Route, rep: Report, key: str):
    try:
        events = get_json("https://511wi.gov/api/v2/get/event", {"key": key, "format": "json"})
    except RuntimeError as e:
        rep.notes.append(f"511WI events: {e}")
        return
    for e in events or []:
        pts = [(e.get("Latitude"), e.get("Longitude")), (e.get("LatitudeSecondary"), e.get("LongitudeSecondary"))]
        pts = [p for p in pts if p[0] and p[1]]
        if e.get("EncodedPolyline"):
            pts += decode_polyline(e["EncodedPolyline"])
        if not pts:
            continue
        dist = corr.min_distance(pts, max_km=NEAR_ROUTE_KM + 1)
        if dist > NEAR_ROUTE_KM:
            continue
        on_route = dist <= ON_ROUTE_KM and _road_match(road_keys(e.get("RoadwayName")), route.roads)
        etype = e.get("EventType") or ""
        full = bool(e.get("IsFullClosure"))
        if etype == "roadwork" and not full:
            continue  # covered by WZDx
        if full and on_route:
            level, title = "BAD", "Road CLOSED on your route"
        elif etype == "accidentsAndIncidents" and on_route:
            level, title = "CAUTION", "Crash/incident on your route"
        elif full:
            level, title = "GOOD", "Road closed nearby"
        elif on_route:
            level, title = "CAUTION", f"{e.get('EventSubType') or etype} on your route"
        else:
            level, title = "GOOD", f"{e.get('EventSubType') or etype} nearby"
        rep.findings.append(Finding(
            level, "511WI", f"{title}: {e.get('RoadwayName', '')}",
            (e.get("Description") or "")[:240],
            "on route" if on_route else f"{dist:.1f} km from route"))


WINTER_LEVEL = {
    "travel not advised": "BAD",
    "completely covered": "BAD",
    "partially covered": "CAUTION",
}


def check_winter_roads(corr: Corridor, route: Route, rep: Report, key: str):
    try:
        segs = get_json("https://511wi.gov/api/v3/get/winterroads", {"key": key, "format": "json"})
    except RuntimeError as e:
        rep.notes.append(f"511WI winter roads: {e}")
        return
    bad = {}
    for s in segs or []:
        cond = (s.get("Overall Condition") or s.get("OverallCondition") or "").strip()
        surface = (s.get("RoadSurface") or "").strip()
        level = WINTER_LEVEL.get(cond.lower())
        if not level:
            surf = surface.lower()
            if any(w in surf for w in ("ice", "icy", "snow", "slush", "slippery", "covered")):
                level = "CAUTION"
        if not level:
            continue
        polys = s.get("EncodedPolyline") or []
        if isinstance(polys, str):
            polys = [polys]
        pts = [p for enc in polys for p in decode_polyline(enc)]
        if not pts:
            continue
        overlap = corr.overlap_km(pts)
        if overlap < 0.5:
            continue
        k = (s.get("RoadwayName"), cond, surface)
        b = bad.setdefault(k, {"level": level, "km": 0.0, "where": []})
        b["km"] += overlap
        b["where"].append(s.get("LocationDescription") or "")
    for (road, cond, surface), b in bad.items():
        rep.findings.append(Finding(
            b["level"], "511WI", f"{road}: {cond or surface}",
            f"{b['km'] * 0.621:.0f} mi of your route. Surface: {surface or 'n/a'}. "
            + "; ".join(w for w in b["where"][:3] if w), "on route"))


# --------------------------------------------------------------------------

def run_check(origin_text: str, dest_text: str, hours: int = 3,
              via_texts: list[str] | None = None) -> Report:
    origin, dest = geocode(origin_text), geocode(dest_text)
    via = [geocode(v) for v in (via_texts or [])]
    rep = Report(origin, dest, None)
    try:
        route = get_route(origin, dest, via)
    except RuntimeError as e:
        rep.notes.append(f"Routing failed ({e}); using a straight line between the two points.")
        route = Route([(origin.lat, origin.lon), (dest.lat, dest.lon)],
                      haversine_km(origin.lat, origin.lon, dest.lat, dest.lon) * 0.621, 0, set(), [])
    rep.route = route
    corr = Corridor(route.latlons)

    check_nws_alerts(corr, rep)
    check_forecast(rep, hours)
    check_wzdx(corr, route, rep)
    key = _wi511_key()
    if key:
        check_511_events(corr, route, rep, key)
        check_winter_roads(corr, route, rep, key)
    else:
        rep.notes.append("No WI511_KEY set: 511WI crashes, closures and snow/ice road "
                         "conditions were NOT checked.")
    return rep


ICON = {"GOOD": "OK", "CAUTION": "CAUTION", "BAD": "BAD"}
VERDICT_TEXT = {
    "GOOD": "Roads look good - no weather or closure problems found on your route.",
    "CAUTION": "Drivable, but use caution.",
    "BAD": "Not a safe time to drive this route if you can avoid it.",
}


def _sorted(findings):
    return sorted(findings, key=lambda f: (-LEVELS[f.level], f.where != "on route"))


def format_full(rep: Report) -> str:
    r = rep.route
    out = [f"Trip: {rep.origin.label}  ->  {rep.dest.label}",
           f"Checked {rep.checked_at.strftime('%a %b %-d, %-I:%M %p %Z')}"]
    if r and r.minutes:
        out.append(f"Route: {r.miles:.0f} mi via {', '.join(r.road_labels) or 'local roads'} "
                   f"(routing estimate ~{r.minutes:.0f} min; usually faster)")
    out += ["", f"VERDICT: {rep.verdict} - {VERDICT_TEXT[rep.verdict]}", ""]
    important = [f for f in rep.findings if f.level != "GOOD"]
    heads_up = [f for f in rep.findings if f.level == "GOOD" and f.where == "on route"]
    nearby = [f for f in rep.findings if f.level == "GOOD" and f.where != "on route"]
    if important:
        out.append("Issues:")
        for f in _sorted(important):
            out.append(f"  [{f.level}] {f.source}: {f.short()}")
            if f.detail:
                out.append(f"      {f.detail}")
    if heads_up:
        out.append("Heads-up on your route (work zones, won't stop you):")
        for f in heads_up:
            out.append(f"  - {f.title}: {f.detail[:140]}")
    if nearby:
        out.append(f"Off-route but nearby ({len(nearby)}): " + "; ".join(f.title for f in nearby[:4])
                   + (" ..." if len(nearby) > 4 else ""))
    if rep.notes:
        out += ["", "Notes:"] + [f"  - {n}" for n in rep.notes]
    return "\n".join(out)


def format_sms(rep: Report, limit: int = 600) -> str:
    """Compact text for a phone. Kept short; SMS gateways split long messages."""
    o, d = rep.origin.label.split(",")[0], rep.dest.label.split(",")[0]
    head = f"Roads {o}->{d}: {rep.verdict}."
    r = rep.route
    if r and r.road_labels:
        head += f" Via {', '.join(r.road_labels[:4])}."
    parts = [head]
    for f in _sorted([f for f in rep.findings if f.level != "GOOD"])[:4]:
        parts.append(f"- {f.short()}")
    if rep.verdict == "GOOD":
        fcs = [n.replace("Forecast ", "") for n in rep.notes if n.startswith("Forecast")]
        parts.append("No weather alerts or closures on route.")
        if fcs:
            parts.append("Wx: " + " | ".join(fcs))
    work = [f for f in rep.findings if f.level == "GOOD" and f.where == "on route"]
    if work:
        parts.append(f"{len(work)} work zone(s) on route: " + ", ".join(sorted({f.title.split(': ')[-1] for f in work}))[:120])
    if not _wi511_key():
        parts.append("(511 snow/ice data off - no key)")
    msg = "\n".join(parts)
    return msg if len(msg) <= limit else msg[: limit - 3] + "..."


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check if a Wisconsin drive is safe right now.")
    ap.add_argument("origin", help="start: address, place name, shortcut (waukesha, racine, fox lake) or 'lat,lon'")
    ap.add_argument("destination", help="destination, same formats")
    ap.add_argument("--sms", action="store_true", help="print a short text-message version")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    ap.add_argument("--hours", type=int, default=3, help="forecast hours to consider (default 3)")
    ap.add_argument("--via", action="append", default=[], metavar="PLACE",
                    help="force the route through this point (repeatable, in order); "
                         "use it to match the roads you actually drive")
    args = ap.parse_args(argv)

    rep = run_check(args.origin, args.destination, args.hours, args.via)
    if args.json:
        print(json.dumps({
            "verdict": rep.verdict, "origin": rep.origin.__dict__, "destination": rep.dest.__dict__,
            "miles": rep.route.miles if rep.route else None,
            "minutes": rep.route.minutes if rep.route else None,
            "roads": rep.route.road_labels if rep.route else [],
            "findings": [f.__dict__ for f in _sorted(rep.findings)],
            "notes": rep.notes, "checked_at": rep.checked_at.isoformat(),
            "sms": format_sms(rep),
        }, indent=2))
    elif args.sms:
        print(format_sms(rep))
    else:
        print(format_full(rep))
    return {"GOOD": 0, "CAUTION": 1, "BAD": 2}[rep.verdict]


if __name__ == "__main__":
    sys.exit(main())
