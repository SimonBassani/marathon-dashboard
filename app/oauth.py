"""
OAuth-2.1-Autorisierungsserver für den Remote-MCP-Zugang.

Claude.ai verbindet sich NICHT vom Handy aus, sondern aus Anthropics Cloud. Der
Server muss also öffentlich per HTTPS erreichbar sein — und darf deshalb nicht
ungeschützt sein. Die MCP-Spezifikation (Revision 2025-06-18) verlangt dafür
OAuth 2.1.

Umgesetzt ist genau der Teil, den Claude als Client braucht:

  RFC 9728  /.well-known/oauth-protected-resource   — wer schützt diese Ressource
  RFC 8414  /.well-known/oauth-authorization-server — welche Endpunkte gibt es
  RFC 7591  POST /oauth/register                    — Dynamic Client Registration
            GET/POST /oauth/authorize               — Login mit dem App-Passwort
            POST /oauth/token                       — Code gegen Token, PKCE-geprüft

Bewusste Vereinfachungen, weil hier genau ein Mensch seine eigenen Daten liest:
  * Nutzerverwaltung = das eine APP_PASSWORD. Kein Registrierungsformular.
  * Redirect-URIs nur auf Anthropic-Domains (plus localhost zum Testen).
  * Zugriffstoken 24 h, Refresh-Token 60 Tage, beide nur als Hash gespeichert.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import db
from .security import GlobalLimiter, RateLimiter, check_password, new_token, token_hash

log = logging.getLogger("oauth")
router = APIRouter()

ACCESS_TTL = timedelta(hours=24)
REFRESH_TTL = timedelta(days=60)
CODE_TTL = timedelta(minutes=5)
MCP_SCOPE = "mcp"

# Nur diese Hosts dürfen als Rückleitungsziel registriert werden. Ohne diese
# Sperre könnte sich jede fremde Seite als Client registrieren und den Nutzer
# nach dem Login auf ihre eigene Adresse samt Code schicken.
ALLOWED_REDIRECT_HOSTS = {
    "claude.ai",
    "www.claude.ai",
    "claude.com",
    "www.claude.com",
    "localhost",
    "127.0.0.1",
}

_login_limiter = RateLimiter(limit=8, window_sec=300)
_global_limiter = GlobalLimiter(limit=40, window_sec=300)


def base_url() -> str:
    url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("PUBLIC_URL ist nicht gesetzt (z. B. https://marathon.example.com)")
    return url


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _dev_mode() -> bool:
    """Lokale Entwicklung erkennt man daran, dass PUBLIC_URL kein HTTPS ist.
    Sobald der Server öffentlich steht, sind Loopback-Ziele nicht mehr erlaubt."""
    return not os.environ.get("PUBLIC_URL", "https://").startswith("https://")


def _redirect_allowed(uri: str) -> bool:
    """Prüft ein Rückleitungsziel gegen die Allowlist.

    Das Schema wird zuerst geprüft, und zwar als Positivliste. Eine reine
    Host-Prüfung reicht NICHT: `javascript://localhost/...` hat den Hostnamen
    `localhost` und käme sonst durch.
    """
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False

    loopback = {"localhost", "127.0.0.1"}
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and parsed.hostname in loopback:
        pass
    else:
        # javascript:, data:, vbscript:, file: und alles andere fliegen hier raus.
        return False

    allowed = ALLOWED_REDIRECT_HOSTS if _dev_mode() else ALLOWED_REDIRECT_HOSTS - loopback
    if parsed.hostname not in allowed:
        return False
    return not parsed.fragment


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource() -> JSONResponse:
    base = base_url()
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "scopes_supported": [MCP_SCOPE],
            "bearer_methods_supported": ["header"],
        }
    )


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
def authorization_server() -> JSONResponse:
    base = base_url()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "scopes_supported": [MCP_SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


# --------------------------------------------------------------------------
# Dynamic Client Registration
# --------------------------------------------------------------------------


@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "redirect_uris fehlt"},
            status_code=400,
        )
    bad = [u for u in uris if not isinstance(u, str) or not _redirect_allowed(u)]
    if bad:
        return JSONResponse(
            {
                "error": "invalid_redirect_uri",
                "error_description": f"Nicht erlaubtes Rückleitungsziel: {bad[0]}",
            },
            status_code=400,
        )

    client_id = f"mcp_{secrets.token_urlsafe(18)}"
    db.run(
        "INSERT INTO oauth_client (client_id, client_name, redirect_uris) VALUES (?,?,?)",
        (client_id, str(body.get("client_name") or "Claude")[:120], json.dumps(uris)),
    )
    log.info("Neuer OAuth-Client registriert: %s", client_id)

    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(_now().timestamp()),
            "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


# --------------------------------------------------------------------------
# Autorisierung
# --------------------------------------------------------------------------

_LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marathon-Dashboard verbinden</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
         background:#0f1115; color:#e7e9ee; padding:24px; }}
  form {{ width:100%; max-width:360px; }}
  h1 {{ font-size:1.25rem; margin:0 0 4px; }}
  p {{ color:#9aa1ae; margin:0 0 20px; font-size:.9rem; }}
  label {{ display:block; font-size:.85rem; color:#9aa1ae; margin-bottom:6px; }}
  input {{ width:100%; box-sizing:border-box; padding:12px; border-radius:10px;
          border:1px solid #2a2f3a; background:#171a21; color:#e7e9ee; font-size:1rem; }}
  button {{ width:100%; margin-top:14px; padding:12px; border:0; border-radius:10px;
           background:#ff5a1f; color:#fff; font-size:1rem; font-weight:600; }}
  .err {{ color:#ff8080; font-size:.85rem; margin-top:12px; }}
  .detail {{ font-size:.8rem; }}
  code {{ word-break:break-all; color:#e7e9ee; }}
</style>
<form method="post" action="/oauth/authorize">
  <h1>Zugriff erlauben?</h1>
  <p><strong>{client_name}</strong> möchte auf dein Marathon-Dashboard zugreifen und
     darf danach deine Trainingsdaten lesen sowie Plan und Notizen schreiben.</p>
  <p class="detail">Nach der Bestätigung wirst du weitergeleitet an:<br><code>{redirect_uri}</code></p>
  <p>Erlaube das nur, wenn du diese Verbindung gerade selbst in Claude gestartet hast.</p>
  <label for="pw">Dashboard-Passwort</label>
  <input id="pw" type="password" name="password" autocomplete="current-password" autofocus required>
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="scope" value="{scope}">
  <input type="hidden" name="resource" value="{resource}">
  <button type="submit">Zugriff erlauben</button>
  {error}
</form>
"""


