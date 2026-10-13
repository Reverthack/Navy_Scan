"""Pilotage PTZ « slew-to-cue » : orienter une caméra PTZ vers une cible.

Backends :
- simulated : enregistre les commandes (aucun matériel requis, pour les tests).
- À ajouter : onvif (commande ContinuousMove / AbsoluteMove sur la caméra réelle).
"""
import logging
import threading
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("ptz")


class NullPtz:
    enabled = False

    def slew_to_cue(self, camera_id, lat, lon, alarm_id=None):
        return None

    def commands(self):
        return []


class SimulatedPtz:
    enabled = True

    def __init__(self, maxlen: int = 200):
        self._commands = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def slew_to_cue(self, camera_id, lat, lon, alarm_id=None):
        cmd = {
            "camera_id": camera_id,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "alarm_id": alarm_id,
            "backend": "simulated",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._commands.append(cmd)
        log.info("PTZ slew-to-cue (simulé) : %s", cmd)
        return cmd

    def commands(self):
        with self._lock:
            return list(reversed(self._commands))


def build_ptz(cfg: dict):
    if not cfg.get("enabled", False):
        return NullPtz()
    backend = cfg.get("backend", "simulated")
    if backend == "simulated":
        return SimulatedPtz()
    raise ValueError(f"backend PTZ inconnu : {backend}")
