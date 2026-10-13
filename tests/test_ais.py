import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hub"))

from ais import VesselRegistry, ingest_line  # noqa: E402


class IngestTests(unittest.TestCase):
    def test_position_message(self):
        reg = VesselRegistry()
        ok = ingest_line('{"mmsi": 227006760, "lat": 48.39, "lon": -4.49}', reg)
        self.assertTrue(ok)
        self.assertEqual(reg.snapshot()[0]["lat"], 48.39)

    def test_static_name_is_merged_with_position(self):
        reg = VesselRegistry()
        ingest_line('{"mmsi": 1, "shipname": "CAP SIZUN  ", "ship_type": 37}', reg)
        self.assertEqual(reg.snapshot(), [])  # pas de position : non exposé
        ingest_line('{"mmsi": 1, "lat": 48.39, "lon": -4.49}', reg)
        vessel = reg.snapshot()[0]
        self.assertEqual(vessel["name"], "CAP SIZUN")
        self.assertEqual(vessel["ship_type"], 37)

    def test_listen_status_counts_messages(self):
        reg = VesselRegistry()
        self.assertEqual(reg.listen_status()["state"], "desactive")
        reg.set_listening(10110)
        ingest_line('{"mmsi": 1, "lat": 48.39, "lon": -4.49}', reg)
        ingest_line("pas du json", reg)  # non compté
        status = reg.listen_status()
        self.assertEqual(status["state"], "ecoute")
        self.assertEqual(status["messages"], 1)
        self.assertIsNotNone(status["last_message_at"])

    def test_listen_error_is_reported(self):
        reg = VesselRegistry()
        reg.set_listen_error(10110, "port déjà utilisé")
        status = reg.listen_status()
        self.assertEqual(status["state"], "erreur")
        self.assertEqual(status["error"], "port déjà utilisé")

    def test_invalid_lines_are_ignored(self):
        reg = VesselRegistry()
        self.assertFalse(ingest_line("pas du json", reg))
        self.assertFalse(ingest_line('{"lat": 48.1, "lon": -4.1}', reg))
        self.assertEqual(reg.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
