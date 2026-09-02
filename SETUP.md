# Einrichtung

Vom leeren Server bis zum verbundenen Claude. Rechne mit 30–45 Minuten, davon
sind 20 Warten auf DNS.

Du brauchst:

- einen kleinen Server mit öffentlicher IP (Empfehlung und Alternativen:
  [docs/hosting.md](docs/hosting.md))
- eine Domain oder Subdomain, die du auf diesen Server zeigen lassen kannst
- deine Garmin-Connect-Zugangsdaten

---

## 1. Domain auf den Server zeigen lassen

Bei deinem Domain-Anbieter einen **A-Record** anlegen:

| Typ | Name | Wert |
|---|---|---|
| A | `marathon` | die IPv4-Adresse deines Servers |

Ergibt `marathon.deine-domain.at`. Prüfen (kann 5–30 Minuten dauern):

```bash
dig +short marathon.deine-domain.at
```

Kommt deine Server-IP zurück, geht's weiter. Vorher nicht — Caddy holt sonst
vergeblich ein Zertifikat und Let's Encrypt bremst dich nach ein paar Versuchen aus.

---

## 2. Server vorbereiten

Auf dem Server, als root:

```bash
apt update && apt upgrade -y
apt install -y docker.io docker-compose-plugin git ufw

# Firewall: nur SSH und HTTPS von außen
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# Nicht als root arbeiten
adduser --disabled-password --gecos "" marathon
usermod -aG docker marathon
```

Ab hier als `marathon` weiterarbeiten: `su - marathon`

---

## 3. Projekt holen und konfigurieren

```bash
git clone https://github.com/SimonBassani/marathon-dashboard.git marathon-dashboard
cd marathon-dashboard
cp .env.example .env
```

Jetzt `.env` ausfüllen (`nano .env`). Die zwei Geheimnisse erzeugst du so — jeweils
einmal ausführen und das Ergebnis eintragen:

```bash
python3 -c "import secrets; print('APP_PASSWORD=' + secrets.token_urlsafe(18))"
python3 -c "import secrets; print('SECRET_KEY='   + secrets.token_urlsafe(48))"
```

Ausgefüllt sieht `.env` etwa so aus:

```ini
DOMAIN=marathon.deine-domain.at
PUBLIC_URL=https://marathon.deine-domain.at
APP_PASSWORD=Xk3p...              # das tippst du beim Anmelden
SECRET_KEY=9fQ2...                # nie wieder anfassen
GARMIN_EMAIL=du@example.com
GARMIN_PASSWORD=dein-garmin-passwort
TZ=Europe/Vienna
```

> **`APP_PASSWORD` merken.** Es ist gleichzeitig das Passwort, mit dem du später
> den Claude-Zugang freigibst. Änderst du es später, werden alle Geräte abgemeldet
> — das ist der schnellste Weg, einen Zugang zu sperren. `SECRET_KEY` musst du dir
> nicht merken; ihn zu ändern meldet ebenfalls alle Geräte ab.

> **Warum steht das Garmin-Passwort im Klartext in der Datei?** Weil sich der Sync
> sonst nicht anmelden kann und es keinen Passwortspeicher gibt, der das besser
> löst, ohne dass du bei jedem Neustart selbst etwas eintippst. Die Datei gehört
> nur deinem Benutzer und liegt nicht in Git. Wenn dir das zu heikel ist: leg dir
> ein eigenes Garmin-Konto nur fürs Dashboard an und teile die Aktivitäten dorthin.

`.env` absichern:

```bash
chmod 600 .env
```

---

## 4. Starten

```bash
docker compose up -d --build
docker compose logs -f
```

Beim ersten Start passiert dreierlei: Caddy holt das HTTPS-Zertifikat (dauert ein
paar Sekunden), die Datenbank wird angelegt, und der Garmin-Sync holt die letzten
drei Tage. Im Log siehst du:

```
app  | Datenbank bereit: /data/marathon.db
app  | Login über Token-Cache
app  | Sync (startup): {'ok': True, 'written': 143, ...}
```

Mit `Strg+C` aus dem Log aussteigen (die Container laufen weiter).

