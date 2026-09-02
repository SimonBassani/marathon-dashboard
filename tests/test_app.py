"""
Rauchtests.

Ziel ist nicht Vollabdeckung, sondern die Zusicherung, dass nach einer Änderung
noch stimmt, was teuer ist, wenn es falsch ist: die Rechenlogik, der Login und
der Schutz des MCP-Endpunkts.

Laufen lassen:  .venv/bin/pytest -q
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Muss VOR dem Import der App gesetzt sein — die Module lesen die Umgebung beim Laden.
_tmp = tempfile.mkdtemp(prefix="marathon-test-")
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "t" * 48)
os.environ.setdefault("APP_PASSWORD", "test-passwort-1234")
os.environ.setdefault("PUBLIC_URL", "https://marathon.example.test")
os.environ.pop("GARMIN_EMAIL", None)  # kein echter Sync im Test

from fastapi.testclient import TestClient  # noqa: E402

from app import analysis, db, garmin, plan  # noqa: E402
from app.main import app  # noqa: E402
from app.security import token_hash, unsign  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    db.init_db()
    for table in ("workout_detail", "workout", "health_metric", "planned_session",
                  "race", "note", "oauth_code", "oauth_token", "oauth_client"):
        db.run(f"DELETE FROM {table}")
    yield


def add_run(day: date, km: float, minutes: float, load: float | None = None) -> int:
    cur = db.run(
        "INSERT INTO workout (garmin_activity_id, day, start_time, sport, name, "
        "duration_sec, distance_m, training_load) VALUES (?,?,?,'run',?,?,?,?)",
        (int(day.toordinal() * 1000 + km), day.isoformat(), f"{day}T07:00:00+02:00",
         f"Lauf {km} km", int(minutes * 60), km * 1000, load),
    )
    return cur.lastrowid


# --------------------------------------------------------------------------
# Formatierung und Parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("3:45", 3 * 3600 + 45 * 60),
    ("3:45:30", 3 * 3600 + 45 * 60 + 30),
    ("3h45", 3 * 3600 + 45 * 60),
    ("bloedsinn", None),
    ("", None),
])
def test_parse_time(text, expected):
    assert analysis.parse_time(text) == expected


def test_fmt_time_and_pace():
    assert analysis.fmt_time(3 * 3600 + 45 * 60) == "3:45:00"
    assert analysis.fmt_time(None) == "–"
    assert analysis.fmt_pace(330) == "5:30/km"
    assert analysis.fmt_pace(0) == "–"


# --------------------------------------------------------------------------
# Auswertung
# --------------------------------------------------------------------------


def test_weekly_volume_buckets_by_monday():
    today = date(2026, 8, 26)  # Mittwoch
    add_run(date(2026, 8, 24), 10, 55)   # Montag derselben Woche
    add_run(date(2026, 8, 26), 8, 44)
    add_run(date(2026, 8, 20), 12, 66)   # Vorwoche
    vols = analysis.weekly_volume(3, today)
    assert vols[-1]["km"] == 18.0
    assert vols[-1]["runs"] == 2
    assert vols[-2]["km"] == 12.0


def test_ramp_rate_ignores_unfinished_week():
    today = date(2026, 8, 26)
    add_run(date(2026, 8, 10), 20, 110)
    add_run(date(2026, 8, 17), 22, 120)
    add_run(date(2026, 8, 24), 4, 22)
    vols = analysis.weekly_volume(4, today)
    # Vergleicht die beiden letzten Wochen MIT Daten, nicht gegen die halbe laufende.
    assert analysis.ramp_rate(vols) == 10.0


def test_acwr_falls_back_to_distance_without_load():
    today = date(2026, 8, 26)
    for i in range(28):
        add_run(today - timedelta(days=i), 5, 30)
    ratio = analysis.acwr(today)
    # Gleichmäßiges Training → akut entspricht chronisch.
    assert ratio == pytest.approx(1.0, abs=0.1)


def test_acwr_flags_spike():
    today = date(2026, 8, 26)
    for i in range(7, 28):
        add_run(today - timedelta(days=i), 2, 12)
    for i in range(7):
        add_run(today - timedelta(days=i), 12, 70)
    assert analysis.acwr(today) > 1.5


def test_riegel_projection_is_slower_over_longer_distance():
    ten_k = 50 * 60
    marathon = analysis.riegel(10, ten_k)
    assert marathon > ten_k * 4.2  # Marathon ist mehr als reine Distanz-Skalierung


def test_warnings_flag_volume_spike():
    today = date(2026, 8, 26)
    for i in range(7, 28):
        add_run(today - timedelta(days=i), 2, 12)
    for i in range(7):
        add_run(today - timedelta(days=i), 14, 80)
    titles = " ".join(w["title"] for w in analysis.warnings(today))
    assert "ACWR" in titles


def test_goal_check_verdict_when_far_off():
    race = {"day": "2027-04-11", "goal_time_sec": 3 * 3600, "distance_km": 42.195}
    pred = {"combined_sec": 4 * 3600}
    result = analysis.goal_check(race, pred)
    assert result["gap_sec"] == 3600
    assert "zu schnell angesetzt" in result["verdict"]


def test_decoupling_detects_hr_drift():
    """Gleiche Pace, aber steigender Puls → positive Entkopplung."""
    n = 120
    details = {
        "metricDescriptors": [{"key": "directHeartRate"}, {"key": "directSpeed"}],
        "activityDetailMetrics": [
            {"metrics": [130 + (0 if i < n // 2 else 20), 3.0]} for i in range(n)
        ],
    }
    result = garmin.compute_decoupling(details)
    assert result is not None
    assert result["decoupling_pct"] > 10


def test_decoupling_returns_none_without_data():
    assert garmin.compute_decoupling({}) is None
    assert garmin.compute_decoupling(None) is None


def test_map_sport():
    assert garmin.map_sport("running") == "run"
    assert garmin.map_sport("treadmill_running") == "run"
    assert garmin.map_sport("road_biking") == "bike"
    assert garmin.map_sport(None) == "other"


# --------------------------------------------------------------------------
# Trainingsplan
# --------------------------------------------------------------------------


def test_plan_generation_covers_period_and_tapers():
    race_day = date.today() + timedelta(weeks=20)
    cfg = plan.PlanConfig(race_day=race_day, goal_time_sec=3 * 3600 + 45 * 60,
                          start_weekly_km=30, start_day=date.today())
    result = plan.generate(cfg)

    assert result["sessions"] > 50
    sessions = plan.read_plan(date.today(), race_day)
    assert all(date.today() <= date.fromisoformat(s["day"]) <= race_day for s in sessions)

    # Der Renntag selbst steht drin.
    assert any(s["title"] == "RENNTAG" for s in sessions)

    # Taper: die letzten Wochen haben weniger Umfang als der Peak.
    # Das Rennen selbst zählt nicht zum Trainingsvolumen — sonst wäre die
    # Rennwoche mit ihren 42 km rechnerisch die größte Woche des Plans.
    def week_km(ws: date) -> float:
        return sum(s["distance_km"] or 0 for s in plan.read_plan(ws, ws + timedelta(days=6))
                   if s["kind"] != "strength" and s["title"] != "RENNTAG")

    weeks = [analysis.week_start(race_day) - timedelta(weeks=i) for i in range(12)]
    volumes = [week_km(w) for w in weeks]
    assert volumes[0] < max(volumes)      # Rennwoche ist die kleinste Trainingswoche
    assert volumes[1] < max(volumes[3:])  # vorletzte Woche unter dem Peak

    # In der Rennwoche steht nichts Hartes und kein langer Lauf mehr.
    race_week = plan.read_plan(analysis.week_start(race_day),
                               analysis.week_start(race_day) + timedelta(days=6))
    assert not any(s["kind"] in ("interval", "tempo", "long") for s in race_week)


def test_plan_respects_selected_run_days():
    race_day = date.today() + timedelta(weeks=10)
    cfg = plan.PlanConfig(race_day=race_day, start_weekly_km=25, run_days=[0, 2, 5],
                          start_day=date.today())
    plan.generate(cfg)
    runs = [s for s in plan.read_plan(date.today(), race_day)
            if s["kind"] not in ("strength", "rest") and s["title"] != "RENNTAG"]
    weekdays = {date.fromisoformat(s["day"]).weekday() for s in runs}
    assert weekdays <= {0, 2, 5}


def test_paces_derive_from_goal():
    paces = plan.paces_from_goal(320)
    assert paces["easy"] > paces["race_pace"] > paces["tempo"] > paces["interval"]


def test_match_completed_links_run_to_plan():
    day = date.today() - timedelta(days=1)
    db.run("INSERT INTO planned_session (day, kind, title, distance_km) VALUES (?,?,?,?)",
           (day.isoformat(), "easy", "Locker 8 km", 8))
    add_run(day, 8, 45)
    assert plan.match_completed(day, day) == 1
    assert plan.read_plan(day, day)[0]["status"] == "done"


def test_match_completed_marks_missed_past_session():
    day = date.today() - timedelta(days=3)
    db.run("INSERT INTO planned_session (day, kind, title, distance_km) VALUES (?,?,?,?)",
           (day.isoformat(), "easy", "Locker 8 km", 8))
    plan.match_completed(day, day)
    assert plan.read_plan(day, day)[0]["status"] == "skipped"


# --------------------------------------------------------------------------
# HTTP: Login, Schutz, MCP
# --------------------------------------------------------------------------


def test_healthz_is_public(client):
    assert client.get("/healthz").status_code == 200


def test_pages_require_login(client):
    for path in ("/", "/plan", "/laeufe", "/gesundheit", "/einstellungen"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, path
        assert r.headers["location"].startswith("/login")


def test_login_rejects_wrong_password(client):
    r = client.post("/login", data={"password": "falsch", "next": "/"}, follow_redirects=False)
    assert r.status_code == 401


def test_login_then_dashboard(client):
    r = client.post("/login", data={"password": "test-passwort-1234", "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 302
    cookie = r.cookies.get("md_session")
    assert cookie and unsign(cookie) is not None

    page = client.get("/", cookies={"md_session": cookie})
    assert page.status_code == 200
    assert "Marathon" in page.text
    client.cookies.clear()


def test_login_blocks_open_redirect(client):
    r = client.post("/login", data={"password": "test-passwort-1234",
                                    "next": "https://boese.example/pwn"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"
    client.cookies.clear()


def test_security_headers_present(client):
    r = client.get("/healthz")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_mcp_requires_bearer_and_points_to_metadata(client):
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    # Genau dieser Header lässt Claude den OAuth-Ablauf finden.
    assert "resource_metadata" in r.headers.get("WWW-Authenticate", "")


def test_mcp_rejects_forged_token(client):
    r = client.post("/mcp/", headers={"Authorization": "Bearer mda_erfunden"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_mcp_accepts_valid_token(client):
    from datetime import datetime, timezone
    token = "mda_gueltig_fuer_den_test"
    db.run("INSERT INTO oauth_token (token_hash, kind, client_id, scope, expires_at) "
           "VALUES (?,'access','c1','mcp',?)",
           (token_hash(token),
            (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()))
    r = client.post(
        "/mcp/",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}}},
    )
    assert r.status_code == 200


def test_expired_token_is_rejected(client):
    from datetime import datetime, timezone
    token = "mda_abgelaufen"
    db.run("INSERT INTO oauth_token (token_hash, kind, client_id, scope, expires_at) "
           "VALUES (?,'access','c1','mcp',?)",
           (token_hash(token),
            (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()))
    r = client.post("/mcp/", headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------


def test_discovery_documents(client):
    pr = client.get("/.well-known/oauth-protected-resource").json()
    assert pr["resource"].endswith("/mcp")

    as_ = client.get("/.well-known/oauth-authorization-server").json()
    assert as_["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in as_["grant_types_supported"]


def test_registration_rejects_foreign_redirect(client):
    r = client.post("/oauth/register", json={
        "client_name": "Boeser Client",
        "redirect_uris": ["https://angreifer.example/callback"],
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_registration_accepts_claude(client):
    r = client.post("/oauth/register", json={
        "client_name": "Claude",
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
    })
    assert r.status_code == 201
    assert r.json()["client_id"].startswith("mcp_")


def test_full_oauth_flow_issues_working_token(client):
    import base64
    import hashlib
    import secrets as _secrets

    redirect = "https://claude.ai/api/mcp/auth_callback"
    client_id = client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [redirect]}).json()["client_id"]

    verifier = _secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    form = client.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "state": "xyz", "code_challenge": challenge, "code_challenge_method": "S256"})
    assert form.status_code == 200

    approved = client.post("/oauth/authorize", data={
        "password": "test-passwort-1234", "client_id": client_id, "redirect_uri": redirect,
        "state": "xyz", "code_challenge": challenge, "scope": "mcp"},
        follow_redirects=False)
    assert approved.status_code == 302
    location = approved.headers["location"]
    assert location.startswith(redirect)
    code = location.split("code=")[1].split("&")[0]

    tokens = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": client_id, "redirect_uri": redirect}).json()
    assert tokens["token_type"] == "Bearer"

    # Und der Token öffnet den MCP-Endpunkt tatsächlich.
    probe = client.post("/mcp/", headers={
        "Authorization": f"Bearer {tokens['access_token']}",
        "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}}})
    assert probe.status_code == 200


def test_oauth_rejects_wrong_pkce_verifier(client):
    import base64
    import hashlib
    import secrets as _secrets

    redirect = "https://claude.ai/api/mcp/auth_callback"
    client_id = client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [redirect]}).json()["client_id"]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(_secrets.token_urlsafe(48).encode()).digest()).decode().rstrip("=")

    approved = client.post("/oauth/authorize", data={
        "password": "test-passwort-1234", "client_id": client_id, "redirect_uri": redirect,
        "state": "", "code_challenge": challenge, "scope": "mcp"}, follow_redirects=False)
    code = approved.headers["location"].split("code=")[1].split("&")[0]

    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": "falscher-verifier", "client_id": client_id,
        "redirect_uri": redirect})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_authorization_code_is_single_use(client):
    import base64
    import hashlib
    import secrets as _secrets

    redirect = "https://claude.ai/api/mcp/auth_callback"
    client_id = client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [redirect]}).json()["client_id"]
    verifier = _secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    approved = client.post("/oauth/authorize", data={
        "password": "test-passwort-1234", "client_id": client_id, "redirect_uri": redirect,
        "state": "", "code_challenge": challenge, "scope": "mcp"}, follow_redirects=False)
    code = approved.headers["location"].split("code=")[1].split("&")[0]

    payload = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
               "client_id": client_id, "redirect_uri": redirect}
    assert client.post("/oauth/token", data=payload).status_code == 200
    second = client.post("/oauth/token", data=payload)
    assert second.status_code == 400


def test_authorize_requires_password(client):
    redirect = "https://claude.ai/api/mcp/auth_callback"
    client_id = client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [redirect]}).json()["client_id"]
    r = client.post("/oauth/authorize", data={
        "password": "falsch", "client_id": client_id, "redirect_uri": redirect,
        "state": "", "code_challenge": "x" * 43, "scope": "mcp"}, follow_redirects=False)
    assert r.status_code == 401


def test_authorize_rejects_unregistered_redirect(client):
    redirect = "https://claude.ai/api/mcp/auth_callback"
    client_id = client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [redirect]}).json()["client_id"]
    r = client.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://claude.ai/anderswohin",
        "response_type": "code", "code_challenge": "x" * 43,
        "code_challenge_method": "S256"})
    assert r.status_code == 400


# --------------------------------------------------------------------------
# MCP-Werkzeuge
# --------------------------------------------------------------------------


def test_mcp_tools_produce_compact_text():
    """Der Sinn der Tools ist Kompaktheit — hier wird das auch geprüft."""
    from app import mcp_server

    today = date.today()
    db.run("INSERT INTO race (name, day, distance_km, goal_time_sec) VALUES (?,?,?,?)",
           ("Testmarathon", (today + timedelta(weeks=30)).isoformat(), 42.195, 3 * 3600 + 45 * 60))
    for i in range(30):
        add_run(today - timedelta(days=i), 10, 55, load=90)
    db.upsert_metric(today, "vo2max", 52)
    db.upsert_metric(today, "race_marathon", 3 * 3600 + 50 * 60)

    text = mcp_server.trainingsstand()
    assert "RENNEN" in text and "PROGNOSE MARATHON" in text
    # Grob: 4 Zeichen je Token. Das Briefing soll unter ~800 Tokens bleiben.
    assert len(text) < 3200, f"trainingsstand ist zu lang: {len(text)} Zeichen"

    runs = mcp_server.laeufe(anzahl=10)
    assert runs.count("\n") <= 14
    assert len(runs) < 2000


def test_mcp_plan_schreiben_replaces_range():
    from app import mcp_server

    day = (date.today() + timedelta(days=2)).isoformat()
    db.run("INSERT INTO planned_session (day, kind, title) VALUES (?,?,?)",
           (day, "easy", "Alte Einheit"))

    msg = mcp_server.plan_schreiben(
        einheiten=[{"datum": day, "art": "long", "titel": "Langer Lauf 24 km", "km": 24}],
        ersetzen=True,
    )
    assert "1 Einheiten" in msg
    sessions = plan.read_plan(date.fromisoformat(day), date.fromisoformat(day))
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Langer Lauf 24 km"
    assert sessions[0]["source"] == "claude"


def test_mcp_plan_schreiben_validates_input():
    from app import mcp_server

    assert "Ungültiges Datum" in mcp_server.plan_schreiben(
        einheiten=[{"datum": "31.12.2026", "art": "easy", "titel": "x"}])
    assert "Unbekannte Art" in mcp_server.plan_schreiben(
        einheiten=[{"datum": "2026-12-31", "art": "quatsch", "titel": "x"}])


def test_mcp_ziel_setzen_roundtrip():
    from app import mcp_server

    msg = mcp_server.ziel_setzen(name="Wien Marathon", datum="2027-04-11", zielzeit="3:45")
    assert "3:45:00" in msg
    race = analysis.active_race()
    assert race["name"] == "Wien Marathon"
    assert race["goal_time_sec"] == 3 * 3600 + 45 * 60


def test_mcp_tools_are_registered():
    """Alle Werkzeuge müssen eine Beschreibung haben — daran erkennt Claude,
    wann es welches nimmt."""
    import asyncio

    from app.mcp_server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "trainingsstand" in tools
    assert "plan_schreiben" in tools
    for name, tool in tools.items():
        assert tool.description, f"{name} hat keine Beschreibung"


# --------------------------------------------------------------------------
# Nachtrag aus dem Sicherheits-Review
# --------------------------------------------------------------------------


def test_redirect_uri_rejects_dangerous_schemes():
    """Eine reine Host-Prüfung reicht nicht: `javascript://localhost/...` hat den
    Hostnamen `localhost` und käme sonst durch."""
    from app.oauth import _redirect_allowed

    for uri in (
        "javascript://localhost/%0aalert(1)",
        "data://localhost/,x",
        "vbscript://127.0.0.1/x",
        "file://localhost/etc/passwd",
        "ftp://claude.ai/x",
    ):
        assert _redirect_allowed(uri) is False, uri

    assert _redirect_allowed("https://claude.ai/api/mcp/auth_callback") is True


def test_loopback_redirect_only_allowed_in_local_development(monkeypatch):
    """Steht der Server öffentlich (PUBLIC_URL ist https), ist ein Rückleitungsziel
    auf localhost nicht mehr plausibel und wird abgewiesen."""
    from app.oauth import _redirect_allowed

    monkeypatch.setenv("PUBLIC_URL", "https://marathon.example.test")
    assert _redirect_allowed("http://localhost:31337/steal") is False

    monkeypatch.setenv("PUBLIC_URL", "http://localhost:8000")
    assert _redirect_allowed("http://localhost:31337/steal") is True


def test_registration_rejects_javascript_redirect(client):
    r = client.post("/oauth/register", json={
        "client_name": "Böser Client",
        "redirect_uris": ["javascript://localhost/%0aalert(1)"],
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_consent_page_names_client_and_target(client):
    """Der Nutzer muss sehen, WER Zugriff will und WOHIN der Code geht — sonst
    ist die Zustimmungsseite für jeden Client identisch und damit wertlos."""
    redirect = "https://claude.ai/api/mcp/auth_callback"
    client_id = client.post("/oauth/register", json={
        "client_name": "Testclient XY", "redirect_uris": [redirect]}).json()["client_id"]

    page = client.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "code_challenge": "x" * 43, "code_challenge_method": "S256"}).text
    assert "Testclient XY" in page
    assert "claude.ai/api/mcp/auth_callback" in page


def test_consent_page_escapes_client_name(client):
    redirect = "https://claude.ai/api/mcp/auth_callback"
    client_id = client.post("/oauth/register", json={
        "client_name": "<script>alert(1)</script>", "redirect_uris": [redirect]}).json()["client_id"]

    page = client.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "code_challenge": "x" * 43, "code_challenge_method": "S256"}).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_session_is_invalidated_by_password_change(monkeypatch):
    """Ein Passwortwechsel muss alle bestehenden Sitzungen beenden. Sonst bleibt
    ein gestohlenes Cookie bis zu 30 Tage gültig, obwohl der Besitzer glaubt,
    den Zugang gesperrt zu haben."""
    from app import security

    cookie = security.new_session_cookie()
    assert security.valid_session(cookie) is True

    monkeypatch.setenv("APP_PASSWORD", "ein-ganz-neues-passwort")
    assert security.valid_session(cookie) is False


def test_global_limiter_ignores_the_key():
    """Der Auffangzähler darf sich nicht durch eine gefälschte Absender-IP
    zurücksetzen lassen — er zählt alle Versuche zusammen."""
    from app.security import GlobalLimiter

    limiter = GlobalLimiter(limit=3, window_sec=300)
    assert [limiter.allow(f"1.2.3.{i}") for i in range(3)] == [True, True, True]
    assert limiter.allow("9.9.9.9") is False


def test_revoke_client_kills_existing_tokens(client):
    """Der Entzugs-Knopf muss auch bereits ausgestellte Tokens entwerten,
    nicht nur den Client aus der Liste nehmen."""
    from datetime import datetime, timezone

    from app.oauth import verify_bearer

    db.run("INSERT INTO oauth_client (client_id, client_name, redirect_uris) VALUES (?,?,?)",
           ("mcp_zuentziehen", "Alter Client", '["https://claude.ai/x"]'))
    token = "mda_noch_gueltig"
    db.run("INSERT INTO oauth_token (token_hash, kind, client_id, scope, expires_at) "
           "VALUES (?,'access','mcp_zuentziehen','mcp',?)",
           (token_hash(token),
            (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()))
    assert verify_bearer(f"Bearer {token}") is True

    # Das Sitzungs-Cookie ist `Secure`; der Testclient spricht http und würde es
    # sonst verwerfen — deshalb hier explizit mitgeben.
    login = client.post("/login", data={"password": "test-passwort-1234", "next": "/"},
                        follow_redirects=False)
    session = login.cookies["md_session"]
    r = client.post("/einstellungen/client-entziehen",
                    data={"client_id": "mcp_zuentziehen"},
                    cookies={"md_session": session}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/einstellungen?saved=entzogen"

    assert verify_bearer(f"Bearer {token}") is False
    assert db.q1("SELECT 1 FROM oauth_client WHERE client_id = ?", ("mcp_zuentziehen",)) is None
    client.cookies.clear()


def test_revoke_client_requires_login(client):
    r = client.post("/einstellungen/client-entziehen",
                    data={"client_id": "irgendwas"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")
