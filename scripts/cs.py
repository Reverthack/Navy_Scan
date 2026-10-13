#!/usr/bin/env python3
"""Commandes du prototype, multiplateforme (Windows, Linux, macOS).

Usage (depuis le dossier du projet) :
    py scripts\\cs.py install        # Windows
    python3 scripts/cs.py install    # Linux / macOS

Commandes :
    install       venv, dépendances, clé API (.env), tests
    run           lance le hub sur http://127.0.0.1:8000/
    simulate      scénario intrusion (humain, sans AIS) -> alarme critique
    simulate-nav  scénario navire AIS -> navire identifié
    test          tests unitaires et API
    smoke         test de bout en bout sur un hub temporaire
"""
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
VPY = VENV / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
ENV_FILE = ROOT / ".env"
HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:8000")


def load_env() -> dict:
    values = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def make_env(extra: dict | None = None) -> dict:
    env = {**load_env(), **os.environ}
    env["PYTHONUTF8"] = "1"  # évite les erreurs d'encodage sur la console Windows
    if extra:
        env.update(extra)
    return env


def need_venv() -> None:
    if not VPY.exists():
        sys.exit("Environnement absent : lancer d'abord la commande 'install'.")


def run(cmd, cwd=ROOT, env=None) -> None:
    print("> " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, env=env or make_env(), check=True)


# ---------- Commandes ----------
def cmd_install() -> None:
    if not VPY.exists():
        run([sys.executable, "-m", "venv", VENV])
    run([VPY, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    run([VPY, "-m", "pip", "install", "--quiet", "-r", ROOT / "requirements.txt"])
    # Côté site (numpy, pillow) : optionnel. Une politique de sécurité peut bloquer ces DLL ;
    # le hub fonctionne sans elles.
    edge = subprocess.run([str(VPY), "-m", "pip", "install", "--quiet", "-r",
                           str(ROOT / "requirements-edge.txt")], cwd=ROOT, env=make_env())
    if edge.returncode != 0:
        print("AVERTISSEMENT : dépendances côté site non installées (numpy/pillow).")
        print("  Le hub et l'interface fonctionnent. Le détecteur edge est indisponible.")
    (ROOT / "data").mkdir(exist_ok=True)

    if not ENV_FILE.exists():
        key = secrets.token_urlsafe(32)
        ENV_FILE.write_text(
            f"CS_API_KEY={key}\nCS_DB={ROOT / 'data' / 'hub.db'}\n", encoding="utf-8"
        )
        print("Clé API générée dans .env (ne pas la partager).")
    else:
        print(".env existant conservé.")

    cmd_test()
    print("\nInstallation terminée.")
    print("  py scripts\\cs.py run            (Windows)  ou  python3 scripts/cs.py run")
    print(f"  Interface : {HUB_URL}/")


def cmd_run() -> None:
    need_venv()
    print(f"Hub sur {HUB_URL}/ (Ctrl+C pour arrêter)")
    run([VPY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=ROOT / "hub")


def cmd_simulate(extra_args=()) -> None:
    need_venv()
    run([VPY, ROOT / "edge" / "simulator.py", "--hub", HUB_URL, *extra_args])


def cmd_test() -> None:
    need_venv()
    run([VPY, "-m", "unittest", "discover", "-s", "tests", "-v"])


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url: str):
    """GET JSON avec la bibliothèque standard (pas de dépendance dans ce script)."""
    import json
    import urllib.request

    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_smoke() -> None:
    need_venv()

    tmp = Path(tempfile.mkdtemp(prefix="cs-smoke-"))
    port = free_port()
    key = "smoke-" + secrets.token_hex(8)
    env = make_env({"CS_API_KEY": key, "CS_DB": str(tmp / "smoke.db"), "CS_AIS": "0"})
    base = f"http://127.0.0.1:{port}"

    print(f"==> Hub temporaire sur {base}")
    hub = subprocess.Popen(
        [str(VPY), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT / "hub", env=env,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                http_get(f"{base}/api/status")
                ready = True
                break
            except OSError:  # connexion refusée tant que le hub démarre
                time.sleep(0.5)
        if not ready:
            raise SystemExit("ECHEC : le hub ne répond pas")

        print("==> Scénario intrusion")
        run([VPY, ROOT / "edge" / "simulator.py", "--hub", base, "--scenario", "intrusion",
             "--speed", "1.5", "--interval", "1", "--ais-port", "0"], env=env)

        alarms = http_get(f"{base}/api/alarms")
        ptz = http_get(f"{base}/api/ptz/commands")
        n_alarms = len(alarms)
        n_crit = sum(1 for a in alarms if a["severity"] == "critique")
        n_snap = sum(1 for a in alarms if a["has_snapshot"])
        n_ptz = len(ptz)
        print(f"    alarmes={n_alarms} critiques={n_crit} vignettes={n_snap} commandes_ptz={n_ptz}")

        failures = []
        if n_alarms < 1:
            failures.append("aucune alarme")
        if n_crit < 1:
            failures.append("aucune alarme critique")
        if n_ptz < 1:
            failures.append("aucune commande PTZ")
        if failures:
            raise SystemExit("ECHEC : " + ", ".join(failures))
        print("SMOKE TEST OK")
    finally:
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()
        shutil.rmtree(tmp, ignore_errors=True)


COMMANDS = {
    "install": cmd_install,
    "run": cmd_run,
    "simulate": lambda: cmd_simulate(["--scenario", "intrusion"]),
    "simulate-nav": lambda: cmd_simulate(["--scenario", "navire", "--speed", "6", "--track-id", "2"]),
    "test": cmd_test,
    "smoke": cmd_smoke,
}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "help"
    if name not in COMMANDS:
        print(__doc__)
        sys.exit(0 if name == "help" else 1)
    COMMANDS[name]()


if __name__ == "__main__":
    main()
