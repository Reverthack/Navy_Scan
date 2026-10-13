"""Réception des messages AIS (JSON, une ligne par message) et registre des navires.

Le flux provient typiquement d'AIS-catcher (RTL-SDR) configuré en sortie JSON/UDP.
Vérifier les noms de champs produits par la version installée (voir README).
"""
import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone

log = logging.getLogger("ais")


class VesselRegistry:
    """Positions et données statiques par MMSI. Thread-safe.

    Les données statiques (nom, type) arrivent dans des messages sans position :
    elles sont stockées à part et fusionnées à la lecture. Un navire sans position
    n'est jamais exposé, pour ne pas casser la corrélation.
    """

    def __init__(self):
        self._positions: dict = {}
        self._static: dict = {}
        self._lock = threading.Lock()
        # État de l'écoute UDP, exposé dans /api/status pour diagnostiquer « pas de navire ».
        self._listen = {
            "state": "desactive",  # desactive | ecoute | erreur
            "udp_port": None,
            "error": None,
            "messages": 0,
            "last_message_at": None,
        }

    def set_listening(self, port: int) -> None:
        with self._lock:
            self._listen.update(state="ecoute", udp_port=port, error=None)

    def set_listen_error(self, port: int, error: str) -> None:
        with self._lock:
            self._listen.update(state="erreur", udp_port=port, error=error)

    def note_message(self) -> None:
        with self._lock:
            self._listen["messages"] += 1
            self._listen["last_message_at"] = datetime.now(timezone.utc).isoformat()

    def listen_status(self) -> dict:
        with self._lock:
            return dict(self._listen)

    def update_position(self, mmsi: int, lat: float, lon: float, ts=None) -> None:
        with self._lock:
            self._positions[mmsi] = {
                "mmsi": mmsi,
                "lat": lat,
                "lon": lon,
                "last_seen": ts or datetime.now(timezone.utc),
            }

    def update_static(self, mmsi: int, name=None, ship_type=None) -> None:
        with self._lock:
            entry = self._static.setdefault(mmsi, {})
            if name:
                entry["name"] = name
            if ship_type is not None:
                entry["ship_type"] = ship_type

    def snapshot(self) -> list:
        with self._lock:
            return [
                {**pos, **self._static.get(mmsi, {})}
                for mmsi, pos in self._positions.items()
            ]


def ingest_line(line: str, registry: VesselRegistry) -> bool:
    """Traite une ligne JSON. Retourne True si un message exploitable a été enregistré."""
    try:
        msg = json.loads(line)
    except ValueError:
        return False
    if not isinstance(msg, dict):
        return False
    registry.note_message()

    mmsi = msg.get("mmsi")
    if mmsi is None:
        return False
    mmsi = int(mmsi)

    name = (msg.get("shipname") or "").strip() or None
    ship_type = msg.get("ship_type")
    if name or ship_type is not None:
        registry.update_static(mmsi, name=name, ship_type=ship_type)

    lat, lon = msg.get("lat"), msg.get("lon")
    if lat is None or lon is None:
        return bool(name or ship_type is not None)
    registry.update_position(mmsi, float(lat), float(lon))
    return True


def udp_listener(host: str, port: int, registry: VesselRegistry, stop_event: threading.Event):
    """Boucle d'écoute UDP, à lancer dans un thread daemon."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # SO_REUSEADDR sous Windows permet à un autre processus de voler le port : on ne l'active
    # que hors Windows.
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        log.error("Impossible d'écouter l'AIS sur %s:%s (%s). Le port est peut-être déjà utilisé.",
                  host, port, exc)
        registry.set_listen_error(port, str(exc))
        sock.close()
        return
    sock.settimeout(1.0)
    registry.set_listening(port)
    log.info("Écoute AIS UDP sur %s:%s", host, port)
    try:
        while not stop_event.is_set():
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            for line in data.decode("utf-8", errors="ignore").splitlines():
                ingest_line(line, registry)
    finally:
        sock.close()
