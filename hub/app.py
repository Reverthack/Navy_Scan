"""Hub central : reçoit les détections, applique les règles, expose l'API et l'interface."""
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ais import VesselRegistry, udp_listener
from ptz import build_ptz
from rules import Confirmer, Cooldown, TrackHistory, evaluate
from store import Store

VERSION = "0.4.0"
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png"}
CAMERA_ONLINE_S = 300  # une caméra est « en ligne » si elle a émis il y a moins de 5 min

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hub")

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Charge un fichier .env (CLE=valeur) sans dépendance. N'écrase pas l'environnement."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

CONFIG = json.loads(
    Path(os.environ.get("CS_CONFIG", ROOT / "config.json")).read_text(encoding="utf-8")
)
API_KEY = os.environ.get("CS_API_KEY")
if not API_KEY:
    raise RuntimeError("Définir CS_API_KEY dans le fichier .env (voir README, section Installation)")

RULES = CONFIG["rules"]
SNAPSHOT_MAX = CONFIG.get("hub", {}).get("snapshot_max_bytes", 2_000_000)

store = Store(os.environ.get("CS_DB", str(ROOT / "data" / "hub.db")))
vessels = VesselRegistry()
confirmer = Confirmer(RULES["confirm_hits"], RULES["confirm_window_s"])
cooldown = Cooldown(RULES.get("cooldown_s", 60))
history = TrackHistory()
ptz = build_ptz(CONFIG.get("ptz", {}))
_stop_ais = threading.Event()


