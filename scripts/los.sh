#!/usr/bin/env bash
#
# Ein Befehl, der alles macht: Python prüfen, Umgebung bauen, Abhängigkeiten
# installieren, optional Garmin verbinden, starten.
#
#     ./scripts/los.sh
#
# Beim zweiten Aufruf überspringt es, was schon da ist, und startet nur.
#
# Absichtlich bash 3.2 (was macOS mitbringt) — keine assoziativen Arrays,
# kein ${var,,}, kein readarray. Läuft damit auch auf einem frischen Mac.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PORT="${PORT:-8000}"
DB="${DB_PATH:-./marathon.db}"

sag()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
bang() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 1 · Python ≥ 3.12 finden. garminconnect verlangt es; macOS bringt nur 3.9 mit.
# ---------------------------------------------------------------------------
sag "1/5  Python suchen"
PY=""
# 3.12 zuerst: darauf läuft das Docker-Image und dagegen sind die Tests grün.
# 3.13 nur als Rückfall, wenn 3.12 nicht da ist.
for kandidat in python3.12 python3.13 python3; do
  command -v "$kandidat" >/dev/null 2>&1 || continue
  if "$kandidat" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
    PY="$kandidat"; break
  fi
done

if [ -z "$PY" ]; then
  bang "Kein Python 3.12 oder neuer gefunden."
  if [ "$(uname)" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      info "Installieren mit:  brew install python@3.12"
      info "Danach dieses Skript nochmal starten."
    else
      info "Erst Homebrew installieren (https://brew.sh), dann:"
      info "  brew install python@3.12"
    fi
  else
    info "Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv"
  fi
  exit 1
fi
info "$($PY --version) — passt"

# ---------------------------------------------------------------------------
# 2 · Virtuelle Umgebung
# ---------------------------------------------------------------------------
sag "2/5  Umgebung vorbereiten"
if [ -x .venv/bin/python ] && .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
  info ".venv existiert und passt — übersprungen"
else
  [ -e .venv ] && { info "alte .venv mit falscher Version entfernt"; rm -rf .venv; }
  "$PY" -m venv .venv
  info ".venv angelegt mit $(.venv/bin/python --version)"
fi

if .venv/bin/python -c 'import fastapi, garminconnect, fastmcp' 2>/dev/null; then
  info "Abhängigkeiten schon da — übersprungen"
else
  info "Abhängigkeiten installieren (dauert 1-2 Minuten) ..."
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements-dev.txt
  info "fertig"
fi

# ---------------------------------------------------------------------------
# 3 · .env — Passwörter landen in einer Datei mit chmod 600, nie in der History
# ---------------------------------------------------------------------------
sag "3/5  Zugangsdaten"
if [ -f .env ]; then
  info ".env existiert — unverändert gelassen"
else
  umask 077                       # die Datei entsteht direkt als 600
  SECRET="$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(48))')"

  GM=""; GP=""
  # Nachfragen nur, wenn wirklich ein Terminal dranhängt. Läuft das Skript aus
  # einer Pipe oder einem Cron-Job, wird still mit Demodaten weitergemacht.
  if [ -t 0 ] && [ -r /dev/tty ]; then
    printf '  Garmin verbinden? Ohne Zugangsdaten läuft alles mit Demodaten. [j/N] '
    read -r antwort < /dev/tty || antwort="n"
    case "$antwort" in
      [jJyY]*)
        printf '  Garmin E-Mail: '
        read -r GM < /dev/tty
        printf '  Garmin Passwort (bleibt unsichtbar): '
        read -rs GP < /dev/tty; printf '\n'
        ;;
      *) info "übersprungen — später einfach .env bearbeiten" ;;
    esac
  else
    info "kein Terminal — Demodaten. Garmin später in .env eintragen."
  fi

  {
    echo "# Von scripts/los.sh erzeugt. Diese Datei ist gitignored — niemals committen."
    echo "APP_PASSWORD=demo-passwort-1234"
    echo "SECRET_KEY=$SECRET"
    echo "PUBLIC_URL=http://localhost:$PORT"
    echo "GARMIN_EMAIL=$GM"
    echo "GARMIN_PASSWORD=$GP"
  } > .env
  chmod 600 .env
  info ".env angelegt (nur für dich lesbar)"
  [ -n "$GM" ] && info "Garmin hinterlegt für $GM" || true
fi

# ---------------------------------------------------------------------------
# 4 · Datenbank. Leere DB ohne Garmin wäre eine leere Seite — dann Demodaten.
# ---------------------------------------------------------------------------
sag "4/5  Datenbank"
# shellcheck disable=SC1091
set -a; . ./.env; set +a          # die App liest .env NICHT selbst ein

if [ -f "$DB" ]; then
  info "$DB existiert — unverändert gelassen"
elif [ -n "${GARMIN_EMAIL:-}" ]; then
  info "$DB wird beim ersten Sync von Garmin gefüllt"
else
  DB_PATH="$DB" .venv/bin/python scripts/seed_demo.py
fi

# ---------------------------------------------------------------------------
# 5 · Starten. --reload, damit ein späteres "git pull" nicht in einem
#     "Internal Server Error" endet, weil der alte Code noch im Speicher liegt.
# ---------------------------------------------------------------------------
sag "5/5  Start"
info "Adresse:  http://localhost:$PORT"
info "Passwort: ${APP_PASSWORD:-demo-passwort-1234}"
info "Beenden:  Strg+C"
echo
exec env DB_PATH="$DB" .venv/bin/uvicorn app.main:app --port "$PORT" --reload
