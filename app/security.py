"""
Passwort, Sitzungs-Cookie und Token-Hilfen.

Bewusst klein und ohne zusätzliche Abhängigkeiten: signierte Cookies mit
HMAC-SHA256 aus der Standardbibliothek. Weniger Fremdcode heißt weniger, das
man bei jedem Update prüfen muss.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

SESSION_COOKIE = "md_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 Tage — es ist ein privates Gerät


def secret_key() -> bytes:
    key = os.environ.get("SECRET_KEY", "")
    if len(key) < 32:
        raise RuntimeError(
            "SECRET_KEY fehlt oder ist zu kurz (mindestens 32 Zeichen). "
            "Erzeugen mit:  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return key.encode()


def cookie_secure() -> bool:
    """Das Sitzungs-Cookie soll im Betrieb nur über HTTPS gehen. Lokal läuft
    die App aber auf http://localhost — dort würde `secure` das Cookie
    verwerfen und der Login schlüge endlos fehl. Deshalb richtet es sich nach
    PUBLIC_URL: https → secure, alles andere → nicht secure."""
    return os.environ.get("PUBLIC_URL", "https://").startswith("https://")


def app_password() -> str:
    pw = os.environ.get("APP_PASSWORD", "")
    if len(pw) < 12:
        raise RuntimeError("APP_PASSWORD fehlt oder ist kürzer als 12 Zeichen.")
    return pw


def check_password(candidate: str) -> bool:
    """Konstante Laufzeit — verrät über die Antwortzeit nichts über das Passwort."""
    return hmac.compare_digest(candidate.encode(), app_password().encode())


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict[str, Any]) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64e(hmac.new(secret_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def unsign(token: str, max_age: int | None = None) -> dict[str, Any] | None:
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64e(hmac.new(secret_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if max_age is not None:
        issued = payload.get("iat", 0)
        if not isinstance(issued, (int, float)) or time.time() - issued > max_age:
            return None
    return payload


def password_version() -> str:
    """Kurzer Fingerabdruck des aktuellen Passworts.

    Er steckt in jedem Sitzungs-Cookie. Wird APP_PASSWORD geändert, passt der
    Fingerabdruck nicht mehr und alle bestehenden Sitzungen sind sofort ungültig.
    Ohne das würde ein Passwortwechsel nur neue Anmeldungen betreffen — ein
    gestohlenes Cookie bliebe bis zu 30 Tage gültig."""
    return hashlib.sha256(app_password().encode()).hexdigest()[:16]


def new_session_cookie() -> str:
    return sign({"iat": int(time.time()), "sub": "owner", "pv": password_version()})


def valid_session(cookie: str | None) -> bool:
    if not cookie:
        return False
    payload = unsign(cookie, SESSION_MAX_AGE)
    if payload is None:
        return False
    return hmac.compare_digest(str(payload.get("pv", "")), password_version())


def token_hash(token: str) -> str:
    """Tokens werden nur als Hash gespeichert. Wer die Datenbank liest, kann
    damit nichts anfangen."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(40)}"


class RateLimiter:
    """Sehr einfacher Zähler gegen Passwort-Raten. Ein Nutzer, ein Prozess —
    mehr braucht es hier nicht."""

    def __init__(self, limit: int = 8, window_sec: int = 300) -> None:
        self.limit = limit
        self.window = window_sec
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        self._hits[key] = hits
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


# Zweite Sperre ohne Schlüssel: greift für ALLE Anfragen zusammen. Selbst wenn
# es jemandem gelingt, die Absender-IP zu fälschen und damit für jeden Versuch
# einen frischen Zähler zu bekommen, bleibt diese Grenze bestehen. Sie liegt
# bewusst hoch genug, dass normale Tippfehler nie danebengreifen.
class GlobalLimiter(RateLimiter):
    KEY = "global"

    def allow(self, _key: str = KEY) -> bool:  # noqa: ARG002 — Signatur bleibt kompatibel
        return super().allow(self.KEY)

    def reset(self, _key: str = KEY) -> None:  # noqa: ARG002
        super().reset(self.KEY)
