"""Offline tests for the 511WI parsing and verdict logic (no network, no key).

Run:  python3 -m unittest test_road_alerts.py
"""

import unittest
from unittest import mock

import road_alerts as ra


def encode_polyline(points):
    """Google polyline encoder, used to build fake 511 segments."""
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        for v, prev in ((round(lat * 1e5), plat), (round(lon * 1e5), plon)):
            d = v - prev
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        plat, plon = round(lat * 1e5), round(lon * 1e5)
    return "".join(out)


# A straight east-west "I-94" route between Waukesha and Milwaukee.
ROUTE_PTS = [(43.03, -88.25), (43.03, -87.95)]
ROUTE = ra.Route(ROUTE_PTS, 15, 20, {"I-94"}, ["I-94"])
CORR = ra.Corridor(ROUTE_PTS)
O = ra.Place("Waukesha, WI", 43.03, -88.25)
D = ra.Place("Milwaukee, WI", 43.03, -87.95)


class PolylineTests(unittest.TestCase):
    def test_round_trip(self):
        pts = [(43.03, -88.25), (43.04, -88.10), (43.05, -87.95)]
        got = ra.decode_polyline(encode_polyline(pts))
        for a, b in zip(pts, got):
            self.assertAlmostEqual(a[0], b[0], places=4)
            self.assertAlmostEqual(a[1], b[1], places=4)

    def test_docs_sample_decodes_inside_wisconsin(self):
        sample = "c~nhGvhawO_Hs@yG{@{GmA{GeBuGuBeYkJkP}FoGkCqPaF{G}A_H_AcHa@cQdAcHMyPuDcPyG"
        pts = ra.decode_polyline(sample)
        self.assertTrue(pts)
        self.assertTrue(all(42 < p[0] < 47 and -93 < p[1] < -86 for p in pts))


class WinterRoadTests(unittest.TestCase):
    def _run(self, cond, surface="", pts=None):
        rep = ra.Report(O, D, ROUTE)
        seg = {"Id": 1, "LocationDescription": "between WIS 16 and US 18",
               "Overall Condition": cond, "AreaName": "WAUKESHA", "RoadwayName": "I-94",
               "EncodedPolyline": [encode_polyline(pts or [(43.03, -88.20), (43.03, -88.10)])],
               "LastUpdated": 0, "RoadSurface": surface}
        with mock.patch.object(ra, "get_json", return_value=[seg]):
            ra.check_winter_roads(CORR, ROUTE, rep, "fake")
        return rep

    def test_completely_covered_is_bad(self):
        rep = self._run("Completely Covered", "Snow")
        self.assertEqual(rep.verdict, "BAD")
        self.assertIn("I-94", rep.findings[0].title)

    def test_travel_not_advised_is_bad(self):
        self.assertEqual(self._run("Travel Not Advised").verdict, "BAD")

    def test_partially_covered_is_caution(self):
        self.assertEqual(self._run("Partially Covered").verdict, "CAUTION")

    def test_normal_is_good(self):
        self.assertEqual(self._run("Normal", "Dry").verdict, "GOOD")

    def test_segment_off_route_ignored(self):
        rep = self._run("Completely Covered", pts=[(44.5, -89.5), (44.6, -89.4)])
        self.assertEqual(rep.findings, [])


class EventTests(unittest.TestCase):
    def _run(self, **kw):
        ev = {"ID": 1, "RoadwayName": "I-94", "Description": "Crash", "Latitude": 43.0301,
              "Longitude": -88.15, "EventType": "accidentsAndIncidents",
              "EventSubType": "crash", "IsFullClosure": False, "EncodedPolyline": None}
        ev.update(kw)
        rep = ra.Report(O, D, ROUTE)
        with mock.patch.object(ra, "get_json", return_value=[ev]):
            ra.check_511_events(CORR, ROUTE, rep, "fake")
        return rep

    def test_crash_on_route_is_caution(self):
        self.assertEqual(self._run().verdict, "CAUTION")

    def test_full_closure_on_route_is_bad(self):
        rep = self._run(EventType="closures", IsFullClosure=True)
        self.assertEqual(rep.verdict, "BAD")

    def test_crash_on_cross_street_is_not_on_route(self):
        rep = self._run(RoadwayName="Barker Rd")
        self.assertEqual(rep.verdict, "GOOD")

    def test_far_away_event_ignored(self):
        self.assertEqual(self._run(Latitude=44.9, Longitude=-91.0).findings, [])


