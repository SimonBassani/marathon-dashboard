"""
SQLite-Zugriff und Schema.

Eine Datei, ein Prozess, ein Nutzer — deshalb reicht SQLite locker. WAL-Modus,
damit der Garmin-Sync schreiben kann, während das Dashboard liest.

Das Schema wird bei jedem Start idempotent angelegt (CREATE TABLE IF NOT EXISTS).
Migrationen laufen über `SCHEMA_STEPS` — jeder Eintrag wird genau einmal
ausgeführt und in `schema_version` vermerkt.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

DB_PATH = Path(os.environ.get("DB_PATH", "/data/marathon.db"))

_local = threading.local()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn() -> sqlite3.Connection:
    """Pro Thread eine Connection. SQLite-Connections sind nicht thread-safe."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Transaktion. Bei einer Exception wird zurückgerollt."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

SCHEMA_STEPS: list[str] = [
    # 1 — Tagesmetriken von Garmin (Schlaf, HRV, RHR, VO2max, Race-Prognosen ...)
    """
    CREATE TABLE IF NOT EXISTS health_metric (
        day        TEXT NOT NULL,          -- ISO-Datum
        kind       TEXT NOT NULL,
        value      REAL NOT NULL,
        meta       TEXT,                   -- JSON
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (day, kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_metric_kind_day ON health_metric(kind, day)",
    # 2 — Einheiten (Garmin-Aktivitäten und manuell erfasste)
    """
    CREATE TABLE IF NOT EXISTS workout (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        garmin_activity_id INTEGER UNIQUE,
        day                TEXT NOT NULL,
        start_time         TEXT NOT NULL,   -- ISO 8601 mit Offset
        sport              TEXT NOT NULL,   -- run | bike | swim | strength | other
        name               TEXT,
        duration_sec       INTEGER NOT NULL DEFAULT 0,
        distance_m         REAL,
        elevation_gain_m   REAL,
        avg_hr             REAL,
        max_hr             REAL,
        calories           REAL,
        training_load      REAL,
        aerobic_effect     REAL,
        anaerobic_effect   REAL,
        avg_cadence        REAL,
        hr_zones           TEXT,            -- JSON {zone1: sek, ...}
        rpe                INTEGER,         -- subjektive Anstrengung 1-10
        feeling            INTEGER,         -- Gefühl 1-10
        notes              TEXT,
        source             TEXT NOT NULL DEFAULT 'garmin',
        updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workout_day ON workout(day)",
    "CREATE INDEX IF NOT EXISTS idx_workout_sport_day ON workout(sport, day)",
    # 3 — Detaildaten je Einheit (Runden, HR-Kurve, Decoupling)
    """
    CREATE TABLE IF NOT EXISTS workout_detail (
        workout_id       INTEGER PRIMARY KEY REFERENCES workout(id) ON DELETE CASCADE,
        laps             TEXT,   -- JSON [{km, sec, avg_hr, ...}]
        hr_curve         TEXT,   -- JSON [{t_sec, hr}]
        pace_curve       TEXT,   -- JSON [{t_sec, pace_s_km}]
        decoupling_pct   REAL,   -- aerobe Entkopplung (Pa:Hr), Marathon-Kernmetrik
        pace_drift_pct   REAL,
        cadence_drift_pct REAL,
        fetched_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # 4 — Das Rennen (und optionale Vorbereitungswettkämpfe)
    """
    CREATE TABLE IF NOT EXISTS race (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        day           TEXT NOT NULL,
        distance_km   REAL NOT NULL DEFAULT 42.195,
        goal_time_sec INTEGER,
        priority      TEXT NOT NULL DEFAULT 'A',   -- A | B | C
        notes         TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # 5 — Geplante Einheiten. Das schreibt der Trainingsplan-Generator ODER Claude via MCP.
    """
    CREATE TABLE IF NOT EXISTS planned_session (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        day           TEXT NOT NULL,
        kind          TEXT NOT NULL,   -- easy | long | tempo | interval | recovery | race_pace | rest | strength | cross
        title         TEXT NOT NULL,
        distance_km   REAL,
        duration_min  INTEGER,
        target_pace_s INTEGER,         -- Sekunden pro km
        description   TEXT,
        status        TEXT NOT NULL DEFAULT 'planned',  -- planned | done | skipped | moved
        workout_id    INTEGER REFERENCES workout(id) ON DELETE SET NULL,
        source        TEXT NOT NULL DEFAULT 'generator', -- generator | claude | manual
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_planned_day ON planned_session(day)",
    # 6 — Notizen / Trainingstagebuch. Claude darf hier schreiben und lesen.
    """
    CREATE TABLE IF NOT EXISTS note (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        day        TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'journal',  -- journal | coach | injury
        text       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_note_day ON note(day)",
    # 7 — Sync-Protokoll, damit man sieht ob Garmin noch liefert
    """
    CREATE TABLE IF NOT EXISTS sync_run (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT,
        ok          INTEGER,
        written     INTEGER NOT NULL DEFAULT 0,
        mode        TEXT,
        error       TEXT
    )
    """,
    # 8 — Freie Einstellungen (Zielzeit, Trainingstage, max. HF ...)
    """
    CREATE TABLE IF NOT EXISTS setting (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # 9 — OAuth-Clients und -Tokens für den Remote-MCP-Zugang (siehe app/oauth.py)
    """
    CREATE TABLE IF NOT EXISTS oauth_client (
        client_id     TEXT PRIMARY KEY,
        client_name   TEXT,
        redirect_uris TEXT NOT NULL,   -- JSON-Liste
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_code (
        code             TEXT PRIMARY KEY,
        client_id        TEXT NOT NULL,
        redirect_uri     TEXT NOT NULL,
        code_challenge   TEXT NOT NULL,
        scope            TEXT NOT NULL DEFAULT '',
        resource         TEXT,
        expires_at       TEXT NOT NULL,
        used             INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_token (
        token_hash  TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,     -- access | refresh
        client_id   TEXT NOT NULL,
        scope       TEXT NOT NULL DEFAULT '',
        expires_at  TEXT,
        revoked     INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_token_kind ON oauth_token(kind, revoked)",
]


def init_db() -> None:
    conn = get_conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (step INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["step"] for r in conn.execute("SELECT step FROM schema_version")}
    for i, stmt in enumerate(SCHEMA_STEPS):
        if i in applied:
            continue
        conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (step, applied_at) VALUES (?, datetime('now'))", (i,)
        )


# --------------------------------------------------------------------------
# Kleine Helfer
# --------------------------------------------------------------------------


def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(get_conn().execute(sql, tuple(params)))


def q1(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, tuple(params)).fetchone()


def run(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    return get_conn().execute(sql, tuple(params))


def get_setting(key: str, default: Any = None) -> Any:
    row = q1("SELECT value FROM setting WHERE key = ?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    run(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
        (key, json.dumps(value)),
    )


def upsert_metric(day: date | str, kind: str, value: float, meta: dict | None = None) -> None:
    run(
        "INSERT INTO health_metric (day, kind, value, meta, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(day, kind) DO UPDATE SET value = excluded.value, "
        "meta = excluded.meta, updated_at = datetime('now')",
        (_d(day), kind, float(value), json.dumps(meta) if meta else None),
    )


def metric_series(kind: str, since: date | str, until: date | str | None = None) -> list[tuple[str, float]]:
    sql = "SELECT day, value FROM health_metric WHERE kind = ? AND day >= ?"
    params: list[Any] = [kind, _d(since)]
    if until is not None:
        sql += " AND day <= ?"
        params.append(_d(until))
    sql += " ORDER BY day"
    return [(r["day"], r["value"]) for r in q(sql, params)]


def latest_metric(kind: str) -> tuple[str, float] | None:
    row = q1(
        "SELECT day, value FROM health_metric WHERE kind = ? ORDER BY day DESC LIMIT 1", (kind,)
    )
    return (row["day"], row["value"]) if row else None


def _d(value: date | str | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def jloads(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