def _client_name(client_id: str) -> str:
    row = db.q1("SELECT client_name FROM oauth_client WHERE client_id = ?", (client_id,))
    return (row["client_name"] if row and row["client_name"] else "Ein unbekannter Client")


def _esc(value: str | None) -> str:
    import html

    return html.escape(value or "", quote=True)


@router.get("/oauth/authorize")
def authorize_form(
    request: Request,
    client_id: str = "",
    redirect_uri: str = "",
    response_type: str = "code",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = MCP_SCOPE,
    resource: str = "",
) -> HTMLResponse:
    error = _validate_authorize(client_id, redirect_uri, response_type, code_challenge,
                               code_challenge_method)
    if error:
        return HTMLResponse(f"<h1>Verbindung nicht möglich</h1><p>{_esc(error)}</p>", status_code=400)

    return HTMLResponse(
        _LOGIN_PAGE.format(
            client_id=_esc(client_id),
            client_name=_esc(_client_name(client_id)),
            redirect_uri=_esc(redirect_uri),
            state=_esc(state),
            code_challenge=_esc(code_challenge),
            scope=_esc(scope or MCP_SCOPE),
            resource=_esc(resource),
            error="",
        )
    )


def _validate_authorize(client_id: str, redirect_uri: str, response_type: str,
                        code_challenge: str, code_challenge_method: str) -> str | None:
    if response_type != "code":
        return "Nur response_type=code wird unterstützt."
    # PKCE ist in OAuth 2.1 Pflicht — ohne Challenge kein Code.
    if not code_challenge or (code_challenge_method and code_challenge_method != "S256"):
        return "PKCE mit S256 ist erforderlich."
    client = db.q1("SELECT redirect_uris FROM oauth_client WHERE client_id = ?", (client_id,))
    if client is None:
        return "Unbekannter Client. Bitte den Connector in Claude neu hinzufügen."
    registered = db.jloads(client["redirect_uris"]) or []
    # Exakter Abgleich — kein Präfix-Matching, das ist die klassische Lücke.
    if redirect_uri not in registered:
        return "Das Rückleitungsziel wurde für diesen Client nicht registriert."
    return None


