"""
Marathon-Analytik.

Alles, was aus den Rohdaten eine Aussage macht: Wo stehe ich, ist die Steigerung
gesund, trägt die Zielzeit noch, und was ist diese Woche dran.

Reine Funktionen über der Datenbank — keine Seiteneffekte, damit sowohl die
Weboberfläche als auch der MCP-Server dieselben Zahlen sehen.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

from . import db

# --------------------------------------------------------------------------
# Formatierung
# --------------------------------------------------------------------------


def fmt_time(seconds: float | None) -> str:
    """3:45:12 bzw. 42:10 für kürzere Zeiten."""
    if seconds is None:
        return "–"
    s = int(round(seconds))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_pace(sec_per_km: float | None) -> str:
    if not sec_per_km or sec_per_km <= 0:
        return "–"
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"


def parse_time(text: str) -> int | None:
    """'3:45', '3:45:00' oder '3h45' → Sekunden. Toleriert Alltagsschreibweisen."""
    if not text:
        return None
    cleaned = text.strip().lower().replace("h", ":").replace("std", ":").replace(" ", "")
    parts = [p for p in cleaned.split(":") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        # Bei einem Marathon ist "3:45" gemeint als 3 Std 45 Min, nicht 3 Min 45 Sek.
        return nums[0] * 3600 + nums[1] * 60
    if len(nums) == 1:
        return nums[0] * 60
    return None


# --------------------------------------------------------------------------
# Rennen und Trainingsphase
# --------------------------------------------------------------------------

# Die Phasen laufen rückwärts vom Rennen. Werte in Wochen VOR dem Renntag.
PHASES = [
    (0, 3, "Taper", "Umfang runter, Intensität halten. Frisch werden ist jetzt wichtiger als fit werden."),
    (3, 9, "Peak", "Höchster Umfang, lange Läufe im Zielpace-Bereich."),
    (9, 18, "Aufbau", "Umfang steigern, Tempoeinheiten und Marathonpace einbauen."),
    (18, 999, "Grundlage", "Ruhig und viel. Aerobe Basis bauen, Kraft und Technik nicht vergessen."),
]


def active_race() -> dict | None:
    """Das nächste A-Rennen, sonst das nächste überhaupt."""
    today = date.today().isoformat()
    row = db.q1(
        "SELECT * FROM race WHERE day >= ? ORDER BY (priority != 'A'), day LIMIT 1", (today,)
    )
    if row is None:
        row = db.q1("SELECT * FROM race ORDER BY day DESC LIMIT 1")
    return dict(row) if row else None


def phase_for(weeks_out: float) -> tuple[str, str]:
    for lo, hi, name, hint in PHASES:
        if lo <= weeks_out < hi:
            return name, hint
    return "Grundlage", PHASES[-1][3]


# --------------------------------------------------------------------------
# Wochenvolumen
# --------------------------------------------------------------------------


def week_start(day: date) -> date:
    """Montag der Woche."""
    return day - timedelta(days=day.weekday())


def weekly_volume(weeks: int = 12, today: date | None = None) -> list[dict]:
    """Laufkilometer, Dauer und Belastung je Kalenderwoche, älteste zuerst."""
    today = today or date.today()
    first = week_start(today) - timedelta(weeks=weeks - 1)
    rows = db.q(
        """
        SELECT day, distance_m, duration_sec, training_load, avg_hr, hr_zones
        FROM workout WHERE sport = 'run' AND day >= ? AND day <= ?
        """,
        (first.isoformat(), today.isoformat()),
    )

    buckets: dict[str, dict[str, Any]] = {}
    for i in range(weeks):
        ws = first + timedelta(weeks=i)
        buckets[ws.isoformat()] = {
            "week_start": ws.isoformat(),
            "km": 0.0,
            "minutes": 0.0,
            "load": 0.0,
            "runs": 0,
            "longest_km": 0.0,
        }

    for r in rows:
        ws = week_start(date.fromisoformat(r["day"])).isoformat()
        b = buckets.get(ws)
        if b is None:
            continue
        km = (r["distance_m"] or 0) / 1000.0
        b["km"] += km
        b["minutes"] += (r["duration_sec"] or 0) / 60.0
        b["load"] += r["training_load"] or 0
        b["runs"] += 1
        b["longest_km"] = max(b["longest_km"], km)

    out = []
    for b in buckets.values():
        b["km"] = round(b["km"], 1)
        b["minutes"] = round(b["minutes"])
        b["load"] = round(b["load"])
        b["longest_km"] = round(b["longest_km"], 1)
        out.append(b)
    return sorted(out, key=lambda x: x["week_start"])


def ramp_rate(volumes: list[dict]) -> float | None:
    """Steigerung der aktuellen gegenüber der Vorwoche in Prozent.

    Faustregel: mehr als 10 % pro Woche erhöht das Verletzungsrisiko deutlich.
    Die laufende (unfertige) Woche wird bewusst NICHT verglichen — sonst steht
    montags immer -80 % da.
    """
    # `volumes` endet immer mit der laufenden Woche — die fällt hier raus.
    finished = [v for v in volumes[:-1] if v["km"] > 0][-2:]
    if len(finished) < 2 or finished[0]["km"] <= 0:
        return None
    return round((finished[1]["km"] / finished[0]["km"] - 1) * 100, 1)


def acwr(today: date | None = None) -> float | None:
    """Acute-Chronic Workload Ratio: Belastung der letzten 7 Tage gegen den
    Wochenschnitt der letzten 28 Tage.

    0,8–1,3 = grün. Über 1,5 = das Trainingsvolumen ist der Form davongelaufen.

    Wir nehmen Garmins Training Load, wenn vorhanden, sonst ersatzweise
    Kilometer — nicht jede Uhr liefert Load.
    """
    today = today or date.today()
    d7 = (today - timedelta(days=6)).isoformat()
    d28 = (today - timedelta(days=27)).isoformat()
    ds = today.isoformat()

    row = db.q1(
        "SELECT COALESCE(SUM(training_load),0) AS load, COALESCE(SUM(distance_m),0) AS dist "
        "FROM workout WHERE day >= ? AND day <= ?",
        (d7, ds),
    )
    row28 = db.q1(
        "SELECT COALESCE(SUM(training_load),0) AS load, COALESCE(SUM(distance_m),0) AS dist "
        "FROM workout WHERE day >= ? AND day <= ?",
        (d28, ds),
    )
    if not row or not row28:
        return None

    acute, chronic = (row["load"], row28["load"] / 4.0)
    if acute <= 0 or chronic <= 0:
        acute, chronic = (row["dist"], row28["dist"] / 4.0)
    if chronic <= 0:
        return None
    return round(acute / chronic, 2)


def intensity_split(days: int = 28, today: date | None = None) -> dict | None:
    """Anteil lockeres vs. hartes Laufen anhand der HF-Zonen.

    Für Marathon gilt 80/20: rund 80 % der Zeit in Zone 1-2. Wer dauernd im
    Mittelbereich hängt, wird weder erholt noch schneller.
    """
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    rows = db.q(
        "SELECT hr_zones FROM workout WHERE sport = 'run' AND day >= ? AND hr_zones IS NOT NULL",
        (since,),
    )
    easy = hard = 0.0
    for r in rows:
        zones = db.jloads(r["hr_zones"])
        if not isinstance(zones, dict):
            continue
        for key, sec in zones.items():
            try:
                n = int(str(key).replace("zone", ""))
                val = float(sec or 0)
            except (ValueError, TypeError):
                continue
            if n <= 2:
                easy += val
            else:
                hard += val
    total = easy + hard
    if total <= 0:
        return None
    return {
        "easy_pct": round(easy / total * 100),
        "hard_pct": round(hard / total * 100),
        "hours": round(total / 3600, 1),
    }


# --------------------------------------------------------------------------
# Prognose
# --------------------------------------------------------------------------

RIEGEL_EXPONENT = 1.06


def riegel(known_distance_km: float, known_time_sec: float, target_km: float = 42.195) -> float:
    """Riegels Formel: T2 = T1 * (D2/D1)^1.06.

    Für den Sprung von 10 km auf Marathon ist sie optimistisch — sie setzt
    voraus, dass die Langstreckenausdauer wirklich da ist. Deshalb gewichten wir
    weiter unten längere Referenzlaeufe stärker.
    """
    return known_time_sec * (target_km / known_distance_km) ** RIEGEL_EXPONENT


def best_efforts(days: int = 90, today: date | None = None) -> list[dict]:
    """Die schnellsten Dauerlaeufe der letzten Monate als Prognosegrundlage.
    Nur Läufe ab 5 km, damit kein Intervalltraining die Rechnung verzerrt."""
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    rows = db.q(
        """
        SELECT id, day, name, distance_m, duration_sec FROM workout
        WHERE sport = 'run' AND day >= ? AND distance_m >= 5000 AND duration_sec > 0
        ORDER BY day DESC
        """,
        (since,),
    )
    out = []
    for r in rows:
        km = r["distance_m"] / 1000.0
        pace = r["duration_sec"] / km
        out.append(
            {
                "id": r["id"],
                "day": r["day"],
                "name": r["name"],
                "km": round(km, 2),
                "duration_sec": r["duration_sec"],
                "pace_s_km": round(pace),
                "projected_marathon_sec": round(riegel(km, r["duration_sec"])),
            }
        )
    # Die schnellsten drei Hochrechnungen — das sind die aussagekräftigsten Tage.
    return sorted(out, key=lambda x: x["projected_marathon_sec"])[:3]


def marathon_prediction(today: date | None = None) -> dict:
    """Kombiniert Garmins eigene Prognose mit einer Riegel-Hochrechnung.

    Garmin schätzt aus VO2max und ist bei Einsteigern oft zu optimistisch, weil
    es die Langstreckenausdauer nicht kennt. Die Riegel-Hochrechnung aus echten
    Läufen ist konservativer. Wir zeigen beide und mitteln für die Anzeige.
    """
    garmin = db.latest_metric("race_marathon")
    efforts = best_efforts(today=today)
    riegel_sec = efforts[0]["projected_marathon_sec"] if efforts else None
    garmin_sec = garmin[1] if garmin else None

    if garmin_sec and riegel_sec:
        combined = round(garmin_sec * 0.4 + riegel_sec * 0.6)
        confidence = "mittel"
    else:
        combined = garmin_sec or riegel_sec
        confidence = "niedrig"

    # Wer schon einen langen Lauf über 30 km in den Beinen hat, dessen Prognose
    # steht auf deutlich festerem Boden.
    long_run = db.q1(
        "SELECT MAX(distance_m) AS m FROM workout WHERE sport = 'run' AND day >= ?",
        (((today or date.today()) - timedelta(days=90)).isoformat(),),
    )
    longest_km = round((long_run["m"] or 0) / 1000.0, 1) if long_run else 0.0
    if combined and longest_km >= 30:
        confidence = "hoch"

    return {
        "garmin_sec": garmin_sec,
        "riegel_sec": riegel_sec,
        "combined_sec": combined,
        "confidence": confidence,
        "longest_run_km": longest_km,
        "basis": efforts,
    }


def goal_check(race: dict | None, prediction: dict) -> dict | None:
    """Vergleicht Zielzeit mit der Prognose und übersetzt das in Zielpace."""
    if not race or not race.get("goal_time_sec"):
        return None
    goal = race["goal_time_sec"]
    dist = race.get("distance_km") or 42.195
    goal_pace = goal / dist
    pred = prediction.get("combined_sec")
    gap = (pred - goal) if pred else None

    if gap is None:
        verdict = "Noch zu wenig Daten für eine Einschätzung."
    elif gap <= -300:
        verdict = "Die Zielzeit ist konservativ — da geht mehr."
    elif gap <= 300:
        verdict = "Zielzeit und aktuelle Form passen zusammen."
    elif gap <= 1200:
        verdict = "Ambitioniert, aber in der verbleibenden Zeit machbar."
    else:
        verdict = "Aktuell deutlich zu schnell angesetzt. Entweder Ziel anpassen oder Umfang aufbauen."

    return {
        "goal_sec": goal,
        "goal_pace_s_km": round(goal_pace),
        "predicted_sec": pred,
        "gap_sec": round(gap) if gap is not None else None,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------
# Gesundheit und Warnungen
# --------------------------------------------------------------------------


def trend(kind: str, days: int = 28, today: date | None = None) -> dict | None:
    """Mittelwert der letzten 7 Tage gegen die Vergleichsperiode davor."""
    today = today or date.today()
    series = db.metric_series(kind, today - timedelta(days=days), today)
    if len(series) < 4:
        return None
    recent_cut = (today - timedelta(days=6)).isoformat()
    recent = [v for d, v in series if d >= recent_cut]
    baseline = [v for d, v in series if d < recent_cut]
    if not recent or not baseline:
        return None
    r, b = statistics.fmean(recent), statistics.fmean(baseline)
    return {
        "recent": round(r, 1),
        "baseline": round(b, 1),
        "delta_pct": round((r / b - 1) * 100, 1) if b else None,
        "n": len(series),
    }


def warnings(today: date | None = None) -> list[dict]:
    """Konkrete Warnungen statt Zahlenfriedhof. Jede hat einen Grund und einen Rat."""
    today = today or date.today()
    out: list[dict] = []

    ratio = acwr(today)
    if ratio is not None and ratio > 1.5:
        out.append({
            "level": "hoch",
            "title": f"Belastungssprung (ACWR {ratio})",
            "text": "Die letzten 7 Tage liegen weit über deinem 4-Wochen-Schnitt. "
                    "Nächste Woche bewusst zurückfahren, sonst wird daraus eine Verletzung.",
        })
    elif ratio is not None and ratio < 0.7:
        out.append({
            "level": "info",
            "title": f"Belastung eingebrochen (ACWR {ratio})",
            "text": "Deutlich weniger als gewohnt. Wenn das kein geplanter Entlastungsblock war, "
                    "wieder langsam aufbauen.",
        })

    vols = weekly_volume(6, today)
    ramp = ramp_rate(vols)
    if ramp is not None and ramp > 25:
        out.append({
            "level": "hoch",
            "title": f"Umfang +{ramp} % zur Vorwoche",
            "text": "Über 10 % pro Woche gilt als Grenze. Diese Woche halten statt weiter steigern.",
        })

    hrv = trend("hrv_overnight", today=today)
    if hrv and hrv["delta_pct"] is not None and hrv["delta_pct"] < -12:
        out.append({
            "level": "mittel",
            "title": f"HRV {hrv['delta_pct']} % unter Normal",
            "text": "Der Körper erholt sich gerade schlechter als sonst. Harte Einheiten verschieben, "
                    "Schlaf priorisieren.",
        })

    rhr = trend("rhr", today=today)
    if rhr and rhr["delta_pct"] is not None and rhr["delta_pct"] > 8:
        out.append({
            "level": "mittel",
            "title": f"Ruhepuls {rhr['delta_pct']} % erhöht",
            "text": "Typisch für beginnenden Infekt oder zu wenig Regeneration. Ein bis zwei ruhige Tage.",
        })

    sleep = trend("sleep_minutes", today=today)
    if sleep and sleep["recent"] < 390:
        out.append({
            "level": "mittel",
            "title": f"Schlaf im Schnitt {round(sleep['recent'] / 60, 1)} h",
            "text": "Unter 6,5 h lässt sich ein Marathonaufbau nicht verkraften. Das ist der billigste "
                    "Hebel im ganzen Plan.",
        })

    split = intensity_split(today=today)
    if split and split["easy_pct"] < 65:
        out.append({
            "level": "mittel",
            "title": f"Nur {split['easy_pct']} % locker gelaufen",
            "text": "Zu viel Mitteltempo. Die lockeren Läufe wirklich locker machen — Ziel sind rund 80 %.",
        })

    # Der lange Lauf ist die Einheit, die den Marathon entscheidet.
    race = active_race()
    if race:
        weeks_out = (date.fromisoformat(race["day"]) - today).days / 7
        longest = db.q1(
            "SELECT MAX(distance_m) AS m FROM workout WHERE sport = 'run' AND day >= ?",
            ((today - timedelta(days=28)).isoformat(),),
        )
        longest_km = (longest["m"] or 0) / 1000.0 if longest else 0.0
        if 3 < weeks_out <= 10 and longest_km < 25:
            out.append({
                "level": "hoch",
                "title": f"Längster Lauf zuletzt {round(longest_km, 1)} km",
                "text": f"Noch {round(weeks_out)} Wochen bis zum Rennen. Bis zum Taper sollten "
                        "mindestens zwei Läufe über 30 km stehen.",
            })

    return out


# --------------------------------------------------------------------------
# Gesamtbild
# --------------------------------------------------------------------------


def training_state(today: date | None = None) -> dict:
    """Der eine Aufruf, der alles Wichtige zusammenfasst.

    Genau das liefert auch das MCP-Tool `trainingsstand` — bewusst kompakt, damit
    ein Claude-Chat davon nicht das halbe Kontextfenster verbraucht.
    """
    today = today or date.today()
    race = active_race()
    weeks_out = None
    phase = phase_hint = None
    if race:
        days_out = (date.fromisoformat(race["day"]) - today).days
        weeks_out = round(days_out / 7, 1)
        phase, phase_hint = phase_for(max(0.0, days_out / 7))

    vols = weekly_volume(12, today)
    finished = [v for v in vols[:-1] if v["km"] > 0]
    pred = marathon_prediction(today)

    return {
        "today": today.isoformat(),
        "race": race,
        "days_to_race": (date.fromisoformat(race["day"]) - today).days if race else None,
        "weeks_to_race": weeks_out,
        "phase": phase,
        "phase_hint": phase_hint,
        "this_week_km": vols[-1]["km"] if vols else 0.0,
        "last_week_km": finished[-1]["km"] if finished else 0.0,
        "avg_4w_km": round(statistics.fmean([v["km"] for v in finished[-4:]]), 1) if finished else 0.0,
        "longest_4w_km": max((v["longest_km"] for v in vols[-4:]), default=0.0),
        "ramp_pct": ramp_rate(vols),
        "acwr": acwr(today),
        "intensity": intensity_split(today=today),
        "prediction": pred,
        "goal": goal_check(race, pred),
        "vo2max": (db.latest_metric("vo2max") or (None, None))[1],
        "readiness": (db.latest_metric("training_readiness") or (None, None))[1],
        "hrv_trend": trend("hrv_overnight", today=today),
        "rhr_trend": trend("rhr", today=today),
        "sleep_trend": trend("sleep_minutes", today=today),
        "warnings": warnings(today),
        "weekly": vols,
    }


def recent_runs(limit: int = 10, today: date | None = None) -> list[dict]:
    rows = db.q(
        """
        SELECT w.id, w.day, w.name, w.distance_m, w.duration_sec, w.avg_hr, w.max_hr,
               w.training_load, w.rpe, w.notes, d.decoupling_pct
        FROM workout w LEFT JOIN workout_detail d ON d.workout_id = w.id
        WHERE w.sport = 'run' ORDER BY w.day DESC, w.start_time DESC LIMIT ?
        """,
        (limit,),
    )
    out = []
    for r in rows:
        km = (r["distance_m"] or 0) / 1000.0
        out.append(
            {
                "id": r["id"],
                "day": r["day"],
                "name": r["name"],
                "km": round(km, 2),
                "duration_sec": r["duration_sec"],
                "pace_s_km": round(r["duration_sec"] / km) if km > 0.3 and r["duration_sec"] else None,
                "avg_hr": round(r["avg_hr"]) if r["avg_hr"] else None,
                "load": round(r["training_load"]) if r["training_load"] else None,
                "decoupling_pct": r["decoupling_pct"],
                "rpe": r["rpe"],
                "notes": r["notes"],
            }
        )
    return out