def _seed_cameras() -> None:
    """Première exécution : importe les caméras de config.json. Ensuite, la base fait foi."""
    if store.count("cameras") > 0:
        return
    for cam in CONFIG.get("cameras", []):
        store.create_camera(cam)
    if CONFIG.get("cameras"):
        log.info("%d caméra(s) importée(s) depuis config.json", len(CONFIG["cameras"]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _seed_cameras()
    ais = CONFIG["ais"]
    # CS_AIS=0 désactive l'écoute UDP (utilisé par le test de bout en bout).
    if ais.get("enabled") and os.environ.get("CS_AIS", "1") != "0":
        threading.Thread(
            target=udp_listener,
            args=(ais["udp_host"], ais["udp_port"], vessels, _stop_ais),
            daemon=True,
        ).start()
    yield
    _stop_ais.set()


app = FastAPI(title="Surveillance côtière — hub", version=VERSION, lifespan=lifespan)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class DetectionEvent(BaseModel):
    site_id: str
    camera_id: str
    timestamp: datetime
    track_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    bearing_deg: float | None = None
    range_m: float | None = None
    classe: str | None = None


class CameraIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    site_id: str
    name: str = Field(min_length=1, max_length=80)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    kind: str = Field(default="thermique_panoramique", max_length=40)
    zone_ids: list[str] = Field(default_factory=list)
    ptz: bool = False
    enabled: bool = True


def _check_key(x_api_key: str | None) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="clé API invalide")


def _validate_camera(cam: CameraIn) -> None:
    """Le site doit exister, et les zones couvertes doivent appartenir à ce site."""
    site = CONFIG["sites"].get(cam.site_id)
    if site is None:
        raise HTTPException(status_code=422, detail=f"site inconnu : {cam.site_id}")
    valid = {z["id"] for z in site["zones"]}
    unknown = [z for z in cam.zone_ids if z not in valid]
    if unknown:
        raise HTTPException(status_code=422, detail=f"zones inconnues pour ce site : {', '.join(unknown)}")


def _camera_status(cam: dict) -> str:
    if not cam["enabled"]:
        return "desactivee"
    if not cam["last_event_at"]:
        return "silencieuse"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(cam["last_event_at"])
    return "en_ligne" if age < timedelta(seconds=CAMERA_ONLINE_S) else "hors_ligne"


def _camera_view(cam: dict) -> dict:
    return {**cam, "status": _camera_status(cam)}


def _resolve_map() -> dict:
    """Choisit le fond de carte. Une clé (ex. MAP_TILE_KEY) vient toujours de .env, jamais de config.json.

    provider = "auto" : CARTO si sa clé est définie, sinon OpenStreetMap.
    Si le fournisseur demandé exige une clé absente, on bascule sur OpenStreetMap.
    """
    cfg = CONFIG.get("map", {})
    providers = cfg.get("providers", {})
    choice = cfg.get("provider", "auto")
    order = ["carto", "osm"] if choice == "auto" else [choice, "osm"]

    for name in order:
        p = providers.get(name)
        if not p:
            continue
        key_name = p.get("requires_key")
        key = os.environ.get(key_name, "") if key_name else ""
        if key_name and not key:
            continue
        url = p["tile_url"].replace("{MAP_TILE_KEY}", key)
        return {
            "provider": name,
            "tile_url": url,
            "attribution": p.get("attribution", ""),
            "max_zoom": p.get("max_zoom", 19),
            "dark": bool(p.get("dark", False)),
        }
    return {}


# ---------- Statut ----------
@app.get("/api/status")
def status():
    return {
        "ok": True,
        "version": VERSION,
        "ais_enabled": bool(CONFIG["ais"].get("enabled")),
        "ais": vessels.listen_status(),
        "ptz_enabled": bool(ptz.enabled),
        "sites": list(CONFIG["sites"].keys()),
        "map": _resolve_map(),
        "cameras": store.count("cameras"),
        "vessels": len(vessels.snapshot()),
        "events": store.count("events"),
        "alarms": store.count("alarms"),
        "time": datetime.now(timezone.utc).isoformat(),
    }


# ---------- Détections ----------
@app.post("/api/events")
def post_event(event: DetectionEvent, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    site = CONFIG["sites"].get(event.site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="site inconnu")

    cam = store.get_camera(event.camera_id)
    if cam is None:
        raise HTTPException(status_code=404,
                            detail=f"caméra inconnue : {event.camera_id} (à déclarer dans l'interface)")
    if cam["site_id"] != event.site_id:
        raise HTTPException(status_code=422, detail="caméra rattachée à un autre site")
    if not cam["enabled"]:
        raise HTTPException(status_code=403, detail="caméra désactivée")

    ts = event.timestamp if event.timestamp.tzinfo else event.timestamp.replace(tzinfo=timezone.utc)
    data = event.model_dump()
    data["timestamp"] = ts.isoformat()
    store.add_event(data)
    store.touch_camera(cam["id"], ts.isoformat())

    # Seules les zones couvertes par cette caméra sont évaluées.
    zones = [z for z in site["zones"] if not cam["zone_ids"] or z["id"] in cam["zone_ids"]]
    if not zones:
        return {"alarm": None, "reason": "aucune zone couverte par cette caméra"}
    site_view = {**site, "zones": zones}

    alarm = evaluate(
        data,
        site=site_view,
        vessels=vessels.snapshot(),
        confirmer=confirmer,
        cooldown=cooldown,
        history=history,
        rules=RULES,
        ais=CONFIG["ais"],
        now=ts,
    )
    if alarm is not None:
        alarm["id"] = store.add_alarm(alarm)
        log.warning("Alarme %s (%s) zone=%s flags=%s",
                    alarm["kind"], alarm["severity"], alarm["zone"], alarm["flags"])
        if alarm["severity"] == "critique" and ptz.enabled and cam["ptz"]:
            alarm["ptz"] = ptz.slew_to_cue(cam["id"], alarm["lat"], alarm["lon"], alarm["id"])
    return {"alarm": alarm}


# ---------- Caméras ----------
@app.get("/api/cameras")
def list_cameras():
    return [_camera_view(c) for c in store.list_cameras()]


@app.post("/api/cameras", status_code=201)
def create_camera(cam: CameraIn, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    _validate_camera(cam)
    if not store.create_camera(cam.model_dump()):
        raise HTTPException(status_code=409, detail=f"la caméra {cam.id} existe déjà")
    return _camera_view(store.get_camera(cam.id))


@app.put("/api/cameras/{camera_id}")
def update_camera(camera_id: str, cam: CameraIn, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    if cam.id != camera_id:
        raise HTTPException(status_code=422, detail="l'identifiant ne se modifie pas")
    _validate_camera(cam)
    if not store.update_camera(camera_id, cam.model_dump()):
        raise HTTPException(status_code=404, detail="caméra inconnue")
    return _camera_view(store.get_camera(camera_id))


@app.delete("/api/cameras/{camera_id}")
def delete_camera(camera_id: str, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    if not store.delete_camera(camera_id):
        raise HTTPException(status_code=404, detail="caméra inconnue")
    return {"ok": True}


# ---------- Alarmes ----------
@app.get("/api/alarms")
def list_alarms(limit: int = 100):
    return store.list_alarms(limit)


@app.post("/api/alarms/{alarm_id}/ack")
def ack_alarm(alarm_id: int, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    if not store.ack_alarm(alarm_id):
        raise HTTPException(status_code=404, detail="alarme inconnue")
    return {"ok": True}


@app.post("/api/alarms/{alarm_id}/snapshot")
async def upload_snapshot(alarm_id: int, request: Request, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    if not store.alarm_exists(alarm_id):
        raise HTTPException(status_code=404, detail="alarme inconnue")
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="image/jpeg ou image/png attendu")
    body = await request.body()
    if not body or len(body) > SNAPSHOT_MAX:
        raise HTTPException(status_code=413, detail="taille d'image invalide")
    store.save_snapshot(alarm_id, ctype, body)
    return {"ok": True, "bytes": len(body)}


@app.get("/api/alarms/{alarm_id}/snapshot")
def get_snapshot(alarm_id: int):
    found = store.get_snapshot(alarm_id)
    if found is None:
        raise HTTPException(status_code=404, detail="pas de vignette")
    ctype, data = found
    return Response(content=data, media_type=ctype, headers={"Cache-Control": "max-age=3600"})


# ---------- Navires, sites, PTZ ----------
@app.get("/api/vessels")
def list_vessels():
    return [
        {**v, "last_seen": v["last_seen"].isoformat()} for v in vessels.snapshot()
    ]


@app.get("/api/sites")
def list_sites():
    return CONFIG["sites"]


@app.get("/api/ptz/commands")
def list_ptz_commands():
    return ptz.commands()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")
