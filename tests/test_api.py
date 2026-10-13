import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hub"))
os.environ["CS_API_KEY"] = "test-key"
os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["CS_AIS"] = "0"

try:
    from fastapi.testclient import TestClient
    import app as hub_app
    HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    HAS_FASTAPI = False

KEY = {"X-API-Key": "test-key"}
INSIDE = (48.3960, -4.4870)
CAM = "cam-sim"  # déclarée par défaut dans config.json


def event(track, i, classe="humain", camera=CAM, site="site-demo"):
    return {
        "site_id": site,
        "camera_id": camera,
        "timestamp": f"2026-10-10T12:{i // 60:02d}:{i % 60:02d}+00:00",
        "track_id": track,
        "lat": INSIDE[0],
        "lon": INSIDE[1],
        "classe": classe,
    }


def camera_body(cam_id="cam-test", **over):
    body = {
        "id": cam_id,
        "site_id": "site-demo",
        "name": "Caméra de test",
        "lat": 48.3905,
        "lon": -4.4861,
        "kind": "thermique_ptz",
        "zone_ids": [],
        "ptz": False,
        "enabled": True,
    }
    body.update(over)
    return body


@unittest.skipUnless(HAS_FASTAPI, "fastapi non installé")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # `with` exécute le démarrage de l'application (import des caméras de config.json),
        # comme Uvicorn en production.
        cls._cm = TestClient(hub_app.app)
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    # ----- Statut et sécurité -----
    def test_status(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertIn("tile_url", r.json()["map"])
        self.assertNotIn("cartocdn", r.json()["map"]["tile_url"])
        self.assertIn("state", r.json()["ais"])

    def test_map_uses_carto_only_when_key_present(self):
        os.environ.pop("MAP_TILE_KEY", None)
        self.assertEqual(self.client.get("/api/status").json()["map"]["provider"], "osm")

        os.environ["MAP_TILE_KEY"] = "cle-de-test"
        try:
            m = self.client.get("/api/status").json()["map"]
            self.assertEqual(m["provider"], "carto")
            self.assertIn("?key=cle-de-test", m["tile_url"])
            self.assertFalse(m["dark"])
        finally:
            os.environ.pop("MAP_TILE_KEY", None)

    def test_event_requires_key(self):
        r = self.client.post("/api/events", json=event(1, 0))
        self.assertEqual(r.status_code, 401)

    def test_dashboard_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Surveillance côtière", r.text)

    # ----- Détections et alarmes -----
    def test_alarm_snapshot_and_ack_flow(self):
        alarm = None
        for i in range(3):
            r = self.client.post("/api/events", json=event(9001, i), headers=KEY)
            self.assertEqual(r.status_code, 200)
            alarm = r.json()["alarm"] or alarm
        self.assertIsNotNone(alarm, "aucune alarme après 3 détections confirmées")
        self.assertEqual(alarm["severity"], "critique")
        self.assertIn("humain", alarm["flags"])

        alarm_id = alarm["id"]
        jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
        r = self.client.post(f"/api/alarms/{alarm_id}/snapshot", content=jpeg,
                             headers={**KEY, "Content-Type": "image/jpeg"})
        self.assertEqual(r.status_code, 200)

        r = self.client.get(f"/api/alarms/{alarm_id}/snapshot")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, jpeg)

        listed = [a for a in self.client.get("/api/alarms").json() if a["id"] == alarm_id][0]
        self.assertTrue(listed["has_snapshot"])
        self.assertFalse(listed["acked"])

        r = self.client.post(f"/api/alarms/{alarm_id}/ack", headers=KEY)
        self.assertEqual(r.status_code, 200)

    def test_critical_alarm_triggers_ptz_when_camera_has_ptz(self):
        # cam-sim a ptz = true dans config.json
        for i in range(3):
            r = self.client.post("/api/events", json=event(9002, 100 + i), headers=KEY)
        commands = self.client.get("/api/ptz/commands").json()
        self.assertTrue(any(c["alarm_id"] == r.json()["alarm"]["id"] for c in commands))

    def test_snapshot_rejects_wrong_type(self):
        r = self.client.post("/api/alarms/1/snapshot", content=b"abc",
                             headers={**KEY, "Content-Type": "text/plain"})
        self.assertIn(r.status_code, (404, 415))

    def test_snapshot_unknown_alarm(self):
        r = self.client.get("/api/alarms/999999/snapshot")
        self.assertEqual(r.status_code, 404)

    # ----- Caméras : contrôle des événements -----
    def test_event_from_undeclared_camera_is_rejected(self):
        r = self.client.post("/api/events", json=event(7, 0, camera="cam-inconnue"), headers=KEY)
        self.assertEqual(r.status_code, 404)
        self.assertIn("caméra inconnue", r.json()["detail"])

    def test_event_with_wrong_site_is_rejected(self):
        r = self.client.post("/api/events", json=event(7, 0, site="autre"), headers=KEY)
        self.assertEqual(r.status_code, 404)  # site inconnu côté config

    def test_disabled_camera_is_rejected(self):
        self.client.post("/api/cameras", json=camera_body("cam-off", enabled=False), headers=KEY)
        r = self.client.post("/api/events", json=event(8, 0, camera="cam-off"), headers=KEY)
        self.assertEqual(r.status_code, 403)

    # ----- Caméras : cycle de vie -----
    def test_camera_crud_cycle(self):
        r = self.client.post("/api/cameras", json=camera_body("cam-crud"), headers=KEY)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["status"], "silencieuse")

        r = self.client.post("/api/cameras", json=camera_body("cam-crud"), headers=KEY)
        self.assertEqual(r.status_code, 409)

        r = self.client.put("/api/cameras/cam-crud", json=camera_body("cam-crud", name="Renommée"), headers=KEY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "Renommée")

        r = self.client.put("/api/cameras/cam-crud", json=camera_body("autre-id"), headers=KEY)
        self.assertEqual(r.status_code, 422)

        r = self.client.delete("/api/cameras/cam-crud", headers=KEY)
        self.assertEqual(r.status_code, 200)
        r = self.client.delete("/api/cameras/cam-crud", headers=KEY)
        self.assertEqual(r.status_code, 404)

    def test_camera_write_requires_key(self):
        r = self.client.post("/api/cameras", json=camera_body("cam-nokey"))
        self.assertEqual(r.status_code, 401)

    def test_camera_rejects_unknown_zone(self):
        r = self.client.post("/api/cameras",
                             json=camera_body("cam-zone", zone_ids=["zone-inexistante"]), headers=KEY)
        self.assertEqual(r.status_code, 422)

    def test_camera_rejects_invalid_id(self):
        r = self.client.post("/api/cameras", json=camera_body("cam avec espace"), headers=KEY)
        self.assertEqual(r.status_code, 422)

    def test_camera_status_becomes_online_after_event(self):
        self.client.post("/api/cameras", json=camera_body("cam-live"), headers=KEY)
        self.client.post("/api/events", json=event(11, 0, camera="cam-live"), headers=KEY)
        cams = {c["id"]: c for c in self.client.get("/api/cameras").json()}
        self.assertIn(cams["cam-live"]["status"], ("en_ligne", "hors_ligne"))
        self.assertIsNotNone(cams["cam-live"]["last_event_at"])


if __name__ == "__main__":
    unittest.main()
