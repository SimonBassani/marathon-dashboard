"""
Diagramme als serverseitig erzeugtes SVG.

Keine Chart-Bibliothek: das spart rund 200 KB JavaScript pro Seitenaufruf, lädt
am Handy sofort und nimmt eine Abhängigkeit aus der Lieferkette. Die Diagramme
sind bewusst schlicht — Balken für Wochenumfang, Linie für Verlaeufe.

Zwei Dinge, die hier bewusst so sind:

  * Die SVGs strecken sich (`preserveAspectRatio="none"`), damit sie jede
    Bildschirmbreite füllen. Deshalb steht KEIN Text im SVG — gestreckter Text
    sieht auf dem Handy zerquetscht aus. Beschriftungen kommen als HTML daneben.
  * Jeder Wert geht durch `_n()` und wird zu einer Zahl. Es landet also nie
    Nutzertext im Markup.
"""
from __future__ import annotations

import html
from datetime import date
from typing import Sequence

# Farben aus static/app.css gespiegelt, damit Diagramm und Seite zusammenpassen.
MUTED = "#8b93a1"
ACCENT = "#ff5a1f"
ACCENT_SOFT = "#ff5a1f55"
GRID = "#252a34"


def _n(value: float | int | None, default: float = 0.0) -> float:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return f if f == f and f not in (float("inf"), float("-inf")) else default


def _esc(text: object) -> str:
    return html.escape(str(text))


def _figure(svg: str, css_class: str, left: str = "", right: str = "", top: str = "") -> str:
    """Setzt das SVG mit HTML-Beschriftungen zusammen."""
    caption = ""
    if left or right:
        caption = (f'<figcaption class="chart-axis"><span>{_esc(left)}</span>'
                   f'<span>{_esc(right)}</span></figcaption>')
    head = f'<p class="chart-top">{_esc(top)}</p>' if top else ""
    return f'<figure class="chart-wrap">{head}{svg}{caption}</figure>'


def volume_chart(weekly: Sequence[dict], height: int = 100) -> str:
    """Balkendiagramm der Wochenkilometer. Die laufende Woche wird blasser
    gezeichnet, weil sie noch nicht fertig ist."""
    if not weekly:
        return _empty("Noch keine Laufdaten")

    width = 100.0
    n = len(weekly)
    gap = 1.4
    bar_w = max(1.0, (width - gap * (n - 1)) / n)
    top = max((_n(w.get("km")) for w in weekly), default=0.0) or 1.0

    bars = []
    for i, w in enumerate(weekly):
        h = max(0.8, _n(w.get("km")) / top * height)
        x = i * (bar_w + gap)
        fill = ACCENT_SOFT if i == n - 1 else ACCENT
        bars.append(
            f'<rect x="{x:.2f}" y="{height - h:.2f}" width="{bar_w:.2f}" height="{h:.2f}" '
            f'rx="0.6" fill="{fill}"><title>{_esc(_label(w))}</title></rect>'
        )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'class="chart chart-bars" role="img" aria-label="Wochenkilometer je Woche">'
        + "".join(bars) + "</svg>"
    )
    return _figure(
        svg, "bars",
        left=_short_date(weekly[0].get("week_start")),
        right=_short_date(weekly[-1].get("week_start")),
        top=f"höchste Woche {top:.0f} km",
    )


def metric_chart(series: Sequence[tuple[str, float]], label: str, unit: str = "",
                 height: int = 100) -> str:
    """Liniendiagramm für einen Messwertverlauf mit 7-Tage-Mittel."""
    points = [(d, _n(v)) for d, v in series if v is not None]
    if len(points) < 2:
        return _empty(f"{label}: noch zu wenig Daten")

    width = 100.0
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = width / (len(points) - 1)

    def y_of(v: float) -> float:
        # 4 Einheiten Luft oben und unten, damit die Linie nicht am Rand klebt.
        return height - 4 - (v - lo) / span * (height - 8)

    line = " ".join(f"{i * step:.2f},{y_of(v):.2f}" for i, (_, v) in enumerate(points))

    # Gleitendes 7-Tage-Mittel — der Verlauf ist aussagekräftiger als der Tageswert.
    smooth = " ".join(
        f"{i * step:.2f},{y_of(sum(values[max(0, i - 6): i + 1]) / len(values[max(0, i - 6): i + 1])):.2f}"
        for i in range(len(points))
    )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'class="chart chart-line" role="img" aria-label="Verlauf {_esc(label)}">'
        f'<polygon points="0,{height} {line} {width:.2f},{height}" fill="{ACCENT}" opacity="0.10"/>'
        f'<polyline points="{line}" fill="none" stroke="{ACCENT}" stroke-width="0.6"'
        f' opacity="0.4" vector-effect="non-scaling-stroke"/>'
        f'<polyline points="{smooth}" fill="none" stroke="{ACCENT}" stroke-width="2.5"'
        f' stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
        "</svg>"
    )
    return _figure(
        svg, "line",
        left=f"tiefster {lo:.0f}{unit}",
        right=f"zuletzt {values[-1]:.0f}{unit}",
        top=f"höchster {hi:.0f}{unit}",
    )


def lap_chart(laps: Sequence[dict], height: int = 100) -> str:
    """Pace je Kilometer als Balken. Schneller heißt höher — so sieht ein
    starker Kilometer auch nach mehr aus."""
    paces = [_n(lap.get("pace_s_km")) for lap in laps if lap.get("pace_s_km")]
    if len(paces) < 2:
        return _empty("Keine Splits vorhanden")

    width = 100.0
    n = len(paces)
    gap = 0.8
    bar_w = max(0.8, (width - gap * (n - 1)) / n)
    slowest, fastest = max(paces), min(paces)
    span = (slowest - fastest) or 1.0

    bars = []
    for i, pace in enumerate(paces):
        rel = (slowest - pace) / span
        h = max(6.0, 10 + rel * (height - 10))
        bars.append(
            f'<rect x="{i * (bar_w + gap):.2f}" y="{height - h:.2f}" width="{bar_w:.2f}" '
            f'height="{h:.2f}" rx="0.5" fill="{ACCENT}" opacity="{0.5 + 0.5 * rel:.2f}"/>'
        )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'class="chart chart-laps" role="img" aria-label="Pace je Kilometer">'
        + "".join(bars) + "</svg>"
    )
    return _figure(svg, "laps", left="km 1", right=f"km {n}",
                   top="höher = schneller")


def _label(week: dict) -> str:
    return (f"{_short_date(week.get('week_start'))}: {_n(week.get('km')):.1f} km, "
            f"{int(_n(week.get('runs')))} Läufe")


def _short_date(iso: object) -> str:
    try:
        d = date.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return ""
    return d.strftime("%d.%m.")


def _empty(message: str) -> str:
    return f'<p class="chart-empty">{_esc(message)}</p>'
