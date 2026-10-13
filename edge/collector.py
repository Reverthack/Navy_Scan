"""Collecteur d'un site : lit les détections d'une source et les envoie au hub.

Sources :
- ReplaySource  : rejoue un fichier JSONL (démo, tests, sans caméra)
- OnvifSource   : stub à implémenter une fois la caméra choisie
"""
import abc
import argparse
import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("edge")


class EventSource(abc.ABC):
    @abc.abstractmethod
    def events(self):
        """Produit des dicts au format DetectionEvent (voir README)."""


class ReplaySource(EventSource):
    def __init__(self, path: str):
        self.path = Path(path)

    def events(self):
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


class OnvifSource(EventSource):
    """À implémenter : abonnement aux événements ONVIF de la caméra (pull point)
    ou lecture des métadonnées d'analytics, puis conversion en détections."""

    def __init__(self, host: str, user: str, password: str, camera_id: str, site_id: str):
        self.host, self.user, self.password = host, user, password
        self.camera_id, self.site_id = camera_id, site_id

    def events(self):
        raise NotImplementedError("Adaptateur ONVIF à implémenter après choix de la caméra")


def post_event(client: httpx.Client, hub_url: str, api_key: str, event: dict, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            resp = client.post(
                f"{hub_url}/api/events",
                json=event,
                headers={"X-API-Key": api_key},
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("Envoi échoué (%d/%d) : %s", attempt, retries, exc)
            time.sleep(2 ** attempt)
    log.error("Événement non transmis : %s", event)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Collecteur d'événements par site")
    parser.add_argument("--hub", default="http://127.0.0.1:8000", help="URL du hub")
    parser.add_argument("--replay", help="fichier JSONL à rejouer (sans caméra)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    api_key = os.environ["CS_API_KEY"]

    if args.replay:
        source = ReplaySource(args.replay)
    else:
        raise SystemExit("Aucune source : utiliser --replay (OnvifSource à implémenter)")

    with httpx.Client() as client:
        for event in source.events():
            result = post_event(client, args.hub, api_key, event)
            if result and result.get("alarm"):
                log.warning("ALARME : %s", result["alarm"])


if __name__ == "__main__":
    main()
