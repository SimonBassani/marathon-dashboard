#!/usr/bin/env bash
#
# Einmalige Einrichtung auf einem frischen Ubuntu-/Debian-Server.
#
#     curl -fsSL https://raw.githubusercontent.com/SimonBassani/marathon-dashboard/main/scripts/server-setup.sh -o setup.sh
#     less setup.sh          # bitte wirklich lesen, bevor du es als root ausführst
#     sudo bash setup.sh
#
# Danach läuft das Dashboard unter deiner Domain mit HTTPS, und am Ende bekommst
# du die drei Werte, die du einmal in die GitHub-Secrets einträgst — ab dann
# aktualisiert sich der Server bei jedem Push von selbst.
#
# Was hier NICHT passiert: es wird nichts an bestehenden Diensten verändert.
# Läuft auf dem Server schon etwas auf Port 80/443, bricht das Skript ab und
# sagt es, statt es dir wegzunehmen.

set -euo pipefail

REPO="${REPO:-https://github.com/SimonBassani/marathon-dashboard.git}"
ZIEL="${ZIEL:-/opt/marathon-dashboard}"
DEPLOY_USER="${DEPLOY_USER:-marathon}"

sag()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
bang() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; }

[ "$(id -u)" -eq 0 ] || { bang "Bitte mit sudo starten."; exit 1; }

# ---------------------------------------------------------------------------
# 0 · Ist der Server frei? Lieber abbrechen als etwas kaputtmachen.
# ---------------------------------------------------------------------------
sag "0/6  Server prüfen"
if command -v ss >/dev/null 2>&1; then
  belegt="$(ss -Hltn '( sport = :80 or sport = :443 )' 2>/dev/null | awk '{print $4}' | tr '\n' ' ')"
  if [ -n "${belegt// /}" ] && [ ! -d "$ZIEL" ]; then
    bang "Port 80/443 ist belegt: $belegt"
    info "Auf diesem Server läuft schon etwas (anderes Caddy, nginx, Traefik?)."
    info "Ich fasse das nicht an. Entweder anderen Server nehmen, oder den"
    info "vorhandenen Reverse-Proxy von Hand auf diese App zeigen lassen."
    exit 1
  fi
fi
info "frei bzw. bestehende Installation gefunden"

# ---------------------------------------------------------------------------
# 1 · Docker
# ---------------------------------------------------------------------------
sag "1/6  Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  info "$(docker --version) — schon da"
else
  info "installieren ..."
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git ufw
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  info "$(docker --version)"
fi

# ---------------------------------------------------------------------------
# 2 · Eigener Benutzer. Der Deploy-Zugang ist damit kein root-Zugang.
# ---------------------------------------------------------------------------
sag "2/6  Benutzer $DEPLOY_USER"
if id "$DEPLOY_USER" >/dev/null 2>&1; then
  info "existiert"
else
  adduser --system --group --shell /bin/bash --home "/home/$DEPLOY_USER" "$DEPLOY_USER"
  info "angelegt (System-Benutzer, kein Passwort-Login)"
fi
usermod -aG docker "$DEPLOY_USER"
info "darf Docker steuern"

# ---------------------------------------------------------------------------
# 3 · Code
# ---------------------------------------------------------------------------
sag "3/6  Code holen"
if [ -d "$ZIEL/.git" ]; then
  info "$ZIEL existiert — aktualisieren"
  git -C "$ZIEL" fetch --quiet origin main
  git -C "$ZIEL" reset --quiet --hard origin/main
else
  git clone --quiet "$REPO" "$ZIEL"
  info "nach $ZIEL geklont"
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$ZIEL"

# ---------------------------------------------------------------------------
# 4 · .env — wird nie überschrieben, wenn sie schon existiert
# ---------------------------------------------------------------------------
sag "4/6  Konfiguration"
if [ -f "$ZIEL/.env" ]; then
  info ".env existiert — unverändert gelassen"
