"""
Demodaten erzeugen.

Damit sieht man das Dashboard sofort in Betrieb, ohne auf den ersten Garmin-Sync
zu warten — und kann die Ansichten anschauen, bevor eigene Daten drin sind.

    DB_PATH=./demo.db python scripts/seed_demo.py

Löschen: einfach die Datei wegwerfen. Auf die echte Datenbank NICHT anwenden,
das Skript legt erfundene Läufe an.
"""
from __future__ import annotations

import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, plan  # noqa: E402

random.seed(42)  # reproduzierbar — zwei Läufe erzeugen dieselben Demodaten


def main() -> None:
    db.init_db()
    today = date.today()
    race_day = today + timedelta(weeks=32)

    for table in ("workout_detail", "workout", "health_metric", "planned_session", "race", "note"):
        db.run(f"DELETE FROM {table}")

    db.run(
        "INSERT INTO race (name, day, distance_km, goal_time_sec) VALUES (?,?,?,?)",
        ("Demo-Marathon", race_day.isoformat(), 42.195, 3 * 3600 + 45 * 60),
    )

    # 16 Wochen Trainingshistorie mit langsam wachsendem Umfang.
    activity_id = 900_000_000
    for week in range(16, 0, -1):
        ws = today - timedelta(weeks=week)
        base_km = 28 + (16 - week) * 1.1
        if week % 4 == 0:
            base_km *= 0.72  # Entlastungswoche

        sessions = [
            (1, base_km * 0.20, 355),   # Dienstag locker
            (3, base_km * 0.22, 320),   # Donnerstag zügiger
            (5, base_km * 0.18, 360),   # Samstag locker
            (6, base_km * 0.40, 345),   # Sonntag lang
        ]
        for weekday, km, pace in sessions:
            day = ws + timedelta(days=weekday)
            if day > today:
                continue
            km = round(km * random.uniform(0.92, 1.08), 2)
            pace = pace + random.randint(-12, 12)
            duration = int(km * pace)
            avg_hr = 138 + (pace < 330) * 14 + random.randint(-5, 5)
            activity_id += 1
            cur = db.run(
                "INSERT INTO workout (garmin_activity_id, day, start_time, sport, name, "
                "duration_sec, distance_m, avg_hr, max_hr, training_load, hr_zones) "
                "VALUES (?,?,?,'run',?,?,?,?,?,?,?)",
                (
                    activity_id, day.isoformat(), f"{day}T07:15:00+02:00",
                    "Langer Lauf" if weekday == 6 else "Dauerlauf",
                    duration, km * 1000, avg_hr, avg_hr + 18,
                    round(km * 6.5),
                    db.json.dumps({
                        "zone1": int(duration * 0.25), "zone2": int(duration * 0.5),
                        "zone3": int(duration * 0.18), "zone4": int(duration * 0.07),
                    }),
                ),
            )
            if weekday == 6:
                db.run(
                    "INSERT INTO workout_detail (workout_id, decoupling_pct, laps) VALUES (?,?,?)",
                    (
                        cur.lastrowid,
                        round(random.uniform(2.5, 8.5), 1),
                        db.json.dumps([
                            {"n": i + 1, "distance_m": 1000,
                             "duration_sec": pace + random.randint(-10, 14),
                             "pace_s_km": pace + random.randint(-10, 14),
                             "avg_hr": avg_hr + i}
                            for i in range(int(km))
                        ]),
                    ),
                )

    # Tagesmetriken mit leichtem Wochenrhythmus.
    for i in range(120):
        day = today - timedelta(days=i)
        wave = math.sin(i / 6.0)
        db.upsert_metric(day, "hrv_overnight", round(52 + wave * 5 + random.uniform(-3, 3), 1))
        db.upsert_metric(day, "rhr", round(48 - wave * 1.5 + random.uniform(-2, 2)))
        db.upsert_metric(day, "sleep_minutes", round(415 + wave * 25 + random.uniform(-35, 35)))
        db.upsert_metric(day, "sleep_score", round(74 + wave * 8 + random.uniform(-6, 6)))
        db.upsert_metric(day, "training_readiness", round(68 + wave * 12 + random.uniform(-8, 8)))
        db.upsert_metric(day, "steps", round(9500 + random.uniform(-2500, 4000)))
        if i % 7 == 0:
            db.upsert_metric(day, "vo2max", round(49 + (120 - i) / 60.0, 1))

    db.upsert_metric(today, "race_marathon", 3 * 3600 + 52 * 60)
    db.upsert_metric(today, "race_half", 1 * 3600 + 51 * 60)
    db.upsert_metric(today, "load_ratio", 1.12)

    db.run("INSERT INTO note (day, kind, text) VALUES (?,?,?)",
           (today.isoformat(), "journal",
            "Demodaten. Rechte Wade beim langen Lauf leicht zwickend, sonst gut."))

    result = plan.generate(plan.PlanConfig(
        race_day=race_day,
        goal_time_sec=3 * 3600 + 45 * 60,
        start_weekly_km=42,
        start_day=today,
    ))
    plan.match_completed(today - timedelta(days=21), today)

    runs = db.q1("SELECT COUNT(*) AS n FROM workout")["n"]
    print(f"Demodaten in {db.DB_PATH}: {runs} Läufe, "
          f"{result['sessions']} geplante Einheiten bis {race_day}.")


if __name__ == "__main__":
    main()