class RoadKeyTests(unittest.TestCase):
    def test_variants_match(self):
        self.assertEqual(ra.road_keys("I-94 EB"), {"I-94"})
        self.assertIn("I-94", ra.road_keys("I 41; I 94; US 41"))
        self.assertEqual(ra.road_keys("STH 59"), ra.road_keys("WI 59"))
        self.assertEqual(ra.road_keys("County Road K"), {"CTH-K"})


def _obs(desc, temp_c, weather=None):
    return {"textDescription": desc, "temperature": {"value": temp_c},
            "presentWeather": [{"weather": w} for w in (weather or [])]}


class BlackIceTests(unittest.TestCase):
    def test_rain_then_freezing_is_caution(self):
        obs = [_obs("Cloudy", -1), _obs("Light Rain", 2, ["rain"])]
        f = ra.assess_recent_weather("Home", obs, 30)
        self.assertEqual(f.level, "CAUTION")
        self.assertIn("Black ice", f.title)

    def test_rain_but_warm_is_fine(self):
        obs = [_obs("Cloudy", 6), _obs("Rain", 7)]
        self.assertIsNone(ra.assess_recent_weather("Home", obs, 40))

    def test_warm_now_but_freezing_at_departure(self):
        obs = [_obs("Cloudy", 3), _obs("Rain", 4)]
        self.assertEqual(ra.assess_recent_weather("Home", obs, 31).level, "CAUTION")

    def test_freezing_rain_is_bad(self):
        obs = [_obs("Freezing Rain", -2, ["freezing_rain"])]
        self.assertEqual(ra.assess_recent_weather("Home", obs, 30).level, "BAD")

    def test_recent_snow_and_cold(self):
        obs = [_obs("Cloudy", -4), _obs("Light Snow", -3, ["snow"])]
        self.assertIn("snow", ra.assess_recent_weather("Home", obs, 25).title.lower())

    def test_dry_is_fine(self):
        self.assertIsNone(ra.assess_recent_weather("Home", [_obs("Clear", -10)], -5))


class DepartTests(unittest.TestCase):
    NOW = ra.datetime(2026, 11, 28, 19, 40, tzinfo=ra.CENTRAL)  # Saturday 7:40pm

    def test_sunday_morning_from_saturday_night(self):
        d = ra.parse_depart("sun 7am", self.NOW)
        self.assertEqual((d.weekday(), d.hour, d.day), (6, 7, 29))

    def test_plain_time_rolls_to_tomorrow(self):
        d = ra.parse_depart("07:00", self.NOW)
        self.assertEqual((d.day, d.hour), (29, 7))

    def test_pm(self):
        self.assertEqual(ra.parse_depart("sun 3pm", self.NOW).hour, 15)

    def test_alert_ending_before_departure_skipped(self):
        rep = ra.Report(O, D, ROUTE, depart=ra.parse_depart("sun 7am", self.NOW))
        alert = {"features": [{"properties": {
            "id": "x", "event": "Winter Storm Warning", "ends": "2026-11-28T22:00:00-06:00",
            "expires": None, "description": "", "headline": "h"}}]}
        with mock.patch.object(ra, "get_json", return_value=alert):
            ra.check_nws_alerts(CORR, rep)
        self.assertEqual(rep.verdict, "GOOD")

    def test_alert_lasting_past_departure_counts(self):
        rep = ra.Report(O, D, ROUTE, depart=ra.parse_depart("sun 7am", self.NOW))
        alert = {"features": [{"properties": {
            "id": "x", "event": "Winter Storm Warning", "ends": "2026-11-29T12:00:00-06:00",
            "expires": None, "description": "", "headline": "h"}}]}
        with mock.patch.object(ra, "get_json", return_value=alert):
            ra.check_nws_alerts(CORR, rep)
        self.assertEqual(rep.verdict, "BAD")


if __name__ == "__main__":
    unittest.main()