@router.post("/oauth/authorize")
def authorize_submit(
    request: Request,
    password: str = Form(""),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    scope: str = Form(MCP_SCOPE),
    resource: str = Form(""),
):
    error = _validate_authorize(client_id, redirect_uri, "code", code_challenge, "S256")
    if error:
        return HTMLResponse(f"<h1>Verbindung nicht möglich</h1><p>{_esc(error)}</p>", status_code=400)

    client_ip = request.client.host if request.client else "unknown"
    if not _login_limiter.allow(client_ip) or not _global_limiter.allow():
        return HTMLResponse(
            "<h1>Zu viele Versuche</h1><p>Bitte in fünf Minuten noch einmal probieren.</p>",
            status_code=429,
        )

    if not check_password(password):
        log.warning("Fehlgeschlagener OAuth-Login von %s", client_ip)
        page = _LOGIN_PAGE.format(
            client_id=_esc(client_id), client_name=_esc(_client_name(client_id)),
            redirect_uri=_esc(redirect_uri), state=_esc(state),
            code_challenge=_esc(code_challenge), scope=_esc(scope), resource=_esc(resource),
            error='<p class="err">Passwort stimmt nicht.</p>',
        )
        return HTMLResponse(page, status_code=401)

    _login_limiter.reset(client_ip)
    _global_limiter.reset()
    code = secrets.token_urlsafe(32)
    db.run(
        "INSERT INTO oauth_code (code, client_id, redirect_uri, code_challenge, scope, "
        "resource, expires_at) VALUES (?,?,?,?,?,?,?)",
        (code, client_id, redirect_uri, code_challenge, scope or MCP_SCOPE,
         resource or None, _iso(_now() + CODE_TTL)),
    )

    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------


def _verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return secrets.compare_digest(expected, challenge)


def _issue_tokens(client_id: str, scope: str) -> dict:
    access = new_token("mda")
    refresh = new_token("mdr")
    db.run(
        "INSERT INTO oauth_token (token_hash, kind, client_id, scope, expires_at) VALUES (?,?,?,?,?)",
        (token_hash(access), "access", client_id, scope, _iso(_now() + ACCESS_TTL)),
    )
    db.run(
        "INSERT INTO oauth_token (token_hash, kind, client_id, scope, expires_at) VALUES (?,?,?,?,?)",
        (token_hash(refresh), "refresh", client_id, scope, _iso(_now() + REFRESH_TTL)),
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "refresh_token": refresh,
        "scope": scope,
    }


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    form = await request.form()
    grant = form.get("grant_type")

    if grant == "authorization_code":
        code = str(form.get("code") or "")
        verifier = str(form.get("code_verifier") or "")
        client_id = str(form.get("client_id") or "")
        redirect_uri = str(form.get("redirect_uri") or "")

        row = db.q1("SELECT * FROM oauth_code WHERE code = ?", (code,))
        if row is None or row["used"]:
            # Ein bereits benutzter Code kann bedeuten, dass er abgefangen wurde.
            # Deshalb zusätzlich alle Tokens dieses Clients entwerten.
            if row is not None:
                db.run("UPDATE oauth_token SET revoked = 1 WHERE client_id = ?", (row["client_id"],))
                log.warning("Autorisierungscode doppelt eingelöst — Tokens von %s entwertet",
                            row["client_id"])
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        db.run("UPDATE oauth_code SET used = 1 WHERE code = ?", (code,))

        if row["expires_at"] < _iso(_now()):
            return JSONResponse({"error": "invalid_grant", "error_description": "Code abgelaufen"},
                                status_code=400)
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not verifier or not _verify_pkce(verifier, row["code_challenge"]):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE fehlgeschlagen"},
                                status_code=400)

        return JSONResponse(_issue_tokens(client_id, row["scope"]))

    if grant == "refresh_token":
        presented = str(form.get("refresh_token") or "")
        row = db.q1(
            "SELECT * FROM oauth_token WHERE token_hash = ? AND kind = 'refresh' AND revoked = 0",
            (token_hash(presented),),
        )
        if row is None or (row["expires_at"] and row["expires_at"] < _iso(_now())):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        # Rotation: der alte Refresh-Token gilt ab jetzt nicht mehr.
        db.run("UPDATE oauth_token SET revoked = 1 WHERE token_hash = ?", (row["token_hash"],))
        return JSONResponse(_issue_tokens(row["client_id"], row["scope"]))

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# --------------------------------------------------------------------------
# Prüfung eingehender Anfragen
# --------------------------------------------------------------------------


def verify_bearer(header: str | None) -> bool:
    """True, wenn der Authorization-Header ein gültiges Zugriffstoken trägt."""
    if not header or not header.lower().startswith("bearer "):
        return False
    presented = header[7:].strip()
    if not presented:
        return False
    row = db.q1(
        "SELECT expires_at FROM oauth_token WHERE token_hash = ? AND kind = 'access' AND revoked = 0",
        (token_hash(presented),),
    )
    if row is None:
        return False
    return not row["expires_at"] or row["expires_at"] >= _iso(_now())


def cleanup_expired() -> int:
    """Abgelaufene Codes und Tokens entfernen. Läuft im Scheduler mit."""
    now = _iso(_now())
    cur = db.run("DELETE FROM oauth_code WHERE expires_at < ?", (now,))
    removed = cur.rowcount or 0
    cur = db.run("DELETE FROM oauth_token WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
    return removed + (cur.rowcount or 0)
