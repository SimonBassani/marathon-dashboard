"""
Garmin-Connect-Sync.

Holt Tagesmetriken (Schlaf, HRV, Ruhepuls, Readiness ...) und Aktivitäten und
schreibt sie idempotent in die SQLite-Datei. Läuft im selben Prozess wie das
Dashboard, angestossen von app/scheduler.py.

Es gibt keine offizielle Garmin-API für Privatpersonen. `python-garminconnect`
spricht die gleichen Endpunkte wie die Garmin-Connect-Website. Das funktioniert
zuverlässig, kann sich aber jederzeit ändern — deshalb ist hier JEDER Block
einzeln in try/except gekapselt: fällt ein Endpunkt aus, laufen die anderen
weiter, statt den ganzen Sync zu killen.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any

from . import db

log = logging.getLogger("garmin")

TOKEN_STORE = os.environ.get("GARMIN_TOKEN_STORE", "/data/garth")

# Aktivitäts-Typen von Garmin auf unsere vier Kategorien abbilden.
_SPORT_MAP = [
    ("run", ("run", "treadmill")),
    ("bike", ("cycl", "bik", "ride", "spinning")),
    ("swim", ("swim",)),
    ("strength", ("strength", "weight", "fitness_equipment")),
]


def map_sport(garmin_type: str | None) -> str:
    if not garmin_type:
        return "other"
    t = garmin_type.lower()
    for sport, needles in _SPORT_MAP:
        if any(n in t for n in needles):
            return sport
    return "other"


def safe(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def login():
    """Meldet sich an. Nutzt den Token-Cache, damit nicht jeder Sync ein
    Passwort-Login auslöst (Garmin drosselt das sonst)."""
    from garminconnect import Garmin

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError("GARMIN_EMAIL / GARMIN_PASSWORD fehlen")

    os.makedirs(TOKEN_STORE, exist_ok=True)
    api = Garmin(email=email, password=password)
    try:
        api.login(TOKEN_STORE)
        log.info("Login über Token-Cache")
    except Exception as exc:  # noqa: BLE001 — Token abgelaufen ist der Normalfall
        log.info("Token-Login fehlgeschlagen (%s), frischer Login", exc)
        api = Garmin(email=email, password=password)
        api.login()
        try:
            api.garth.dump(TOKEN_STORE)
        except Exception as dump_exc:  # noqa: BLE001
            log.warning("Token konnte nicht gespeichert werden: %s", dump_exc)
    return api


# --------------------------------------------------------------------------
# Tagesmetriken
# --------------------------------------------------------------------------


def fetch_day(api, day: date) -> list[tuple[date, str, float, dict | None]]:
    """Alle Tagesmetriken für einen Tag. Gibt Tupel zurück, schreibt nicht —
    so kann der Aufrufer parallelisieren und erst am Ende gesammelt committen."""
    ds = day.isoformat()
    rows: list[tuple[date, str, float, dict | None]] = []

    def add(kind: str, value: Any, meta: dict | None = None) -> None:
        if value is not None:
            rows.append((day, kind, float(value), meta))

    # Tagesstatistik: Schritte, Kalorien, Ruhepuls, Stress, Body Battery
    try:
        stats = api.get_stats(ds) or {}
        add("steps", stats.get("totalSteps"))
        add("calories", stats.get("totalKilocalories"))
        add("rhr", stats.get("restingHeartRate"))
        stress = stats.get("averageStressLevel")
        if stress and stress > 0:
            add("stress_avg", stress)
        add("body_battery_high", stats.get("bodyBatteryHighestValue"))
        add("body_battery_low", stats.get("bodyBatteryLowestValue"))
    except Exception as exc:  # noqa: BLE001
        log.warning("stats %s: %s", ds, exc)

    # Schlaf inkl. Phasen — für Marathonlaeufer die wichtigste Regenerationsgröße
    try:
        sleep = api.get_sleep_data(ds) or {}
        dto = sleep.get("dailySleepDTO") or {}
        total = dto.get("sleepTimeSeconds")
        if total:
            add("sleep_minutes", total / 60.0)
        add("sleep_score", safe(dto, "sleepScores", "overall", "value"))
        for key, kind in (
            ("deepSleepSeconds", "sleep_deep_min"),
            ("remSleepSeconds", "sleep_rem_min"),
            ("lightSleepSeconds", "sleep_light_min"),
            ("awakeSleepSeconds", "sleep_awake_min"),
        ):
            sec = dto.get(key)
            if sec is not None:  # 0 ist ein gültiger Wert, nicht "fehlt"
                add(kind, sec / 60.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("sleep %s: %s", ds, exc)

    # HRV über Nacht — Frühwarnsystem für Übertraining
    try:
        hrv = api.get_hrv_data(ds) or {}
        summary = hrv.get("hrvSummary") or {}
        add("hrv_overnight", summary.get("lastNightAvg"), {"status": summary.get("status")})
    except Exception as exc:  # noqa: BLE001
        log.warning("hrv %s: %s", ds, exc)

    # Training Readiness (0-100)
    try:
        raw = api.get_training_readiness(ds)
        r = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else None)
        if r:
            add("training_readiness", r.get("score"),
                {"level": r.get("level"), "feedback": r.get("feedbackShort")})
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness %s: %s", ds, exc)

    # Atemfrequenz — steigt bei beginnendem Infekt, bevor man es selbst merkt
    try:
        resp = api.get_respiration_data(ds) or {}
        val = resp.get("avgSleepRespirationValue") or resp.get("avgWakingRespirationValue")
        if val and val > 0:
            add("respiration_avg", val)
    except Exception as exc:  # noqa: BLE001
        log.warning("respiration %s: %s", ds, exc)

    return rows


def fetch_snapshot(api, today: date) -> list[tuple[date, str, float, dict | None]]:
    """Werte, die sich langsam ändern — einmal pro Sync reicht.
    VO2max, Trainingsstatus, Belastung und Garmins eigene Rennprognosen."""
    ds = today.isoformat()
    rows: list[tuple[date, str, float, dict | None]] = []

    def add(kind: str, value: Any, meta: dict | None = None) -> None:
        if value is not None:
            rows.append((today, kind, float(value), meta))

    # VO2max (laufspezifisch)
    try:
        mx = api.get_max_metrics(ds) or []
        entry = mx[0] if isinstance(mx, list) and mx else (mx if isinstance(mx, dict) else {})
        add("vo2max", safe(entry, "generic", "vo2MaxPreciseValue")
            or safe(entry, "generic", "vo2MaxValue"))
        add("fitness_age", safe(entry, "generic", "fitnessAge"))
    except Exception as exc:  # noqa: BLE001
        log.warning("vo2max: %s", exc)

    # Trainingsstatus + akute/chronische Belastung (Garmins ACWR)
    try:
        ts = api.get_training_status(ds) or {}
        latest = safe(ts, "mostRecentTrainingStatus", "latestTrainingStatusData", default={})
        picked = next((v for v in latest.values() if isinstance(v, dict)), None) if isinstance(latest, dict) else None
        if picked:
            acute = picked.get("acuteTrainingLoadDTO") or {}
            add("load_ratio", acute.get("acwr") or acute.get("acuteChronicWorkloadRatio"))
            add("acute_load", acute.get("dailyTrainingLoadAcute"))
            add("chronic_load", acute.get("dailyTrainingLoadChronic"))
            phrase = picked.get("trainingStatusFeedbackPhrase") or picked.get("trainingStatus")
            if phrase is not None:
                rows.append((today, "training_status", 1.0, {"status": str(phrase)}))
    except Exception as exc:  # noqa: BLE001
        log.warning("training_status: %s", exc)

    # Garmins Rennprognosen in Sekunden — die Marathon-Prognose ist unsere Referenz
    try:
        rp = api.get_race_predictions() or {}
        if isinstance(rp, list) and rp:
            rp = rp[0]
        if isinstance(rp, dict):
            for key, kind in (
                ("time5K", "race_5k"),
                ("time10K", "race_10k"),
                ("timeHalfMarathon", "race_half"),
                ("timeMarathon", "race_marathon"),
            ):
                add(kind, rp.get(key))
    except Exception as exc:  # noqa: BLE001
        log.warning("race_predictions: %s", exc)

    return rows


# --------------------------------------------------------------------------
# Aktivitäten
# --------------------------------------------------------------------------


def upsert_workouts(activities: list[dict]) -> int:
    from dateutil import parser as dt_parser
    from dateutil import tz

    tzinfo = tz.gettz(os.environ.get("TZ", "Europe/Vienna"))
    count = 0
    for a in activities:
        gid = a.get("activityId")
        start_str = a.get("startTimeLocal") or a.get("startTimeGMT")
        if not gid or not start_str:
            continue
        try:
            start = dt_parser.parse(start_str)
            if start.tzinfo is None:
                start = start.replace(tzinfo=tzinfo)
        except (ValueError, TypeError):
            continue

        sport = map_sport(safe(a, "activityType", "typeKey") or a.get("activityName"))
        hr_zones = a.get("hrTimeInZone")

        db.run(
            """
            INSERT INTO workout (
                garmin_activity_id, day, start_time, sport, name, duration_sec,
                distance_m, elevation_gain_m, avg_hr, max_hr, calories,
                training_load, aerobic_effect, anaerobic_effect, avg_cadence,
                hr_zones, source, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'garmin', datetime('now'))
            ON CONFLICT(garmin_activity_id) DO UPDATE SET
                day = excluded.day,
                start_time = excluded.start_time,
                sport = excluded.sport,
                name = excluded.name,
                duration_sec = excluded.duration_sec,
                distance_m = excluded.distance_m,
                elevation_gain_m = excluded.elevation_gain_m,
                avg_hr = excluded.avg_hr,
                max_hr = excluded.max_hr,
                calories = excluded.calories,
                avg_cadence = excluded.avg_cadence,
                -- Belastung und Zonen werden erst nachträglich angereichert.
                -- Sie dürfen daher NICHT mit NULL überschrieben werden.
                training_load    = COALESCE(excluded.training_load, workout.training_load),
                aerobic_effect   = COALESCE(excluded.aerobic_effect, workout.aerobic_effect),
                anaerobic_effect = COALESCE(excluded.anaerobic_effect, workout.anaerobic_effect),
                hr_zones         = COALESCE(excluded.hr_zones, workout.hr_zones),
                updated_at = datetime('now')
            """,
            (
                int(gid), start.date().isoformat(), start.isoformat(), sport,
                a.get("activityName"), int(a.get("duration") or 0), a.get("distance"),
                a.get("elevationGain"), a.get("averageHR"), a.get("maxHR"), a.get("calories"),
                a.get("activityTrainingLoad") or a.get("trainingLoad"),
                a.get("aerobicTrainingEffect"), a.get("anaerobicTrainingEffect"),
                a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageBikingCadenceInRevPerMinute"),
                json.dumps(hr_zones) if hr_zones else None,
            ),
        )
        count += 1
    return count


def enrich_run(api, workout_id: int, gid: int) -> None:
    """Lädt HR-Zonen und Runden nach und berechnet die aerobe Entkopplung.

    Entkopplung (Pa:Hr) ist die Marathon-Metrik schlechthin: Wenn die Herzfrequenz
    in der zweiten Hälfte eines langen Laufs bei gleicher Pace deutlich steigt,
    reicht die Grundlagenausdauer für die Zieldistanz noch nicht.
    """
    hr_zones = None
    try:
        zones = api.get_activity_hr_in_timezones(gid)
        if isinstance(zones, list):
            hr_zones = {
                f"zone{z.get('zoneNumber')}": round(z.get("secsInZone") or 0)
                for z in zones
                if z.get("zoneNumber") is not None
            }
    except Exception as exc:  # noqa: BLE001
        log.warning("hr_zones %s: %s", gid, exc)

    if hr_zones:
        db.run("UPDATE workout SET hr_zones = ? WHERE id = ?", (json.dumps(hr_zones), workout_id))

    laps = None
    try:
        splits = api.connectapi(f"/activity-service/activity/{gid}/splits")
        laps = _compact_laps(splits)
    except Exception as exc:  # noqa: BLE001
        log.warning("splits %s: %s", gid, exc)

    decoupling = None
    try:
        details = api.connectapi(f"/activity-service/activity/{gid}/details")
        decoupling = compute_decoupling(details)
    except Exception as exc:  # noqa: BLE001
        log.warning("details %s: %s", gid, exc)

    if laps is None and decoupling is None:
        return

    db.run(
        """
        INSERT INTO workout_detail (workout_id, laps, decoupling_pct, pace_drift_pct,
                                    cadence_drift_pct, fetched_at)
        VALUES (?,?,?,?,?, datetime('now'))
        ON CONFLICT(workout_id) DO UPDATE SET
            laps = COALESCE(excluded.laps, workout_detail.laps),
            decoupling_pct = COALESCE(excluded.decoupling_pct, workout_detail.decoupling_pct),
            pace_drift_pct = COALESCE(excluded.pace_drift_pct, workout_detail.pace_drift_pct),
            cadence_drift_pct = COALESCE(excluded.cadence_drift_pct, workout_detail.cadence_drift_pct),
            fetched_at = datetime('now')
        """,
        (
            workout_id,
            json.dumps(laps) if laps else None,
            (decoupling or {}).get("decoupling_pct"),
            (decoupling or {}).get("pace_drift_pct"),
            (decoupling or {}).get("cadence_drift_pct"),
        ),
    )


def _compact_laps(splits: Any) -> list[dict] | None:
    """Nur die Felder behalten, die man wirklich anschaut. Die Roh-JSON von
    Garmin ist pro Lauf mehrere hundert Kilobyte gross."""
    if not isinstance(splits, dict):
        return None
    laps = splits.get("lapDTOs")
    if not isinstance(laps, list):
        return None
    out = []
    for i, lap in enumerate(laps, start=1):
        if not isinstance(lap, dict):
            continue
        dist = lap.get("distance")
        dur = lap.get("duration")
        out.append(
            {
                "n": i,
                "distance_m": round(dist) if dist else None,
                "duration_sec": round(dur) if dur else None,
                "pace_s_km": round(dur / (dist / 1000)) if dist and dur and dist > 50 else None,
                "avg_hr": round(lap["averageHR"]) if lap.get("averageHR") else None,
                "elev_gain_m": round(lap["elevationGain"]) if lap.get("elevationGain") else None,
            }
        )
    return out or None


def compute_decoupling(details: Any) -> dict | None:
    """Vergleicht Pace-pro-Herzschlag in der ersten und zweiten Hälfte.

    decoupling_pct > 5 %  → Grundlagenausdauer reicht für diese Dauer noch nicht
    decoupling_pct < 5 %  → aerob gut abgesichert

    Erwartet Garmins `activityDetailMetrics` mit `metricDescriptors`.
    """
    if not isinstance(details, dict):
        return None
    metrics = details.get("activityDetailMetrics")
    descriptors = details.get("metricDescriptors")
    if not isinstance(metrics, list) or not isinstance(descriptors, list) or not metrics:
        return None

    idx_hr = idx_speed = idx_cad = None
    for i, d in enumerate(descriptors):
        key = (d.get("key") or "").lower() if isinstance(d, dict) else ""
        if "heartrate" in key and idx_hr is None:
            idx_hr = i
        elif "speed" in key and idx_speed is None:
            idx_speed = i
        elif "cadence" in key and idx_cad is None:
            idx_cad = i

    def column(idx: int | None) -> list[float]:
        if idx is None:
            return []
        out = []
        for m in metrics:
            vals = m.get("metrics") if isinstance(m, dict) else None
            if isinstance(vals, list) and len(vals) > idx and vals[idx] is not None:
                out.append(float(vals[idx]))
            else:
                out.append(float("nan"))
        return out

    hr = column(idx_hr)
    speed = column(idx_speed)
    cadence = column(idx_cad)

    result: dict[str, float] = {}

    def halves(series: list[float], floor: float) -> tuple[float, float] | None:
        clean = [(i, v) for i, v in enumerate(series) if v == v and v > floor]
        if len(clean) < 20:
            return None
        mid = clean[len(clean) // 2][0]
        first = [v for i, v in clean if i < mid]
        second = [v for i, v in clean if i >= mid]
        if len(first) < 8 or len(second) < 8:
            return None
        return sum(first) / len(first), sum(second) / len(second)

    # Aerobe Entkopplung: (Speed/HR erste Hälfte) vs. (Speed/HR zweite Hälfte)
    if hr and speed and len(hr) == len(speed):
        pairs = [(h, s) for h, s in zip(hr, speed) if h == h and s == s and h > 60 and s > 0.5]
        if len(pairs) >= 40:
            mid = len(pairs) // 2
            def ratio(chunk: list[tuple[float, float]]) -> float:
                return (sum(s for _, s in chunk) / len(chunk)) / (sum(h for h, _ in chunk) / len(chunk))
            r1, r2 = ratio(pairs[:mid]), ratio(pairs[mid:])
            if r1 > 0:
                dec = round((r1 - r2) / r1 * 100, 1)
                if -30 <= dec <= 30:  # alles darüber ist Messrauschen (Pausen, GPS-Aussetzer)
                    result["decoupling_pct"] = dec

    sp = halves(speed, 0.5)
    if sp and sp[0] > 0:
        drift = round((sp[1] / sp[0] - 1) * 100, 1)
        if -40 <= drift <= 40:
            result["pace_drift_pct"] = drift

    cad = halves(cadence, 50)
    if cad and cad[0] > 0:
        drift = round((cad[1] / cad[0] - 1) * 100, 1)
        if -30 <= drift <= 30:
            result["cadence_drift_pct"] = drift

    return result or None


# --------------------------------------------------------------------------
# Einstiegspunkt
# --------------------------------------------------------------------------


def sync(days: int = 7, workers: int = 4, mode: str = "manual", enrich_limit: int = 5) -> dict:
    """Holt `days` Tage rückwärts. Idempotent — mehrfach laufen lassen ist unschädlich."""
    db.init_db()
    cur = db.run("INSERT INTO sync_run (mode) VALUES (?)", (mode,))
    run_id = cur.lastrowid
    started = time.monotonic()
    written = 0
    error: str | None = None

    try:
        api = login()
        today = date.today()
        targets = [today - timedelta(days=i) for i in range(max(1, days))]

        # Die Tagesabfragen sind reine I/O-Wartezeit — parallel holen, seriell schreiben.
        collected: list[tuple[date, str, float, dict | None]] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch_day, api, d): d for d in targets}
            for fut in as_completed(futures):
                try:
                    collected.extend(fut.result())
                except Exception as exc:  # noqa: BLE001
                    log.warning("fetch_day %s: %s", futures[fut], exc)

        collected.extend(fetch_snapshot(api, today))

        with db.tx():
            for day, kind, value, meta in collected:
                db.upsert_metric(day, kind, value, meta)
                written += 1

        # Aktivitäten
        try:
            start = (today - timedelta(days=max(1, days))).isoformat()
            activities = api.get_activities_by_date(start, today.isoformat()) or []
            with db.tx():
                written += upsert_workouts(activities)
        except Exception as exc:  # noqa: BLE001
            log.warning("activities: %s", exc)

        # Läufe ohne Detaildaten nachträglich anreichern — teuerster Teil,
        # deshalb gedeckelt auf `enrich_limit` pro Lauf.
        pending = db.q(
            """
            SELECT w.id, w.garmin_activity_id FROM workout w
            LEFT JOIN workout_detail d ON d.workout_id = w.id
            WHERE w.sport = 'run' AND w.garmin_activity_id IS NOT NULL
              AND d.workout_id IS NULL AND w.duration_sec > 600
            ORDER BY w.day DESC LIMIT ?
            """,
            (enrich_limit,),
        )
        for row in pending:
            try:
                enrich_run(api, row["id"], row["garmin_activity_id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("enrich %s: %s", row["garmin_activity_id"], exc)

    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        log.error("Sync fehlgeschlagen: %s", error)

    db.run(
        "UPDATE sync_run SET finished_at = datetime('now'), ok = ?, written = ?, error = ? WHERE id = ?",
        (0 if error else 1, written, error, run_id),
    )
    return {
        "ok": error is None,
        "written": written,
        "seconds": round(time.monotonic() - started, 1),
        "error": error,
    }


if __name__ == "__main__":  # manueller Aufruf: python -m app.garmin 30
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(sync(days=n, mode="cli"))