else
  umask 077
  printf '  Domain (z.B. marathon.deine-domain.at): '
  read -r DOMAIN < /dev/tty
  [ -n "$DOMAIN" ] || { bang "Ohne Domain kein HTTPS. Abbruch."; exit 1; }

  printf '  Passwort fürs Dashboard: '
  read -rs APP_PASSWORD < /dev/tty; printf '\n'
  [ -n "$APP_PASSWORD" ] || { bang "Ohne Passwort steht das Dashboard offen. Abbruch."; exit 1; }

  printf '  Garmin verbinden? [j/N] '
  read -r a < /dev/tty
  GM=""; GP=""
  case "$a" in
    [jJyY]*)
      printf '  Garmin E-Mail: '; read -r GM < /dev/tty
      printf '  Garmin Passwort: '; read -rs GP < /dev/tty; printf '\n' ;;
  esac

  SECRET="$(head -c 48 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_')"
  {
    echo "# Von scripts/server-setup.sh erzeugt. Niemals committen."
    echo "DOMAIN=$DOMAIN"
    echo "PUBLIC_URL=https://$DOMAIN"
    echo "APP_PASSWORD=$APP_PASSWORD"
    echo "SECRET_KEY=$SECRET"
    echo "GARMIN_EMAIL=$GM"
    echo "GARMIN_PASSWORD=$GP"
    echo "TZ=Europe/Vienna"
  } > "$ZIEL/.env"
  chmod 600 "$ZIEL/.env"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$ZIEL/.env"
  info ".env angelegt (chmod 600)"
fi

# ---------------------------------------------------------------------------
# 5 · Firewall und Start
# ---------------------------------------------------------------------------
sag "5/6  Firewall und Start"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp  >/dev/null 2>&1 || true
  ufw allow 80/tcp  >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  ufw --force enable >/dev/null 2>&1 || true
  info "ufw: nur 22, 80, 443 offen"
fi

cd "$ZIEL"
docker compose up -d --build
info "Container gestartet"

# Warten, bis die App gesund meldet — sonst sagt man "fertig" und es läuft nicht.
info "warte auf Gesundheitsprüfung ..."
ok=0
for _ in $(seq 1 30); do
  if docker compose exec -T app python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)" \
      >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
[ "$ok" -eq 1 ] && info "App antwortet" || bang "App antwortet nicht — 'docker compose logs app' ansehen"

# ---------------------------------------------------------------------------
# 6 · Deploy-Schlüssel für GitHub Actions
# ---------------------------------------------------------------------------
sag "6/6  Automatisches Deployment vorbereiten"
SSH_DIR="/home/$DEPLOY_USER/.ssh"
mkdir -p "$SSH_DIR"; chmod 700 "$SSH_DIR"
KEY="$SSH_DIR/github-deploy"

if [ -f "$KEY" ]; then
  info "Deploy-Schlüssel existiert — nicht neu erzeugt"
  info "Brauchst du ihn nochmal: sudo cat $KEY"
else
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "github-actions-deploy" >/dev/null
  cat "$KEY.pub" >> "$SSH_DIR/authorized_keys"
  chmod 600 "$SSH_DIR/authorized_keys"
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SSH_DIR"
  info "Schlüsselpaar erzeugt, öffentlicher Teil eingetragen"
fi

IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"

cat <<HINWEIS

===========================================================================
FERTIG. Das Dashboard läuft.

    https://$(grep '^DOMAIN=' "$ZIEL/.env" | cut -d= -f2-)

(Beim ersten Aufruf kann das Zertifikat eine Minute brauchen.)

---------------------------------------------------------------------------
Damit sich der Server künftig bei jedem Push selbst aktualisiert, trage
diese vier Werte einmal auf GitHub ein:

  Repo -> Settings -> Secrets and variables -> Actions -> New secret

  DEPLOY_HOST         $IP
  DEPLOY_USER         $DEPLOY_USER
  DEPLOY_PATH         $ZIEL
  DEPLOY_KEY          (der private Schlüssel unten, komplett)
  DEPLOY_KNOWN_HOSTS  (die Zeile darunter)

--- DEPLOY_KEY ------------------------------------------------------------
HINWEIS
cat "$KEY"
cat <<'HINWEIS2'
--- DEPLOY_KNOWN_HOSTS ----------------------------------------------------
HINWEIS2
ssh-keyscan -t ed25519 "$IP" 2>/dev/null || echo "(ssh-keyscan fehlgeschlagen — ssh-keyscan $IP von Hand ausführen)"
cat <<'HINWEIS3'
---------------------------------------------------------------------------
Danach dieses Terminal schließen: der private Schlüssel stand hier im Klartext.
===========================================================================
HINWEIS3
