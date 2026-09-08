"""
Der ganze Server in einem Prozess: Dashboard, Garmin-Sync und MCP-Endpunkt.

  /            Weboberfläche (Passwort-Login, Sitzungs-Cookie)
  /mcp         MCP über Streamable HTTP, geschützt per OAuth-Bearer-Token
  /oauth/*     OAuth-2.1-Server für den Claude-Connector (app/oauth.py)
  /healthz     Zustandsprüfung für Docker

Warum alles in einem Prozess: ein Nutzer, eine SQLite-Datei. Jeder weitere
Container wäre Betriebsaufwand ohne Gegenwert — und jede Komponente, die es
nicht gibt, kann auch nicht verwundbar werden.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import analysis, db, oauth, plan, views
from .mcp_server import mcp
from .scheduler import Scheduler
from .security import (
    SESSION_COOKIE,
    GlobalLimiter,
    RateLimiter,
    check_password,
    cookie_secure,
    new_session_cookie,
    valid_session,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.filters["time"] = analysis.fmt_time
templates.env.filters["pace"] = analysis.fmt_pace

# Der MCP-Endpunkt bringt seinen eigenen Lebenszyklus mit (Sitzungsverwaltung).
mcp_app = mcp.http_app(path="/")
scheduler = Scheduler()

_login_limiter = RateLimiter(limit=10, window_sec=300)
_global_limiter = GlobalLimiter(limit=60, window_sec=300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("Datenbank bereit: %s", db.DB_PATH)
    async with mcp_app.lifespan(app):
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()


app = FastAPI(title="Marathon-Dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)


# --------------------------------------------------------------------------
# Sicherheits-Middleware
# --------------------------------------------------------------------------


class SecurityMiddleware(BaseHTTPMiddleware):
    """Setzt Schutz-Header und bewacht den MCP-Endpunkt.

    Der 401 auf /mcp trägt bewusst einen `WWW-Authenticate`-Header mit Verweis
    auf die Ressourcen-Metadaten (RFC 9728). Genau daran erkennt Claude, wo der
    Autorisierungsserver liegt, und startet den OAuth-Ablauf von selbst.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path == "/mcp" or path.startswith("/mcp/"):
            if not oauth.verify_bearer(request.headers.get("authorization")):
                base = os.environ.get("PUBLIC_URL", "").rstrip("/")
                return JSONResponse(
                    {"error": "invalid_token"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer resource_metadata="{base}/.well-known/'
                            f'oauth-protected-resource"'
                        )
                    },
                )

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Die Oberfläche lädt ausschließlich eigene Dateien — keine CDNs,
        # kein Inline-Skript außer dem eigenen mit Nonce-freiem 'self'.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        return response


app.add_middleware(SecurityMiddleware)
app.include_router(oauth.router)
app.mount("/mcp", mcp_app)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def _require_login(request: Request) -> bool:
    return valid_session(request.cookies.get(SESSION_COOKIE))


def _redirect_to_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={request.url.path}", status_code=302)


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@app.post("/login")
def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
    client_ip = request.client.host if request.client else "unknown"
    if not _login_limiter.allow(client_ip) or not _global_limiter.allow():
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next, "error": "Zu viele Versuche. Bitte in fünf Minuten erneut."},
            status_code=429,
        )
    if not check_password(password):
        log.warning("Fehlgeschlagener Dashboard-Login von %s", client_ip)
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": "Passwort stimmt nicht."},
            status_code=401,
        )

    _login_limiter.reset(client_ip)
    _global_limiter.reset()
    # `next` darf nur ein Pfad auf diesem Server sein, sonst ist es eine
    # offene Weiterleitung, die man für Phishing missbrauchen kann.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=302)
    response.set_cookie(
        SESSION_COOKIE, new_session_cookie(),
        httponly=True, secure=cookie_secure(), samesite="lax",
        max_age=60 * 60 * 24 * 30, path="/",
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------
# Oberfläche
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not _require_login(request):
        return _redirect_to_login(request)
    state = analysis.training_state()

    # Die laufende Woche auf einen Blick — Montag bis Sonntag, geplant gegen
    # gelaufen. Steht bewusst auch hier und nicht nur auf dem Plan-Tab: das ist
    # die Frage, die man sich morgens stellt.
    heute = date.today()
    ws = analysis.week_start(heute)
    plan.match_completed(ws, heute)
    week_by_day: dict[str, list[dict]] = {}
    for s in plan.read_plan(ws, ws + timedelta(days=6)):
        week_by_day.setdefault(s["day"], []).append(s)
    week_days = [
        {
            "day": (d := (ws + timedelta(days=i))).isoformat(),
            "label": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")[i],
            "num": d.day,
            "is_today": d == heute,
            "is_past": d < heute,
            "sessions": week_by_day.get(d.isoformat(), []),
        }
        for i in range(7)
    ]

    return templates.TemplateResponse(
        request, "home.html",
        {
            "s": state,
            "chart": views.volume_chart(state["weekly"]),
            "today_sessions": plan.read_plan(heute, heute + timedelta(days=2)),
            "week_days": week_days,
            "week_compare": plan.week_compare(ws),
            "labels": plan.KIND_LABELS,
            "last_sync": db.q1("SELECT * FROM sync_run ORDER BY id DESC LIMIT 1"),
            "nav": "home",
        },
    )


@app.get("/laeufe", response_class=HTMLResponse)
def runs_page(request: Request, limit: int = 30):
    if not _require_login(request):
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request, "runs.html",
        {"runs": analysis.recent_runs(min(max(limit, 5), 100)), "nav": "runs"},
    )


