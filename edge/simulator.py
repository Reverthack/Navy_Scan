"""Simulateur de scénarios : aucune caméra ni antenne AIS requise.

Scénarios :
- intrusion : une embarcation ou un humain approche la zone restreinte sans navire AIS.
              Attendu : alarme critique, commande PTZ, vignette.
- navire    : un navire AIS suit la même trajectoire que la cible.
              Attendu : alarme « navire_identifie » (info).

Usage :
  python edge/simulator.py --hub http://127.0.0.1:8000 --scenario intrusion
"""
import argparse
import io
import json
import logging
import math
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("simulator")
M_PER_DEG_LAT = 111_320


def to_latlon(site, x_east: float, y_north: float):
    lat = site["lat"] + y_north / M_PER_DEG_LAT
    lon = site["lon"] + x_east / (M_PER_DEG_LAT * math.cos(math.radians(site["lat"])))
    return lat, lon


def to_xy(site, lat: float, lon: float):
    y = (lat - site["lat"]) * M_PER_DEG_LAT
    x = (lon - site["lon"]) * M_PER_DEG_LAT * math.cos(math.radians(site["lat"]))
    return x, y


def zone_target_xy(site, zone):
    pts = zone["polygon"]
    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    return to_xy(site, lat, lon)


def fake_frame_jpeg(label: str, cx: int = 320, cy: int = 256) -> bytes:
    """Image thermique factice (bruit + cible centrale). Nécessite Pillow."""
    from PIL import Image, ImageDraw

    img = Image.effect_noise((640, 512), 12).convert("L")
    draw = ImageDraw.Draw(img)
    draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=235)
    draw.text((12, 12), label, fill=255)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return buf.getvalue()


def send_ais(sock, port: int, mmsi: int, name: str, lat: float, lon: float) -> None:
    msgs = [
        {"mmsi": mmsi, "shipname": name, "ship_type": 37},
        {"mmsi": mmsi, "lat": round(lat, 6), "lon": round(lon, 6), "speed": 6.0},
    ]
    payload = "\n".join(json.dumps(m) for m in msgs).encode()
    sock.sendto(payload, ("127.0.0.1", port))


def run(args) -> None:
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    site = config["sites"][args.site_id]
    zone = site["zones"][0]
    api_key = os.environ["CS_API_KEY"]

    tx, ty = zone_target_xy(site, zone)
    bearing = math.radians(args.bearing)
    x0 = args.start_range * math.sin(bearing)
    y0 = args.start_range * math.cos(bearing)
    dist_total = math.hypot(tx - x0, ty - y0)
    if args.speed <= 0 or args.interval <= 0:
        raise SystemExit("--speed et --interval doivent être > 0")
    steps = max(1, int(dist_total / (args.speed * args.interval)))

    ais_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    track_id = args.track_id
    classe = "humain" if args.speed < 3 else "embarcation"
    log.info("Scénario %s : %s à %.1f m/s, %d pas", args.scenario, classe, args.speed, steps)

    t0 = datetime.now(timezone.utc)
    with httpx.Client(timeout=5) as client:
        for step in range(steps + 1):
            frac = min(1.0, step / steps)
            x = x0 + (tx - x0) * frac
            y = y0 + (ty - y0) * frac
            lat, lon = to_latlon(site, x, y)
            # Horodatage simulé : cohérent avec la vitesse, même sans attente réelle.
            now = (t0 + timedelta(seconds=step * args.interval)).isoformat()

            if args.scenario == "navire" and args.ais_port:
                send_ais(ais_sock, args.ais_port, 227006760, "SIMU NAVIRE", lat, lon)

            event = {
                "site_id": args.site_id,
                "camera_id": args.camera_id,
                "timestamp": now,
                "track_id": track_id,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "classe": classe,
            }
            resp = client.post(f"{args.hub}/api/events", json=event, headers={"X-API-Key": api_key})
            resp.raise_for_status()
            alarm = resp.json().get("alarm")

            if alarm:
                log.warning("Alarme %s (%s) id=%s", alarm["kind"], alarm["severity"], alarm.get("id"))
                if alarm.get("id") is not None:
                    try:
                        frame = fake_frame_jpeg(f"{alarm['kind']} #{alarm['id']}")
                        client.post(
                            f"{args.hub}/api/alarms/{alarm['id']}/snapshot",
                            content=frame,
                            headers={"X-API-Key": api_key, "Content-Type": "image/jpeg"},
                        ).raise_for_status()
                    except ImportError:
                        log.info("Pillow absent : pas de vignette simulée")

            time.sleep(args.interval if args.realtime else 0)

    ais_sock.close()
    log.info("Fin du scénario")


def main() -> None:
    p = argparse.ArgumentParser(description="Simulateur de scénarios de surveillance")
    p.add_argument("--hub", default="http://127.0.0.1:8000")
    p.add_argument("--scenario", choices=["intrusion", "navire"], default="intrusion")
    p.add_argument("--site-id", default="site-demo")
    p.add_argument("--camera-id", default="cam-sim")
    p.add_argument("--track-id", type=int, default=int(time.time()) % 100000)
    p.add_argument("--bearing", type=float, default=225, help="cap de départ depuis le site (°)")
    p.add_argument("--start-range", type=float, default=1200, help="distance de départ (m)")
    p.add_argument("--speed", type=float, default=1.5, help="vitesse en m/s (<3 = humain)")
    p.add_argument("--interval", type=float, default=1.0, help="intervalle entre détections (s)")
    p.add_argument("--ais-port", type=int, default=10110, help="0 pour désactiver l'envoi AIS")
    p.add_argument("--realtime", action="store_true", help="attendre réellement entre les détections")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args)


if __name__ == "__main__":
    main()