Jetzt `https://marathon.deine-domain.at` aufrufen und mit `APP_PASSWORD` anmelden.

**Am Handy:** Seite in Safari bzw. Chrome öffnen → Teilen → „Zum Home-Bildschirm".
Dann startet sie wie eine App, ohne Browser-Leiste.

---

## 5. Rennen und Plan anlegen

Unter **Setup**:

1. **Zielrennen** — Name, Renntag, Zielzeit (z. B. `3:45`), Distanz.
2. **Trainingsplan erzeugen** — aktueller Wochenumfang und deine Lauftage.
   Der Plan geht bis zum Renntag und darf jederzeit neu erzeugt werden.

Beim Umfang ehrlich sein: eintragen, was du **tatsächlich** in den letzten Wochen
gelaufen bist, nicht was du dir vorgenommen hast. Der ganze Aufbau rechnet darauf.

---

## 6. Die volle Historie nachladen

Der laufende Sync holt nur die letzten Tage. Einmalig alles nachladen:

```bash
docker compose exec app python -m app.garmin 365
```

Das dauert einige Minuten. Danach hat die Prognose eine ordentliche Grundlage.

---

## 7. Claude verbinden

→ **[docs/claude-verbinden.md](docs/claude-verbinden.md)**

Kurzfassung: In Claude unter *Einstellungen → Connectors → Connector hinzufügen*
die Adresse `https://marathon.deine-domain.at/mcp` eintragen und mit dem
`APP_PASSWORD` bestätigen.

---

## Es läuft nicht — woran liegt's

**Seite nicht erreichbar / Zertifikatsfehler**

```bash
dig +short marathon.deine-domain.at    # zeigt der A-Record auf den Server?
docker compose logs caddy | tail -30   # was sagt Caddy?
```

Häufigste Ursache: der A-Record war beim ersten Start noch nicht aktiv.
`docker compose restart caddy` und ein paar Minuten warten.

**„Passwort stimmt nicht", obwohl es stimmt**

Nach dem Ändern von `.env` muss neu gestartet werden:
`docker compose up -d`. Und: nach acht Fehlversuchen sperrt das Dashboard
fünf Minuten lang.

**Keine Garmin-Daten**

```bash
docker compose logs app | grep -i garmin
```

- `GARMIN_EMAIL/PASSWORD fehlen` → `.env` unvollständig
- `401` oder `403` → falsches Passwort, oder Garmin verlangt gerade eine
  Bestätigung. Einmal in der Garmin-App anmelden, dann
  `docker compose restart app`
- Einzelne Zeilen wie `sleep 2026-08-12: ...` sind normal: der betreffende Tag
  hat keine Daten (Uhr nicht getragen). Der Rest läuft weiter.

**App startet nicht, Log sagt `SECRET_KEY fehlt oder ist zu kurz`**

Der Schlüssel braucht mindestens 32 Zeichen. Neu erzeugen (siehe Schritt 3).
Das ist Absicht: mit einem schwachen Schlüssel könnte jemand sich selbst ein
gültiges Sitzungs-Cookie ausstellen.

**Container startet immer wieder neu**

```bash
docker compose ps                          # RestartCount anschauen
docker compose logs app --tail 50
```

Ein Container in einer Neustartschleife ist manchmal ein Absturz — und manchmal
ein Einbruchsversuch, der am schreibgeschützten Dateisystem scheitert. Beides
steht im Log. Nicht wegklicken.

---

## Sicherung

Die gesamte Datenbank ist eine Datei. Sicherung auf deinen Rechner:

```bash
docker compose exec app python -c "
import sqlite3, shutil
src = sqlite3.connect('/data/marathon.db')
dst = sqlite3.connect('/data/backup.db')
src.backup(dst)          # konsistent, auch während geschrieben wird
"
docker compose cp app:/data/backup.db ./marathon-backup-$(date +%F).db
```

Wiederherstellen: Datei nach `/data/marathon.db` zurückspielen und
`docker compose restart app`.

Einmal im Monat reicht. Verlierst du sie, sind nur deine Notizen und Bewertungen
weg — die Garmin-Daten holt der Sync neu.
