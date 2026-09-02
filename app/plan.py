"""
Trainingsplan-Generator.

Erzeugt aus Renntag, Zielzeit, aktuellem Wochenumfang und verfügbaren Tagen
einen periodisierten Plan bis zum Rennen und schreibt ihn als `planned_session`
in die Datenbank.

Der Plan ist ein Vorschlag, kein Gesetz. Er ist bewusst so gebaut, dass Claude
ihn über den MCP-Zugang wochenweise umbauen kann — deshalb trägt jede Einheit
ein `source`-Feld ('generator' oder 'claude'), und `plan_schreiben` ersetzt immer
nur den angefragten Zeitraum.

Trainingslogik (Standardaufbau für Marathon):
  * 3 Wochen steigern, 1 Woche entlasten (rund 70 % Umfang)
  * Der lange Lauf wächst auf 30-35 km, danach Taper
  * 3 Wochen Taper: 75 %, 55 %, 35 % des Peak-Umfangs
  * Rund 80 % des Volumens locker, harte Einheiten klar abgegrenzt
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db
from .analysis import phase_for, week_start

# Wochentage: 0 = Montag
DEFAULT_RUN_DAYS = [1, 3, 5, 6]  # Di, Do, Sa, So — Sonntag ist der lange Lauf

KIND_LABELS = {
    "easy": "Lockerer Dauerlauf",
    "long": "Langer Lauf",
    "tempo": "Tempodauerlauf",
    "interval": "Intervalle",
    "race_pace": "Marathonpace",
    "recovery": "Regenerationslauf",
    "rest": "Ruhetag",
    "strength": "Kraft / Stabi",
    "cross": "Alternativtraining",
}


@dataclass
class PlanConfig:
    race_day: date
    goal_time_sec: int | None = None
    distance_km: float = 42.195
    start_weekly_km: float = 30.0
    peak_weekly_km: float | None = None
    run_days: list[int] = field(default_factory=lambda: list(DEFAULT_RUN_DAYS))
    long_run_day: int = 6  # Sonntag
    start_day: date | None = None

    def __post_init__(self) -> None:
        if self.start_day is None:
            self.start_day = date.today()
        if not self.run_days:
            self.run_days = list(DEFAULT_RUN_DAYS)
        self.run_days = sorted({d % 7 for d in self.run_days})
        if self.long_run_day not in self.run_days:
            self.long_run_day = self.run_days[-1]
        if self.peak_weekly_km is None:
            # Realistischer Peak: rund 60 % über dem Ausgangsumfang, aber nie
            # mehr als das Doppelte und nie unter 45 km für einen Marathon.
            self.peak_weekly_km = max(45.0, min(self.start_weekly_km * 1.6, self.start_weekly_km * 2))


def target_pace_s(goal_time_sec: int | None, distance_km: float) -> int | None:
    if not goal_time_sec or distance_km <= 0:
        return None
    return round(goal_time_sec / distance_km)


def paces_from_goal(goal_pace: int | None) -> dict[str, int | None]:
    """Trainingspaces abgeleitet von der Marathon-Zielpace.

    Die Aufschläge sind die üblichen Daumenwerte aus der Trainingsliteratur:
    locker deutlich langsamer als Wettkampf, Tempo etwas schneller,
    Intervalle klar schneller.
    """
    if not goal_pace:
        return {k: None for k in ("easy", "long", "race_pace", "tempo", "interval", "recovery")}
    return {
        "recovery": goal_pace + 90,
        "easy": goal_pace + 65,
        "long": goal_pace + 45,
        "race_pace": goal_pace,
        "tempo": goal_pace - 15,
        "interval": goal_pace - 45,
    }


def week_targets(cfg: PlanConfig, weeks: int) -> list[float]:
    """Wochenumfang je Trainingswoche, inklusive Entlastungswochen und Taper."""
    taper_weeks = min(3, max(1, weeks // 6)) if weeks >= 6 else 1
    build_weeks = weeks - taper_weeks

    targets: list[float] = []
    for i in range(build_weeks):
        # Lineare Steigerung vom Start- zum Peak-Umfang ...
        progress = i / max(1, build_weeks - 1)
        base = cfg.start_weekly_km + (cfg.peak_weekly_km - cfg.start_weekly_km) * progress
        # ... aber jede 4. Woche ist Entlastung.
        if i > 0 and (i + 1) % 4 == 0:
            base *= 0.7
        targets.append(round(base, 1))

    taper_factors = {3: [0.75, 0.55, 0.35], 2: [0.6, 0.35], 1: [0.4]}[taper_weeks]
    for f in taper_factors:
        targets.append(round(cfg.peak_weekly_km * f, 1))
    return targets


def long_run_km(week_target: float, weeks_out: float, peak: float) -> float:
    """Der lange Lauf ist rund ein Drittel der Woche, gedeckelt bei 35 km.
    Im Taper fällt er deutlich ab, in der Rennwoche entfällt er."""
    if weeks_out <= 0.5:
        return 0.0
    share = 0.33 if weeks_out > 3 else 0.28
    km = week_target * share
    cap = 35.0 if peak >= 70 else 32.0
    return round(min(km, cap), 1)


def build_week(cfg: PlanConfig, ws: date, target_km: float, weeks_out: float,
               paces: dict[str, int | None]) -> list[dict]:
    """Eine Trainingswoche als Liste von Einheiten."""
    phase, _ = phase_for(max(0.0, weeks_out))
    sessions: list[dict] = []

    # Ob dies die Rennwoche ist, entscheidet der Kalender — nicht `weeks_out`.
    # Fällt das Rennen z. B. auf einen Samstag, ist weeks_out für diese Woche
    # rund 0,7 und ein Schwellwert wie "<= 0.5" würde den Renntag verschlucken.
    is_race_week = ws <= cfg.race_day <= ws + timedelta(days=6)

    lr_km = 0.0 if is_race_week else long_run_km(target_km, weeks_out, cfg.peak_weekly_km or target_km)
    remaining = max(0.0, target_km - lr_km)
    other_days = [d for d in cfg.run_days if d != cfg.long_run_day]

    # Qualitätseinheit: in der Grundlagenphase Tempo, später Marathonpace bzw.
    # Intervalle. In der Rennwoche gar nichts Hartes mehr.
    if is_race_week or weeks_out <= 1:
        quality_kind = None
    elif phase == "Taper":
        quality_kind = "race_pace"
    elif phase == "Grundlage":
        quality_kind = "tempo"
    else:
        quality_kind = "interval" if int(weeks_out) % 2 == 0 else "race_pace"

    quality_day = other_days[len(other_days) // 2] if other_days else None
    per_easy = round(remaining / max(1, len(other_days)), 1) if other_days else 0.0

    for d in other_days:
        day = ws + timedelta(days=d)
        if d == quality_day and quality_kind:
            km = round(min(per_easy + 2, remaining * 0.45), 1)
            sessions.append({
                "day": day,
                "kind": quality_kind,
                "title": _quality_title(quality_kind, km, weeks_out),
                "distance_km": km,
                "target_pace_s": paces.get(quality_kind),
                "description": _quality_desc(quality_kind, paces),
            })
        else:
            sessions.append({
                "day": day,
                "kind": "easy",
                "title": f"Locker {per_easy:g} km",
                "distance_km": per_easy,
                "target_pace_s": paces.get("easy"),
                "description": "Unterhaltungstempo. Wenn du nicht sprechen kannst, ist es zu schnell.",
            })

    if lr_km > 0:
        day = ws + timedelta(days=cfg.long_run_day)
        with_rp = weeks_out <= 12 and lr_km >= 20
        sessions.append({
            "day": day,
            "kind": "long",
            "title": f"Langer Lauf {lr_km:g} km",
            "distance_km": lr_km,
            "target_pace_s": paces.get("long"),
            "description": (
                f"Ruhig beginnen. Letzte {max(5, round(lr_km * 0.3)):g} km im Marathonpace, "
                "damit der Körper das Renntempo mit müdem Zustand verbindet."
                if with_rp else
                "Durchgehend locker. Es geht um die Dauer auf den Beinen, nicht um das Tempo."
            ),
        })

    # Ein fester Krafttermin — der wirksamste Verletzungsschutz im Marathonaufbau.
    strength_day = next((d for d in range(7) if d not in cfg.run_days), None)
    if strength_day is not None and not is_race_week and weeks_out > 1:
        sessions.append({
            "day": ws + timedelta(days=strength_day),
            "kind": "strength",
            "title": "Kraft / Stabi 30 min",
            "distance_km": None,
            "duration_min": 30,
            "target_pace_s": None,
            "description": "Rumpf, Waden, Gesäß, einbeinig. Hält die Achillessehne aus dem Aufbau raus.",
        })

    if is_race_week:
        race_km = cfg.distance_km
        # Nichts mehr nach dem Rennen, und der Tag davor bleibt frei.
        sessions = [s for s in sessions if s["day"] < cfg.race_day - timedelta(days=1)]
        sessions.append({
            "day": cfg.race_day,
            "kind": "race_pace",
            "title": "RENNTAG",
            "distance_km": race_km,
            "target_pace_s": paces.get("race_pace"),
            "description": "Erste 10 km bewusst zu langsam anfühlen lassen. Alles davor war Vorbereitung darauf.",
        })

    return sessions


def _quality_title(kind: str, km: float, weeks_out: float) -> str:
    if kind == "interval":
        reps = 5 if weeks_out > 8 else 6
        return f"{reps} x 1000 m Intervalle"
    if kind == "tempo":
        return f"Tempodauerlauf {km:g} km"
    return f"{km:g} km im Marathonpace"


def _quality_desc(kind: str, paces: dict[str, int | None]) -> str:
    from .analysis import fmt_pace

    if kind == "interval":
        return (f"Nach 15 min Einlaufen. Zielpace {fmt_pace(paces.get('interval'))}, "
                "je 2–3 min Trabpause. Danach 10 min auslaufen.")
    if kind == "tempo":
        return (f"20 min einlaufen, dann durchgehend {fmt_pace(paces.get('tempo'))} — "
                "zügig, aber kontrolliert. Auslaufen nicht vergessen.")
    return (f"Im Renntempo {fmt_pace(paces.get('race_pace'))}. Die Einheit, die dir sagt, "
            "ob die Zielzeit realistisch ist.")


def generate(cfg: PlanConfig, replace: bool = True) -> dict:
    """Erzeugt den Plan und schreibt ihn. Bestehende `claude`-Einheiten bleiben
    per Default erhalten, wenn `replace=False`."""
    start = week_start(cfg.start_day or date.today())
    total_weeks = max(1, ((cfg.race_day - start).days // 7) + 1)
    targets = week_targets(cfg, total_weeks)
    goal_pace = target_pace_s(cfg.goal_time_sec, cfg.distance_km)
    paces = paces_from_goal(goal_pace)

    all_sessions: list[dict] = []
    for i, target in enumerate(targets):
        ws = start + timedelta(weeks=i)
        weeks_out = (cfg.race_day - ws).days / 7
        if weeks_out < -0.5:
            continue
        all_sessions.extend(build_week(cfg, ws, target, weeks_out, paces))

    all_sessions = [s for s in all_sessions if cfg.start_day <= s["day"] <= cfg.race_day]

    with db.tx():
        if replace:
            db.run(
                "DELETE FROM planned_session WHERE day >= ? AND day <= ? AND source = 'generator'",
                (cfg.start_day.isoformat(), cfg.race_day.isoformat()),
            )
        for s in all_sessions:
            db.run(
                """
                INSERT INTO planned_session
                    (day, kind, title, distance_km, duration_min, target_pace_s,
                     description, source, updated_at)
                VALUES (?,?,?,?,?,?,?, 'generator', datetime('now'))
                """,
                (
                    s["day"].isoformat(), s["kind"], s["title"], s.get("distance_km"),
                    s.get("duration_min"), s.get("target_pace_s"), s.get("description"),
                ),
            )

    return {
        "weeks": len(targets),
        "sessions": len(all_sessions),
        "peak_weekly_km": cfg.peak_weekly_km,
        "goal_pace_s_km": goal_pace,
        "from": cfg.start_day.isoformat(),
        "to": cfg.race_day.isoformat(),
    }


# --------------------------------------------------------------------------
# Lesen und Abgleich mit dem tatsächlichen Training
# --------------------------------------------------------------------------


def read_plan(start: date, end: date) -> list[dict]:
    rows = db.q(
        "SELECT * FROM planned_session WHERE day >= ? AND day <= ? ORDER BY day, id",
        (start.isoformat(), end.isoformat()),
    )
    return [dict(r) for r in rows]


def match_completed(start: date, end: date) -> int:
    """Verknüpft geplante Laufeinheiten mit tatsächlich gelaufenen Aktivitäten
    desselben Tages, damit man Plan gegen Realität sieht."""
    updated = 0
    with db.tx():
        rows = db.q(
            "SELECT id, day, kind FROM planned_session "
            "WHERE day >= ? AND day <= ? AND status = 'planned' AND workout_id IS NULL",
            (start.isoformat(), end.isoformat()),
        )
        for r in rows:
            sport = "strength" if r["kind"] == "strength" else "run"
            match = db.q1(
                "SELECT id FROM workout WHERE day = ? AND sport = ? "
                "AND id NOT IN (SELECT workout_id FROM planned_session WHERE workout_id IS NOT NULL) "
                "ORDER BY duration_sec DESC LIMIT 1",
                (r["day"], sport),
            )
            if match:
                db.run(
                    "UPDATE planned_session SET workout_id = ?, status = 'done', "
                    "updated_at = datetime('now') WHERE id = ?",
                    (match["id"], r["id"]),
                )
                updated += 1
            elif r["day"] < date.today().isoformat():
                db.run(
                    "UPDATE planned_session SET status = 'skipped', updated_at = datetime('now') "
                    "WHERE id = ?",
                    (r["id"],),
                )
    return updated


def week_compare(ws: date) -> dict:
    """Plan gegen Realität für eine Woche."""
    we = ws + timedelta(days=6)
    planned = read_plan(ws, we)
    planned_km = sum(p["distance_km"] or 0 for p in planned if p["kind"] != "strength")
    row = db.q1(
        "SELECT COALESCE(SUM(distance_m),0) AS m, COUNT(*) AS n FROM workout "
        "WHERE sport = 'run' AND day >= ? AND day <= ?",
        (ws.isoformat(), we.isoformat()),
    )
    actual_km = round((row["m"] or 0) / 1000.0, 1) if row else 0.0
    return {
        "week_start": ws.isoformat(),
        "planned_km": round(planned_km, 1),
        "actual_km": actual_km,
        "planned_sessions": len(planned),
        "done": sum(1 for p in planned if p["status"] == "done"),
        "skipped": sum(1 for p in planned if p["status"] == "skipped"),
        "actual_runs": row["n"] if row else 0,
    }