@app.get("/lauf/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    if not _require_login(request):
        return _redirect_to_login(request)
    row = db.q1(
        "SELECT w.*, d.laps, d.decoupling_pct, d.pace_drift_pct FROM workout w "
        "LEFT JOIN workout_detail d ON d.workout_id = w.id WHERE w.id = ?",
        (run_id,),
    )
    if row is None:
        return HTMLResponse("<h1>Lauf nicht gefunden</h1>", status_code=404)
    laps = db.jloads(row["laps"]) or []
    return templates.TemplateResponse(
        request, "run_detail.html",
        {"w": row, "laps": laps, "lap_chart": views.lap_chart(laps), "nav": "runs"},
    )


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request, weeks: int = 6):
    if not _require_login(request):
        return _redirect_to_login(request)
    start = analysis.week_start(date.today())
    end = start + timedelta(weeks=min(max(weeks, 1), 30), days=-1)
    plan.match_completed(start - timedelta(weeks=4), date.today())
    sessions = plan.read_plan(start, end)

    by_week: dict[str, list[dict]] = {}
    for s in sessions:
        ws = analysis.week_start(date.fromisoformat(s["day"])).isoformat()
        by_week.setdefault(ws, []).append(s)

    return templates.TemplateResponse(
        request, "plan.html",
        {
            "by_week": by_week,
            "compare": {ws: plan.week_compare(date.fromisoformat(ws)) for ws in by_week},
            "labels": plan.KIND_LABELS,
            "today": date.today().isoformat(),
            "nav": "plan",
        },
    )


@app.get("/gesundheit", response_class=HTMLResponse)
def health_page(request: Request, days: int = 30):
    if not _require_login(request):
        return _redirect_to_login(request)
    days = min(max(days, 7), 180)
    since = date.today() - timedelta(days=days)
    charts = {
        key: views.metric_chart(db.metric_series(key, since), label, unit)
        for key, label, unit in (
            ("hrv_overnight", "HRV über Nacht", "ms"),
            ("rhr", "Ruhepuls", "bpm"),
            ("sleep_minutes", "Schlaf", "min"),
            ("training_readiness", "Readiness", ""),
            ("vo2max", "VO2max", ""),
        )
    }
    return templates.TemplateResponse(
        request, "health.html",
        {"charts": charts, "days": days, "notes": db.q(
            "SELECT * FROM note WHERE day >= ? ORDER BY day DESC, id DESC LIMIT 30",
            (since.isoformat(),)), "nav": "health"},
    )


@app.get("/einstellungen", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = ""):
    if not _require_login(request):
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "race": analysis.active_race(),
            "public_url": os.environ.get("PUBLIC_URL", ""),
            "clients": db.q("SELECT client_id, client_name, created_at FROM oauth_client ORDER BY created_at DESC"),
            "saved": saved,
            "nav": "settings",
        },
    )


