# Road alerts: is it safe to drive?

Give it where you are and where you're going. It builds the driving route and
checks everything along it, then says **GOOD**, **CAUTION** or **BAD** and why.

```
python3 road_alerts.py "Waukesha, WI" "Racine, WI"
python3 road_alerts.py waukesha racine --sms       # short version for a text
python3 road_alerts.py "43.01,-88.23" "Fox Lake, WI" --json
```

Use `--via PLACE` (repeatable) to force the route onto the roads you actually
drive, and `Name=lat,lon` to give a point a friendly name without putting an
address in the code:

```
python3 road_alerts.py "Home=43.03,-88.17" "Church=42.70,-87.84" --via 43.03,-88.18 --sms
```

Places can be an address, a town, `lat,lon`, or a shortcut:
`waukesha`, `racine`, `fox lake`, `milwaukee`, `brookfield`, `oconomowoc`,
`kenosha`, `madison`. Python 3.9+, standard library only.

Exit code: `0` GOOD, `1` CAUTION, `2` BAD (handy for scripts).

## What it checks

| Source | What | Key? |
|---|---|---|
| NWS `api.weather.gov` alerts | Winter Storm / Blizzard / Ice Storm warnings, advisories etc. at points every ~12 km along the route | No |
| NWS hourly forecast | Snow, freezing rain, precip near freezing at start and destination over the next 3 h | No |
| 511WI WZDx feed | Work zones, lane closures, full road closures | No |
| 511WI events | Crashes, incidents, closures | `WI511_KEY` |
| 511WI winter roads | Partially / Completely Covered, Travel Not Advised segments | `WI511_KEY` |

## How the verdict is decided

- **BAD:** NWS warning such as a Winter Storm, Blizzard or Ice Storm Warning; forecast of
  snow (60%+), heavy snow, or freezing rain/sleet; 511 says *Completely Covered*
  (snow-covered, i.e. not plowed yet) or *Travel Not Advised* on your route; or
  a road on your route is fully closed (511 events).
- **CAUTION:** advisories (Winter Weather, Wind Chill...); precipitation near
  freezing (icy spots); *Partially Covered* segments; a crash on your route; a
  full (non-ramp) work-zone closure on your route.
- **Heads-up only:** lane closures and flagging in work zones on your route,
  plus anything off-route nearby. These are listed but don't change the verdict.

"On your route" means within ~250 m of the route **and** on the same road
(e.g. `I-94`), so a crash on a cross street doesn't count.

## Limits

- No live congestion speeds. "Heavy traffic" is inferred from crashes and
  closures only.
- 511WI does not publish snowplow locations. *Completely Covered* is the
  closest signal for "not plowed yet". The ratings come from WisDOT's
  weather/pavement models, adjusted by staff using cameras.
- Routing and geocoding use the public OpenStreetMap services (OSRM demo server,
  Nominatim). They're fine for personal use, not for heavy traffic.
- Don't rely on this alone; check 511wi.gov before a risky drive.

## 511WI key

Request one at https://511wi.gov/developers/doc (needs a My 511WI account).
Then either `export WI511_KEY=...` or put it in `road-alerts/.wi511_key`
(git-ignored). Limit: 10 calls per minute; one trip check uses 2.

## Tests

```
python3 -m unittest test_road_alerts.py
```
