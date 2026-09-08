#!/usr/bin/env bash
#
# Entfernt das Marathon-Dashboard restlos vom VPS.
#
#     sudo bash scripts/vps-entfernen.sh
#
# Rührt ausschließlich das an, was vps-beistellen.sh angelegt hat: den
# Container marathon-app, das Volume marathon_data und optional diesen Ordner.
# Das fremde Netz, der fremde Proxy und alle anderen Container bleiben
# unberührt — deshalb hier kein "docker system prune", das würde auch die
# Images deiner anderen Projekte wegräumen.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJEKT="${PROJEKT:-marathon}"
COMPOSE="docker-compose.beistellen.yml"

sag()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
bang() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; }

sag "Was entfernt wird"
docker ps -a --filter "name=marathon-app" --format '  Container: {{.Names}} ({{.Status}})' || true
docker volume ls --filter "name=${PROJEKT}_data" --format '  Volume:    {{.Name}}' || true
info "Ordner:    $(pwd)  (nur wenn du gleich zustimmst)"

echo
info "NICHT angetastet: alle anderen Container, Volumes, Netze und Images."
echo

printf '  Wirklich entfernen? Die Trainingsdaten im Volume sind danach weg. [tippe: JA] '
read -r bestaetigung < /dev/tty
[ "$bestaetigung" = "JA" ] || { info "Abgebrochen. Nichts verändert."; exit 0; }

# ---------------------------------------------------------------------------
# 1 · Container und Volume
# ---------------------------------------------------------------------------
sag "1/3  Container und Daten"
if [ -f "$COMPOSE" ]; then
  # FREMDES_NETZ muss gesetzt sein, sonst verweigert compose das Einlesen —
  # der Wert ist beim Abbau egal, das externe Netz wird ohnehin nicht angefasst.
  FREMDES_NETZ="${FREMDES_NETZ:-bridge}" \
    docker compose -f "$COMPOSE" -p "$PROJEKT" down -v --remove-orphans || true
  info "Container gestoppt, Volume entfernt"
else
  docker rm -f marathon-app >/dev/null 2>&1 || true
  docker volume rm "${PROJEKT}_data" >/dev/null 2>&1 || true
  info "Container und Volume entfernt (ohne Compose-Datei)"
fi

# Nur unser eigenes Image, nichts anderes.
docker image rm "${PROJEKT}-app" >/dev/null 2>&1 && info "Image entfernt" || true

# ---------------------------------------------------------------------------
# 2 · Gegenprüfen statt behaupten
# ---------------------------------------------------------------------------
sag "2/3  Nachprüfen"
if docker ps -a --format '{{.Names}}' | grep -qx "marathon-app"; then
  bang "Container marathon-app existiert noch — bitte ansehen."
else
  info "kein marathon-app Container mehr da"
fi
if docker volume ls --format '{{.Name}}' | grep -qx "${PROJEKT}_data"; then
  bang "Volume ${PROJEKT}_data existiert noch."
else
  info "kein ${PROJEKT}_data Volume mehr da"
fi
info "andere Container laufen weiter:"
docker ps --format '    {{.Names}}  ({{.Status}})' | grep -v marathon-app || info "    (keine)"

# ---------------------------------------------------------------------------
# 3 · Was du im fremden Caddyfile noch löschen musst
# ---------------------------------------------------------------------------
sag "3/3  Letzter Schritt von Hand"
DOMAIN_ALT="$(grep '^DOMAIN=' .env 2>/dev/null | cut -d= -f2- || true)"
cat <<HINWEIS

Im Caddyfile deines anderen Projekts steht noch der Block für
  ${DOMAIN_ALT:-marathon.deine-domain.at}

Den bitte löschen und Caddy neu laden — sonst antwortet die Subdomain mit einem
Fehler statt gar nicht:

    docker exec -w /etc/caddy <dein-caddy-container> caddy reload

Den A-Record der Subdomain kannst du beim DNS-Anbieter ebenfalls entfernen.

Diesen Ordner löschen (enthält die .env mit deinen Zugangsdaten):
    cd .. && rm -rf "$(basename "$(pwd)")"
HINWEIS
