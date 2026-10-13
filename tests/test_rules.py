import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hub"))

from geo import destination_point, haversine_m, point_in_polygon  # noqa: E402
from rules import Confirmer, Cooldown, TrackHistory, evaluate  # noqa: E402

SITE = {
    "lat": 48.3905,
    "lon": -4.4861,
    "zones": [
        {
            "id": "zone-plage",
            "name": "Plage nord",
            "restricted": True,
            "polygon": [[48.3950, -4.4900], [48.3950, -4.4850], [48.3985, -4.4850], [48.3985, -4.4900]],
        }
    ],
}
RULES = {"confirm_hits": 3, "confirm_window_s": 10, "max_speed_mps": 12.0}
AIS = {"match_radius_m": 500, "max_age_s": 900}
T0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
INSIDE = (48.3960, -4.4870)  # à l'intérieur de la zone


class Harness:
    """État partagé d'un scénario, comme dans le hub."""

    def __init__(self, confirm_hits=3, cooldown_s=60):
        self.confirmer = Confirmer(confirm_hits, 10)
        self.cooldown = Cooldown(cooldown_s)
        self.history = TrackHistory()

    def run(self, ev, vessels=(), rules=RULES):
        now = datetime.fromisoformat(ev["timestamp"])
        return evaluate(ev, site=SITE, vessels=list(vessels), confirmer=self.confirmer,
                        cooldown=self.cooldown, history=self.history,
                        rules=rules, ais=AIS, now=now)


def pos_event(i, lat, lon, track=1, classe=None, step_s=1):
    ev = {"site_id": "site-demo", "camera_id": "cam-01", "track_id": track,
          "lat": lat, "lon": lon,
          "timestamp": (T0 + timedelta(seconds=i * step_s)).isoformat()}
    if classe:
        ev["classe"] = classe
    return ev


class GeoTests(unittest.TestCase):
    def test_haversine_one_degree_latitude(self):
        self.assertAlmostEqual(haversine_m(0, 0, 1, 0), 111_195, delta=50)

    def test_destination_point_distance_matches_range(self):
        lat, lon = destination_point(SITE["lat"], SITE["lon"], 37, 1234)
        self.assertAlmostEqual(haversine_m(SITE["lat"], SITE["lon"], lat, lon), 1234, delta=0.5)

    def test_point_in_polygon(self):
        poly = SITE["zones"][0]["polygon"]
        self.assertTrue(point_in_polygon(*INSIDE, poly))
        self.assertFalse(point_in_polygon(48.4000, -4.4870, poly))


class ConfirmerTests(unittest.TestCase):
    def test_confirmed_after_n_hits_within_window(self):
        c = Confirmer(3, 10)
        self.assertFalse(c.confirm("k", T0))
        self.assertFalse(c.confirm("k", T0 + timedelta(seconds=4)))
        self.assertTrue(c.confirm("k", T0 + timedelta(seconds=8)))

    def test_hits_outside_window_are_dropped(self):
        c = Confirmer(3, 10)
        c.confirm("k", T0)
        c.confirm("k", T0 + timedelta(seconds=1))
        self.assertFalse(c.confirm("k", T0 + timedelta(seconds=30)))


class CooldownTests(unittest.TestCase):
    def test_suppresses_repeats_within_window(self):
        cd = Cooldown(60)
        self.assertTrue(cd.ready("k", T0))
        self.assertFalse(cd.ready("k", T0 + timedelta(seconds=30)))
        self.assertTrue(cd.ready("k", T0 + timedelta(seconds=61)))


class TrackHistoryTests(unittest.TestCase):
    def test_speed_from_positions(self):
        h = TrackHistory()
        lat0, lon0 = INSIDE
        lat1, lon1 = destination_point(lat0, lon0, 0, 30)  # 30 m en 10 s = 3 m/s
        h.add("t", lat0, lon0, T0)
        h.add("t", lat1, lon1, T0 + timedelta(seconds=10))
        self.assertAlmostEqual(h.speed_mps("t"), 3.0, delta=0.05)

    def test_no_speed_with_single_point(self):
        h = TrackHistory()
        h.add("t", *INSIDE, T0)
        self.assertIsNone(h.speed_mps("t"))


class EvaluateTests(unittest.TestCase):
    def test_unidentified_target_in_restricted_zone_is_critical(self):
        h = Harness()
        alarm = None
        for i in range(3):
            alarm = h.run(pos_event(i, *INSIDE))
        self.assertEqual(alarm["kind"], "intrusion_sans_ais")
        self.assertEqual(alarm["severity"], "critique")

    def test_target_outside_zone_gives_nothing(self):
        h = Harness(confirm_hits=1)
        ev = pos_event(0, 48.4000, -4.4870)
        self.assertIsNone(h.run(ev))

    def test_nearby_ais_vessel_identifies_target(self):
        h = Harness(confirm_hits=1)
        vessel = {"mmsi": 123456789, "name": "TEST", "ship_type": 37,
                  "lat": INSIDE[0] + 0.0005, "lon": INSIDE[1], "last_seen": T0}
        alarm = h.run(pos_event(0, *INSIDE), vessels=[vessel])
        self.assertEqual(alarm["kind"], "navire_identifie")
        self.assertEqual(alarm["severity"], "info")
        self.assertEqual(alarm["mmsi"], 123456789)

    def test_stale_ais_vessel_is_ignored(self):
        h = Harness(confirm_hits=1)
        vessel = {"mmsi": 1, "name": None, "lat": INSIDE[0], "lon": INSIDE[1],
                  "last_seen": T0 - timedelta(hours=2)}
        alarm = h.run(pos_event(0, *INSIDE), vessels=[vessel])
        self.assertEqual(alarm["kind"], "intrusion_sans_ais")

    def test_cooldown_prevents_alarm_flood(self):
        h = Harness(confirm_hits=1, cooldown_s=60)
        first = h.run(pos_event(0, *INSIDE))
        second = h.run(pos_event(1, *INSIDE))
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_fast_track_gets_speed_flag(self):
        h = Harness(confirm_hits=1, cooldown_s=0)
        lat, lon = INSIDE
        lat2, lon2 = destination_point(lat, lon, 0, 100)  # 100 m en 1 s
        h.run(pos_event(0, lat, lon, track=5, classe="embarcation"))
        alarm = h.run(pos_event(1, lat2, lon2, track=5, classe="embarcation"))
        self.assertIn("vitesse_elevee", alarm["flags"])
        self.assertGreater(alarm["vitesse_mps"], 12.0)

    def test_human_class_is_flagged_and_critical(self):
        h = Harness(confirm_hits=1)
        alarm = h.run(pos_event(0, *INSIDE, classe="humain"))
        self.assertIn("humain", alarm["flags"])
        self.assertEqual(alarm["severity"], "critique")


if __name__ == "__main__":
    unittest.main()