@app.post("/einstellungen/rennen")
def save_race(
    request: Request,
    name: str = Form(...),
    day: str = Form(...),
    goal_time: str = Form(""),
    distance_km: float = Form(42.195),
):
    if not _require_login(request):
        return _redirect_to_login(request)
    try:
        race_day = date.fromisoformat(day)
    except ValueError:
        return RedirectResponse("/einstellungen?saved=fehler", status_code=302)

    goal_sec = analysis.parse_time(goal_time) if goal_time else None
    existing = db.q1("SELECT id FROM race WHERE day = ?", (race_day.isoformat(),))
    if existing:
        db.run("UPDATE race SET name = ?, goal_time_sec = ?, distance_km = ? WHERE id = ?",
               (name, goal_sec, distance_km, existing["id"]))
    else:
        db.run("INSERT INTO race (name, day, distance_km, goal_time_sec) VALUES (?,?,?,?)",
               (name, race_day.isoformat(), distance_km, goal_sec))
    return RedirectResponse("/einstellungen?saved=ok", status_code=302)


@app.post("/einstellungen/plan")
def build_plan(
    request: Request,
    weekly_km: float = Form(30.0),
    run_days: str = Form("di,do,sa,so"),
    peak_km: str = Form(""),
):
    if not _require_login(request):
        return _redirect_to_login(request)
    race = analysis.active_race()
    if not race:
        return RedirectResponse("/einstellungen?saved=kein-rennen", status_code=302)

    names = ["mo", "di", "mi", "do", "fr", "sa", "so"]
    days = [names.index(t.strip().lower()[:2]) for t in run_days.split(",")
            if t.strip().lower()[:2] in names] or [1, 3, 5, 6]
    try:
        peak = float(peak_km) if peak_km.strip() else None
    except ValueError:
        peak = None

    plan.generate(plan.PlanConfig(
        race_day=date.fromisoformat(race["day"]),
        goal_time_sec=race.get("goal_time_sec"),
        distance_km=race.get("distance_km") or 42.195,
        start_weekly_km=max(5.0, weekly_km),
        peak_weekly_km=peak,
        run_days=days,
        long_run_day=max(days),
    ))
    return RedirectResponse("/plan", status_code=302)


@app.post("/einstellungen/client-entziehen")
def revoke_client(request: Request, client_id: str = Form(...)):
    """Entzieht einem verbundenen Claude-Client den Zugang — sofort und vollständig.

    Ohne diesen Weg müsste man die Datenbankdatei von Hand bearbeiten, und ein
    einmal ausgestellter Refresh-Token bliebe bis zu 60 Tage gültig."""
    if not _require_login(request):
        return _redirect_to_login(request)
    with db.tx():
        db.run("UPDATE oauth_token SET revoked = 1 WHERE client_id = ?", (client_id,))
        db.run("DELETE FROM oauth_code WHERE client_id = ?", (client_id,))
        db.run("DELETE FROM oauth_client WHERE client_id = ?", (client_id,))
    log.info("OAuth-Client entzogen: %s", client_id)
    return RedirectResponse("/einstellungen?saved=entzogen", status_code=302)


@app.post("/sync")
async def trigger_sync(request: Request):
    if not _require_login(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    result = await scheduler.run_sync_now(days=3)
    return JSONResponse(result)


@app.post("/lauf/{run_id}/bewerten")
def rate_run(request: Request, run_id: int, rpe: str = Form(""), feeling: str = Form(""),
             notes: str = Form("")):
    if not _require_login(request):
        return _redirect_to_login(request)

    def as_int(v: str) -> int | None:
        try:
            n = int(v)
        except (ValueError, TypeError):
            return None
        return n if 1 <= n <= 10 else None

    db.run(
        "UPDATE workout SET rpe = COALESCE(?, rpe), feeling = COALESCE(?, feeling), "
        "notes = COALESCE(NULLIF(?, ''), notes), updated_at = datetime('now') WHERE id = ?",
        (as_int(rpe), as_int(feeling), notes[:2000], run_id),
    )
    return RedirectResponse(f"/lauf/{run_id}", status_code=302)


@app.get("/healthz")
def healthz() -> JSONResponse:
    last = db.q1("SELECT started_at, ok FROM sync_run ORDER BY id DESC LIMIT 1")
    return JSONResponse({
        "ok": True,
        "last_sync": last["started_at"] if last else None,
        "last_sync_ok": bool(last["ok"]) if last else None,
    })


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    return JSONResponse({
        "name": "Marathon-Dashboard",
        "short_name": "Marathon",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1115",
        "theme_color": "#0f1115",
        "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }, media_type="application/manifest+json")
