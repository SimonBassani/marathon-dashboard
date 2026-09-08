#!/usr/bin/env bash
#
# Stellt das Marathon-Dashboard auf einem VPS dazu, auf dem schon etwas anderes
# mit einem Reverse-Proxy läuft. Nichts am bestehenden Dienst wird verändert —
# das Skript sagt am Ende nur, welche vier Zeilen du im fremden Caddyfile
# ergänzen musst.
#
#     sudo bash scripts/vps-beistellen.sh
#
# Rückgängig: scripts/vps-entfernen.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJEKT="${PROJEKT:-marathon}"
COMPOSE="docker-compose.beistellen.yml"

sag()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
bang() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; }

command -v docker >/dev/null 2>&1 || { bang "Docker fehlt."; exit 1; }

# ---------------------------------------------------------------------------
# 1 · Fremdes Docker-Netz finden, in das wir uns hängen
# ---------------------------------------------------------------------------
sag "1/4  Vorhandenes Netz suchen"
if [ -n "${FREMDES_NETZ:-}" ]; then
  info "vorgegeben: $FREMDES_NETZ"
else
  # Netze, in denen ein Caddy/nginx/Traefik hängt — das ist der Proxy, der uns
  # später abholen soll.
  kandidaten="$(docker ps --format '{{.Names}}' \
    | grep -iE 'caddy|nginx|traefik' \
    | while read -r c; do
        docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$c"
      done | tr ' ' '\n' | grep -v '^$' | sort -u)"

  anzahl="$(printf '%s\n' "$kandidaten" | grep -c . || true)"
  if [ "$anzahl" -eq 1 ]; then
    FREMDES_NETZ="$kandidaten"
    info "gefunden: $FREMDES_NETZ"
  elif [ "$anzahl" -eq 0 ]; then
    bang "Kein laufender Reverse-Proxy gefunden."
    info "Läuft dein anderes Projekt gerade? Sonst Netz von Hand angeben:"
    info "  FREMDES_NETZ=dashboard_internal sudo -E bash scripts/vps-beistellen.sh"
    exit 1
  else
    bang "Mehrere Netze in Frage:"
    printf '%s\n' "$kandidaten" | sed 's/^/    /'
    info "Bitte eines auswählen:"
    info "  FREMDES_NETZ=<name> sudo -E bash scripts/vps-beistellen.sh"
    exit 1
  fi
fi
export FREMDES_NETZ

docker network inspect "$FREMDES_NETZ" >/dev/null 2>&1 \
  || { bang "Netz $FREMDES_NETZ existiert nicht."; exit 1; }

# Den Proxy-Container merken, um am Ende den Reload-Befehl zu nennen.
PROXY="$(docker ps --format '{{.Names}}' --filter "network=$FREMDES_NETZ" \
         | grep -iE 'caddy|nginx|traefik' | head -1 || true)"

# ---------------------------------------------------------------------------
# 2 · .env
# ---------------------------------------------------------------------------
sag "2/4  Konfiguration"
if [ -f .env ]; then
  info ".env existiert — unverändert gelassen"
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
else
  umask 077
  printf '  Subdomain fürs Marathon-Dashboard (z.B. marathon.deine-domain.at): '
  read -r DOMAIN < /dev/tty
  [ -n "$DOMAIN" ] || { bang "Ohne Domain kein HTTPS. Abbruch."; exit 1; }

  printf '  Passwort fürs Dashboard: '
  read -rs APP_PASSWORD < /dev/tty; printf '\n'
  [ -n "$APP_PASSWORD" ] || { bang "Ohne Passwort steht es offen. Abbruch."; exit 1; }

  printf '  Garmin verbinden? [j/N] '
  read -r a < /dev/tty
  GM=""; GP=""
  case "$a" in
    [jJyY]*)
      printf '  Garmin E-Mail: ';   read -r GM < /dev/tty
      printf '  Garmin Passwort: '; read -rs GP < /dev/tty; printf '\n' ;;
  esac

  SECRET="$(head -c 48 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_')"
  {
    echo "# Von scripts/vps-beistellen.sh erzeugt. Niemals committen."
    echo "DOMAIN=$DOMAIN"
    echo "PUBLIC_URL=https://$DOMAIN"
    echo "APP_PASSWORD=$APP_PASSWORD"
    echo "SECRET_KEY=$SECRET"
    echo "GARMIN_EMAIL=$GM"
    echo "GARMIN_PASSWORD=$GP"
    echo "TZ=Europe/Vienna"
  } > .env
  chmod 600 .env
  info ".env angelegt (chmod 600)"
fi

# ---------------------------------------------------------------------------
# 3 · Starten — ohne eigenes Caddy, ohne Ports nach außen
# ---------------------------------------------------------------------------
sag "3/4  Container starten"
docker compose -f "$COMPOSE" -p "$PROJEKT" up -d --build
info "gestartet als Projekt '$PROJEKT', Container 'marathon-app'"

info "warte auf Gesundheitsprüfung ..."
ok=0
for _ in $(seq 1 30); do
  if docker exec marathon-app python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)" \
      >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
if [ "$ok" -eq 1 ]; then
  info "App antwortet"
else
  bang "App antwortet nicht"
  docker logs --tail 40 marathon-app || true
  exit 1
fi

# ---------------------------------------------------------------------------
# 4 · Was du im fremden Caddyfile ergänzen musst
# ---------------------------------------------------------------------------
sag "4/4  Letzter Schritt (den kann dir niemand abnehmen)"

cat <<HINWEIS

Dein bestehender Reverse-Proxy weiß noch nichts von uns. Ergänze in seinem
Caddyfile diesen Block — ans Ende, der bestehende bleibt unangetastet:

------------------------------------------------------------------
$DOMAIN {
    encode zstd gzip

    # MCP-Streams laufen lange; ohne diese Timeouts bricht Caddy sie ab.
    reverse_proxy marathon-app:8000 {
        flush_interval -1
        transport http {
            read_timeout 10m
            write_timeout 10m
        }
    }

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        -Server
    }
}
------------------------------------------------------------------

Dann Caddy neu laden (ohne Ausfall für dein anderes Dashboard):
HINWEIS

if [ -n "$PROXY" ]; then
  echo "    docker exec -w /etc/caddy $PROXY caddy reload"
else
  echo "    docker exec -w /etc/caddy <dein-caddy-container> caddy reload"
fi

cat <<HINWEIS2

Voraussetzung: Ein A-Record für $DOMAIN zeigt auf diesen Server.

Danach erreichbar unter:  https://$DOMAIN

Wieder loswerden:  sudo bash scripts/vps-entfernen.sh
HINWEIS2
