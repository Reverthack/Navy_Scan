"""Persistance SQLite (événements, alarmes, vignettes, caméras)."""
import json
import sqlite3
import threading
from datetime import datetime, timezone


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    acked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    alarm_id INTEGER PRIMARY KEY,
                    content_type TEXT NOT NULL,
                    data BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cameras (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    kind TEXT NOT NULL,
                    zone_ids TEXT NOT NULL,
                    ptz INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_event_at TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    # ---------- Événements et alarmes ----------
    def add_event(self, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (received_at, payload) VALUES (?, ?)",
                (datetime.now(timezone.utc).isoformat(), json.dumps(payload)),
            )
            self._conn.commit()

    def add_alarm(self, alarm: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alarms (created_at, severity, kind, payload) VALUES (?, ?, ?, ?)",
                (alarm["created_at"], alarm["severity"], alarm["kind"], json.dumps(alarm)),
            )
            self._conn.commit()
            return cur.lastrowid

    def alarm_exists(self, alarm_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
        return row is not None

    def list_alarms(self, limit: int = 100) -> list:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT a.id, a.acked, a.payload,
                       EXISTS (SELECT 1 FROM snapshots s WHERE s.alarm_id = a.id) AS has_snap
                FROM alarms a ORDER BY a.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {**json.loads(r["payload"]), "id": r["id"], "acked": bool(r["acked"]),
             "has_snapshot": bool(r["has_snap"])}
            for r in rows
        ]

    def ack_alarm(self, alarm_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE alarms SET acked = 1 WHERE id = ?", (alarm_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def save_snapshot(self, alarm_id: int, content_type: str, data: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshots (alarm_id, content_type, data, created_at) "
                "VALUES (?, ?, ?, ?)",
                (alarm_id, content_type, sqlite3.Binary(data), datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def get_snapshot(self, alarm_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT content_type, data FROM snapshots WHERE alarm_id = ?", (alarm_id,)
            ).fetchone()
        return (row["content_type"], bytes(row["data"])) if row else None

    def count(self, table: str) -> int:
        if table not in {"events", "alarms", "cameras"}:
            raise ValueError(table)
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---------- Caméras ----------
    @staticmethod
    def _cam_from_row(row) -> dict:
        return {
            "id": row["id"],
            "site_id": row["site_id"],
            "name": row["name"],
            "lat": row["lat"],
            "lon": row["lon"],
            "kind": row["kind"],
            "zone_ids": json.loads(row["zone_ids"]),
            "ptz": bool(row["ptz"]),
            "enabled": bool(row["enabled"]),
            "last_event_at": row["last_event_at"],
        }

    def list_cameras(self) -> list:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM cameras ORDER BY name, id").fetchall()
        return [self._cam_from_row(r) for r in rows]

    def get_camera(self, camera_id: str):
        with self._lock:
            row = self._conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
        return self._cam_from_row(row) if row else None

    def create_camera(self, cam: dict) -> bool:
        """Crée une caméra. Retourne False si l'identifiant existe déjà."""
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM cameras WHERE id = ?", (cam["id"],)).fetchone()
            if exists:
                return False
            self._write_camera(cam)
            return True

    def update_camera(self, camera_id: str, cam: dict) -> bool:
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM cameras WHERE id = ?", (camera_id,)).fetchone()
            if not exists:
                return False
            self._write_camera({**cam, "id": camera_id})
            return True

    def _write_camera(self, cam: dict) -> None:
        # appelé sous verrou
        self._conn.execute(
            """
            INSERT INTO cameras (id, site_id, name, lat, lon, kind, zone_ids, ptz, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                site_id = excluded.site_id, name = excluded.name,
                lat = excluded.lat, lon = excluded.lon, kind = excluded.kind,
                zone_ids = excluded.zone_ids, ptz = excluded.ptz,
                enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (cam["id"], cam["site_id"], cam["name"], cam["lat"], cam["lon"], cam["kind"],
             json.dumps(cam.get("zone_ids", [])), int(cam.get("ptz", False)),
             int(cam.get("enabled", True)), datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def delete_camera(self, camera_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def touch_camera(self, camera_id: str, ts_iso: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE cameras SET last_event_at = ? WHERE id = ?", (ts_iso, camera_id))
            self._conn.commit()
