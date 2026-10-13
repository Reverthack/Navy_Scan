"""Logique d'alarme : localisation, zone, confirmation, anti-rebond, vitesse et corrélation AIS.

Module sans dépendance réseau ni base de données, pour rester testable.
"""
from collections import deque
from datetime import datetime, timedelta

from geo import destination_point, haversine_m, point_in_polygon

HUMAN_CLASSES = {"humain"}


def locate_target(event: dict, site: dict):
    """Retourne (lat, lon) de la cible, ou None si elle n'est pas localisable."""
    if event.get("lat") is not None and event.get("lon") is not None:
        return float(event["lat"]), float(event["lon"])
    if event.get("bearing_deg") is not None and event.get("range_m") is not None:
        return destination_point(
            site["lat"], site["lon"], float(event["bearing_deg"]), float(event["range_m"])
        )
    return None


def find_zone(lat: float, lon: float, zones: list):
    for zone in zones:
        if point_in_polygon(lat, lon, zone["polygon"]):
            return zone
    return None


class Confirmer:
    """Exige `min_hits` détections d'une même piste dans une fenêtre glissante."""

    def __init__(self, min_hits: int, window_s: float):
        self.min_hits = min_hits
        self.window = timedelta(seconds=window_s)
        self._hits: dict = {}

    def confirm(self, key, ts: datetime) -> bool:
        hits = [t for t in self._hits.get(key, []) if t >= ts - self.window]
        hits.append(ts)
        self._hits[key] = hits
        return len(hits) >= self.min_hits


class Cooldown:
    """Évite de répéter la même alarme pendant `seconds` secondes."""

    def __init__(self, seconds: float):
        self.window = timedelta(seconds=seconds)
        self._last: dict = {}

    def ready(self, key, ts: datetime) -> bool:
        last = self._last.get(key)
        if last is not None and ts - last < self.window:
            return False
        self._last[key] = ts
        return True


class TrackHistory:
    """Dernières positions par piste, pour estimer la vitesse."""

    def __init__(self, maxlen: int = 10):
        self.maxlen = maxlen
        self._points: dict = {}

    def add(self, key, lat: float, lon: float, ts: datetime) -> None:
        self._points.setdefault(key, deque(maxlen=self.maxlen)).append((ts, lat, lon))

    def speed_mps(self, key):
        pts = self._points.get(key)
        if not pts or len(pts) < 2:
            return None
        t0, la0, lo0 = pts[0]
        t1, la1, lo1 = pts[-1]
        dt = (t1 - t0).total_seconds()
        if dt <= 0:
            return None
        return haversine_m(la0, lo0, la1, lo1) / dt


def match_vessel(lat, lon, vessels, now, radius_m, max_age_s):
    """Navire AIS le plus proche, vu récemment et dans le rayon. -> (vessel, distance)."""
    best, best_d = None, None
    for v in vessels:
        if (now - v["last_seen"]).total_seconds() > max_age_s:
            continue
        d = haversine_m(lat, lon, v["lat"], v["lon"])
        if d <= radius_m and (best_d is None or d < best_d):
            best, best_d = v, d
    return best, best_d


def evaluate(event: dict, *, site: dict, vessels: list, confirmer: Confirmer,
             cooldown: Cooldown, history: TrackHistory, rules: dict, ais: dict,
             now: datetime):
    """Retourne un dict d'alarme, ou None si rien à signaler."""
    pos = locate_target(event, site)
    if pos is None:
        return None
    lat, lon = pos

    zone = find_zone(lat, lon, site["zones"])
    if zone is None:
        return None

    track = (event["site_id"], event["camera_id"], event.get("track_id"))
    key = track + (zone["id"],)
    history.add(track, lat, lon, now)

    if not confirmer.confirm(key, now):
        return None

    speed = history.speed_mps(track)
    cls = event.get("classe")
    vessel, dist = match_vessel(
        lat, lon, vessels, now, ais["match_radius_m"], ais["max_age_s"]
    )

    flags = []
    if speed is not None and speed > rules["max_speed_mps"]:
        flags.append("vitesse_elevee")
    if cls in HUMAN_CLASSES:
        flags.append("humain")

    restricted = bool(zone.get("restricted"))
    if vessel is not None:
        kind = "navire_identifie"
        severity = "alerte" if flags else "info"
    else:
        kind = "intrusion_sans_ais"
        severity = "critique" if (restricted or "humain" in flags) else "alerte"

    if not cooldown.ready(key, now):
        return None

    alarm = {
        "site_id": event["site_id"],
        "zone_id": zone["id"],
        "zone": zone["name"],
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "camera_id": event["camera_id"],
        "track_id": event.get("track_id"),
        "created_at": now.isoformat(),
        "kind": kind,
        "severity": severity,
        "classe": cls,
        "vitesse_mps": round(speed, 2) if speed is not None else None,
        "flags": flags,
    }
    if vessel is not None:
        alarm.update(
            mmsi=vessel["mmsi"],
            vessel_name=vessel.get("name"),
            ship_type=vessel.get("ship_type"),
            distance_m=round(dist),
        )
    return alarm
