"""
MCP-Server — der Zugang für Claude.

Grundsatz für jedes Tool hier: **kompakt antworten**. Ein Chat, der 200 rohe
Aktivitäten als JSON bekommt, verbrennt fünfstellig viele Tokens für eine
Frage, die mit drei Zeilen beantwortet wäre. Deshalb rechnet der Server die
Auswertung selbst und gibt fertigen Text zurück. `trainingsstand` kostet so
rund 300 Tokens statt 30.000.

Die Tools sind auf Deutsch benannt, damit Claude sie im Gespräch natürlich
trifft ("wie schaut mein Trainingsstand aus?").
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from . import analysis, db, plan
from .analysis import fmt_pace, fmt_time

log = logging.getLogger("mcp")

mcp = FastMCP(
    name="marathon-dashboard",
    instructions=(
        "Zugriff auf die Garmin-Trainingsdaten und den Marathon-Trainingsplan des Nutzers. "
        "Beginne bei Trainingsfragen immer mit `trainingsstand` — das liefert Formstand, "
        "Warnungen und Rennprognose in einem Aufruf. Nutze `plan_schreiben`, um die "
        "kommende Woche zu planen; bestehende Einheiten im selben Zeitraum werden dabei "
        "ersetzt. Sei bei Trainingsempfehlungen konkret (Distanz, Pace, Tag) und beachte "
        "die Warnungen aus `trainingsstand`."
    ),
)


def _km(value: float | None) -> str:
    return f"{value:g} km" if value else "–"


# --------------------------------------------------------------------------
# Lesen
# --------------------------------------------------------------------------


@mcp.tool
def trainingsstand() -> str:
    """Gesamtbild in einem Aufruf: Wochen bis zum Rennen, Trainingsphase, Umfang,
    Belastung, Rennprognose, Zielabgleich und aktuelle Warnungen.

    Das ist der richtige erste Aufruf für fast jede Trainingsfrage."""
    s = analysis.training_state()
    lines: list[str] = []

    race = s["race"]
    if race:
        lines.append(
            f"RENNEN: {race['name']} am {race['day']} — noch {s['days_to_race']} Tage "
            f"({s['weeks_to_race']} Wochen). Phase: {s['phase']}."
        )
        lines.append(f"  {s['phase_hint']}")
    else:
        lines.append("RENNEN: noch keines hinterlegt (Tool `ziel_setzen` verwenden).")

    lines.append(
        f"\nUMFANG: diese Woche {s['this_week_km']} km, Vorwoche {s['last_week_km']} km, "
        f"4-Wochen-Schnitt {s['avg_4w_km']} km. Längster Lauf (4 Wochen): {s['longest_4w_km']} km."
    )
    if s["ramp_pct"] is not None:
        lines.append(f"  Steigerung zur Vorwoche: {s['ramp_pct']:+.1f} % (Richtwert: max. +10 %)")
    if s["acwr"] is not None:
        lines.append(f"  Belastungsverhältnis ACWR: {s['acwr']} (grün 0,8–1,3)")
    if s["intensity"]:
        i = s["intensity"]
        lines.append(f"  Intensität 4 Wochen: {i['easy_pct']} % locker / {i['hard_pct']} % hart "
                     f"(Ziel rund 80/20, Basis {i['hours']} h)")

    p = s["prediction"]
    lines.append(
        f"\nPROGNOSE MARATHON: {fmt_time(p['combined_sec'])} (Verlässlichkeit: {p['confidence']})"
    )
    if p["garmin_sec"]:
        lines.append(f"  Garmin schätzt {fmt_time(p['garmin_sec'])}")
    if p["riegel_sec"]:
        lines.append(f"  Hochrechnung aus echten Läufen: {fmt_time(p['riegel_sec'])}")
    lines.append(f"  Längster Lauf der letzten 90 Tage: {p['longest_run_km']} km")

    g = s["goal"]
    if g:
        lines.append(
            f"\nZIEL: {fmt_time(g['goal_sec'])} → Zielpace {fmt_pace(g['goal_pace_s_km'])}"
        )
        if g["gap_sec"] is not None:
            lines.append(f"  Abstand Prognose zu Ziel: {g['gap_sec']:+d} s")
        lines.append(f"  {g['verdict']}")

    health = []
    if s["vo2max"]:
        health.append(f"VO2max {s['vo2max']:.0f}")
    if s["readiness"]:
        health.append(f"Readiness {s['readiness']:.0f}/100")
    for label, key in (("HRV", "hrv_trend"), ("Ruhepuls", "rhr_trend")):
        t = s[key]
        if t and t["delta_pct"] is not None:
            health.append(f"{label} {t['recent']:g} ({t['delta_pct']:+.0f} %)")
    if s["sleep_trend"]:
        health.append(f"Schlaf {s['sleep_trend']['recent'] / 60:.1f} h")
    if health:
        lines.append("\nGESUNDHEIT: " + " · ".join(health))

    if s["warnings"]:
        lines.append("\nWARNUNGEN:")
        for w in s["warnings"]:
            lines.append(f"  [{w['level']}] {w['title']}: {w['text']}")
    else:
        lines.append("\nWARNUNGEN: keine.")

    return "\n".join(lines)


@mcp.tool
def wochen(
    anzahl: Annotated[int, Field(ge=1, le=26, description="Wie viele Wochen zurück")] = 8,
) -> str:
    """Wochenübersicht: geplante gegen tatsächliche Kilometer, Anzahl Läufe,
    längster Lauf. Eine Zeile pro Woche."""
    vols = analysis.weekly_volume(anzahl)
    out = ["Woche ab  | km    | Läufe | längster | geplant | erledigt/verpasst"]
    for v in vols:
        ws = date.fromisoformat(v["week_start"])
        cmp_ = plan.week_compare(ws)
        out.append(
            f"{v['week_start']} | {v['km']:>5.1f} | {v['runs']:>6} | {v['longest_km']:>8.1f} | "
            f"{cmp_['planned_km']:>7.1f} | {cmp_['done']}/{cmp_['skipped']}"
        )
    return "\n".join(out)


@mcp.tool
def laeufe(
    anzahl: Annotated[int, Field(ge=1, le=50, description="Anzahl der letzten Läufe")] = 10,
) -> str:
    """Die letzten Läufe, eine Zeile je Lauf: Datum, Distanz, Zeit, Pace,
    Durchschnittspuls, aerobe Entkopplung."""
    runs = analysis.recent_runs(anzahl)
    if not runs:
        return "Keine Läufe in der Datenbank. Läuft der Garmin-Sync?"
    out = ["ID  | Datum      | km    | Zeit    | Pace     | HF  | Entk. | Name"]
    for r in runs:
        out.append(
            f"{r['id']:<3} | {r['day']} | {r['km']:>5.2f} | {fmt_time(r['duration_sec']):>7} | "
            f"{fmt_pace(r['pace_s_km']):>8} | {r['avg_hr'] or '–':>3} | "
            f"{(str(r['decoupling_pct']) + '%') if r['decoupling_pct'] is not None else '–':>5} | "
            f"{(r['name'] or '')[:28]}"
        )
    out.append("\nEntk. = aerobe Entkopplung. Unter 5 % heißt: die Grundlagenausdauer trägt die Dauer.")
    return "\n".join(out)


@mcp.tool
def lauf(lauf_id: Annotated[int, Field(description="ID aus der Liste von `laeufe`")]) -> str:
    """Detail zu einem Lauf inklusive Kilometer-Splits — für die Frage, ob eine
    Einheit wirklich so gelaufen wurde wie geplant."""
    row = db.q1(
        "SELECT w.*, d.laps, d.decoupling_pct, d.pace_drift_pct FROM workout w "
        "LEFT JOIN workout_detail d ON d.workout_id = w.id WHERE w.id = ?",
        (lauf_id,),
    )
    if row is None:
        return f"Kein Lauf mit ID {lauf_id}."

    km = (row["distance_m"] or 0) / 1000.0
    out = [
        f"{row['name'] or 'Lauf'} — {row['day']}",
        f"{km:.2f} km in {fmt_time(row['duration_sec'])} "
        f"({fmt_pace(row['duration_sec'] / km if km > 0.3 else None)})",
    ]
    if row["avg_hr"]:
        out.append(f"Puls: Schnitt {row['avg_hr']:.0f}, Maximum {row['max_hr'] or 0:.0f}")
    if row["decoupling_pct"] is not None:
        out.append(f"Aerobe Entkopplung: {row['decoupling_pct']} % "
                   f"({'gut abgesichert' if row['decoupling_pct'] < 5 else 'Grundlage reicht noch nicht'})")
    zones = db.jloads(row["hr_zones"])
    if isinstance(zones, dict):
        out.append("HF-Zonen (min): " + ", ".join(
            f"{k}={round(v / 60)}" for k, v in sorted(zones.items()) if v
        ))
    if row["rpe"]:
        out.append(f"Subjektiv: RPE {row['rpe']}/10" + (f", Gefühl {row['feeling']}/10" if row["feeling"] else ""))
    if row["notes"]:
        out.append(f"Notiz: {row['notes']}")

    laps = db.jloads(row["laps"])
    if isinstance(laps, list) and laps:
        out.append("\nSplits:")
        for lap in laps[:45]:
            out.append(
                f"  {lap.get('n'):>2}. {fmt_pace(lap.get('pace_s_km')):>8}"
                f"  HF {lap.get('avg_hr') or '–'}"
            )
    return "\n".join(out)


@mcp.tool
def gesundheit(
    tage: Annotated[int, Field(ge=3, le=90, description="Zeitraum in Tagen")] = 14,
) -> str:
    """Erholungswerte im Verlauf: Schlaf, HRV, Ruhepuls, Readiness. Kompakt als
    Tagesreihe — die Grundlage für die Frage, ob eine harte Einheit heute Sinn hat."""
    today = date.today()
    since = today - timedelta(days=tage)
    kinds = [
        ("sleep_minutes", "Schlaf h", lambda v: f"{v / 60:.1f}"),
        ("hrv_overnight", "HRV", lambda v: f"{v:.0f}"),
        ("rhr", "Ruhepuls", lambda v: f"{v:.0f}"),
        ("training_readiness", "Readiness", lambda v: f"{v:.0f}"),
    ]
    series = {k: dict(db.metric_series(k, since, today)) for k, _, _ in kinds}
    days = sorted({d for s in series.values() for d in s}, reverse=True)
    if not days:
        return "Keine Gesundheitsdaten. Läuft der Garmin-Sync?"

    out = ["Datum      | " + " | ".join(label for _, label, _ in kinds)]
    for d in days[:tage]:
        cells = []
        for kind, _, fmt in kinds:
            v = series[kind].get(d)
            cells.append(fmt(v) if v is not None else "–")
        out.append(f"{d} | " + " | ".join(cells))

    for kind, label, _ in kinds:
        t = analysis.trend(kind)
        if t and t["delta_pct"] is not None:
            out.append(f"{label}: 7-Tage-Schnitt {t['recent']:g} vs. Basis {t['baseline']:g} "
                       f"({t['delta_pct']:+.0f} %)")
    return "\n".join(out)


@mcp.tool
def plan_lesen(
    von: Annotated[str, Field(description="Startdatum ISO, z. B. 2026-09-01")] = "",
    bis: Annotated[str, Field(description="Enddatum ISO")] = "",
) -> str:
    """Geplante Einheiten in einem Zeitraum. Ohne Angabe: die nächsten 14 Tage."""
    start = date.fromisoformat(von) if von else date.today()
    end = date.fromisoformat(bis) if bis else start + timedelta(days=14)
    sessions = plan.read_plan(start, end)
    if not sessions:
        return f"Keine geplanten Einheiten zwischen {start} und {end}."

    out = [f"Plan {start} bis {end}:"]
    for s in sessions:
        bits = [f"{s['day']} [{s['kind']}] {s['title']}"]
        if s["distance_km"]:
            bits.append(_km(s["distance_km"]))
        if s["target_pace_s"]:
            bits.append(fmt_pace(s["target_pace_s"]))
        if s["status"] != "planned":
            bits.append(f"({s['status']})")
        out.append("  " + " · ".join(bits))
        if s["description"]:
            out.append(f"      {s['description']}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Schreiben
# --------------------------------------------------------------------------


@mcp.tool
def plan_schreiben(
    einheiten: Annotated[
        list[dict[str, Any]],
        Field(description=(
            "Liste von Einheiten. Pflichtfelder je Eintrag: `datum` (ISO), `art` "
            "(easy|long|tempo|interval|race_pace|recovery|rest|strength|cross) und `titel`. "
            "Optional: `km` (Zahl), `minuten` (Zahl), `pace` (Sekunden pro km) und "
            "`beschreibung` (Text)."
        )),
    ],
    ersetzen: Annotated[bool, Field(description=(
        "True ersetzt alle vorhandenen Einheiten im betroffenen Zeitraum. "
        "False hängt die neuen Einheiten zusätzlich an."
    ))] = True,
) -> str:
    """Schreibt geplante Einheiten in den Trainingsplan — so plant Claude die Woche.

    Vor dem Planen `trainingsstand` aufrufen und die Warnungen beachten."""
    if not einheiten:
        return "Keine Einheiten übergeben."

    parsed = []
    for raw in einheiten:
        day_str = str(raw.get("datum") or raw.get("day") or "")
        try:
            day = date.fromisoformat(day_str)
        except ValueError:
            return f"Ungültiges Datum: {day_str!r}. Format ist YYYY-MM-DD."
        kind = str(raw.get("art") or raw.get("kind") or "easy")
        if kind not in plan.KIND_LABELS:
            return (f"Unbekannte Art {kind!r}. Erlaubt: {', '.join(plan.KIND_LABELS)}.")
        title = str(raw.get("titel") or raw.get("title") or plan.KIND_LABELS[kind])
        parsed.append({
            "day": day,
            "kind": kind,
            "title": title[:160],
            "km": raw.get("km") or raw.get("distance_km"),
            "minutes": raw.get("minuten") or raw.get("duration_min"),
            "pace": raw.get("pace") or raw.get("target_pace_s"),
            "description": (raw.get("beschreibung") or raw.get("description") or None),
        })

    lo = min(p["day"] for p in parsed)
    hi = max(p["day"] for p in parsed)

    with db.tx():
        if ersetzen:
            db.run("DELETE FROM planned_session WHERE day >= ? AND day <= ? AND status = 'planned'",
                   (lo.isoformat(), hi.isoformat()))
        for p in parsed:
            db.run(
                "INSERT INTO planned_session (day, kind, title, distance_km, duration_min, "
                "target_pace_s, description, source, updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'claude', datetime('now'))",
                (p["day"].isoformat(), p["kind"], p["title"], p["km"], p["minutes"],
                 p["pace"], p["description"]),
            )

    return (f"{len(parsed)} Einheiten von {lo} bis {hi} gespeichert"
            f"{' (vorherige ersetzt)' if ersetzen else ''}.")


@mcp.tool
def plan_generieren(
    wochen_km_jetzt: Annotated[float, Field(gt=0, le=200, description="Aktueller Wochenumfang in km")],
    lauftage: Annotated[str, Field(description=(
        "Wochentage zum Laufen, kommagetrennt: mo,di,mi,do,fr,sa,so"
    ))] = "di,do,sa,so",
    peak_km: Annotated[float | None, Field(description="Maximaler Wochenumfang, sonst automatisch")] = None,
) -> str:
    """Erzeugt einen kompletten periodisierten Plan bis zum Renntag (Grundlage,
    Aufbau, Peak, Taper) und schreibt ihn in die Datenbank.

    Einmal am Anfang aufrufen. Danach die Wochen mit `plan_schreiben` feinjustieren —
    das überschreibt den Generator nicht dauerhaft."""
    race = analysis.active_race()
    if not race:
        return "Kein Rennen hinterlegt. Zuerst `ziel_setzen` aufrufen."

    names = ["mo", "di", "mi", "do", "fr", "sa", "so"]
    days = [names.index(t.strip().lower()[:2]) for t in lauftage.split(",")
            if t.strip().lower()[:2] in names]
    if not days:
        return f"Keine gültigen Lauftage in {lauftage!r}. Beispiel: di,do,sa,so"

    cfg = plan.PlanConfig(
        race_day=date.fromisoformat(race["day"]),
        goal_time_sec=race.get("goal_time_sec"),
        distance_km=race.get("distance_km") or 42.195,
        start_weekly_km=wochen_km_jetzt,
        peak_weekly_km=peak_km,
        run_days=days,
        long_run_day=max(days),
    )
    result = plan.generate(cfg)
    return (
        f"Plan erstellt: {result['weeks']} Wochen, {result['sessions']} Einheiten "
        f"({result['from']} bis {result['to']}). Peak-Umfang {result['peak_weekly_km']:g} km/Woche"
        + (f", Zielpace {fmt_pace(result['goal_pace_s_km'])}." if result["goal_pace_s_km"] else ".")
    )


@mcp.tool
def ziel_setzen(
    name: Annotated[str, Field(description="Name des Rennens")],
    datum: Annotated[str, Field(description="Renntag im Format YYYY-MM-DD")],
    zielzeit: Annotated[str, Field(description="Zielzeit, z. B. '3:45' oder '3:45:00'")] = "",
    distanz_km: Annotated[float, Field(gt=0, le=500)] = 42.195,
) -> str:
    """Legt das Zielrennen an oder aktualisiert es (Renntag, Zielzeit, Distanz)."""
    try:
        day = date.fromisoformat(datum)
    except ValueError:
        return f"Ungültiges Datum {datum!r}. Format ist YYYY-MM-DD."

    goal_sec = analysis.parse_time(zielzeit) if zielzeit else None
    if zielzeit and goal_sec is None:
        return f"Zielzeit {zielzeit!r} nicht verstanden. Beispiele: '3:45' oder '3:45:00'."

    existing = db.q1("SELECT id FROM race WHERE day = ? AND name = ?", (day.isoformat(), name))
    if existing:
        db.run("UPDATE race SET goal_time_sec = ?, distance_km = ? WHERE id = ?",
               (goal_sec, distanz_km, existing["id"]))
    else:
        db.run("INSERT INTO race (name, day, distance_km, goal_time_sec) VALUES (?,?,?,?)",
               (name, day.isoformat(), distanz_km, goal_sec))

    weeks = round((day - date.today()).days / 7, 1)
    pace = fmt_pace(goal_sec / distanz_km) if goal_sec else "–"
    return (f"{name} am {day} gespeichert ({weeks} Wochen hin). "
            f"Zielzeit {fmt_time(goal_sec)}, Zielpace {pace}.")


@mcp.tool
def notiz(
    text: Annotated[str, Field(min_length=1, max_length=4000, description="Der Notiztext")],
    datum: Annotated[str, Field(description="Tag im Format YYYY-MM-DD, sonst heute")] = "",
    art: Annotated[str, Field(description="journal, coach oder injury")] = "journal",
) -> str:
    """Hält etwas im Trainingstagebuch fest — wie sich eine Einheit angefühlt hat,
    ein Zwicken im Knie, eine Entscheidung. Taucht im Dashboard beim jeweiligen Tag auf."""
    try:
        day = date.fromisoformat(datum) if datum else date.today()
    except ValueError:
        return f"Ungültiges Datum {datum!r}."
    kind = art if art in {"journal", "coach", "injury"} else "journal"
    db.run("INSERT INTO note (day, kind, text) VALUES (?,?,?)", (day.isoformat(), kind, text))
    return f"Notiz zum {day} gespeichert."


@mcp.tool
def einheit_bewerten(
    lauf_id: Annotated[int, Field(description="ID aus `laeufe`")],
    rpe: Annotated[int | None, Field(ge=1, le=10, description="Anstrengung 1-10")] = None,
    gefühl: Annotated[int | None, Field(ge=1, le=10, description="Gefühl 1-10")] = None,
    notiz_text: Annotated[str, Field(max_length=2000)] = "",
) -> str:
    """Ergänzt einen Lauf um die subjektive Bewertung. Garmin misst den Puls,
    aber nicht, ob sich die Einheit schwer angefühlt hat — genau das macht den
    Unterschied zwischen Daten und Trainingssteuerung."""
    row = db.q1("SELECT id FROM workout WHERE id = ?", (lauf_id,))
    if row is None:
        return f"Kein Lauf mit ID {lauf_id}."
    db.run(
        "UPDATE workout SET rpe = COALESCE(?, rpe), feeling = COALESCE(?, feeling), "
        "notes = COALESCE(NULLIF(?, ''), notes), updated_at = datetime('now') WHERE id = ?",
        (rpe, gefühl, notiz_text, lauf_id),
    )
    return f"Lauf {lauf_id} bewertet."
