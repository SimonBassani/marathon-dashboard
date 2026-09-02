# Marathon-Dashboard

Ein Trainings-Dashboard für die Marathonvorbereitung. Holt sich die Daten selbst
von Garmin, rechnet sie in Aussagen um statt in Zahlenfriedhöfe — und Claude kann
es lesen **und beschreiben**. Du planst deine Wochen also im Chat, am Handy,
unterwegs. Der Plan landet direkt im Dashboard.

Ein Container, eine Datei als Datenbank, kein Node, kein Build. Läuft auf einem
4-Euro-Server.

---

## Schnellstart

**Nur mal anschauen (5 Minuten, kein Server, kein Garmin-Konto nötig):**

```bash
git clone https://github.com/SimonBassani/marathon-dashboard.git marathon-dashboard && cd marathon-dashboard

python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Demodaten, damit gleich was zu sehen ist
DB_PATH=./demo.db .venv/bin/python scripts/seed_demo.py

DB_PATH=./demo.db \
APP_PASSWORD=demo-passwort-1234 \
SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))") \
PUBLIC_URL=http://localhost:8000 \
.venv/bin/uvicorn app.main:app --port 8000
```

[localhost:8000](http://localhost:8000) öffnen, Passwort `demo-passwort-1234`.

**Richtig aufsetzen:** die sechs Schritte weiter unten. Rechne mit 40 Minuten,
davon 20 Warten auf DNS.

---

## Was es dir zeigt

| | |
|---|---|
| **Wo du stehst** | Countdown, Trainingsphase, Wochenkilometer, Steigerungsrate, Belastungsverhältnis (ACWR), 80/20-Intensitätsverteilung |
| **Was rauskommt** | Marathonprognose aus zwei Quellen — Garmins Schätzung und einer Hochrechnung aus deinen echten Läufen — plus Abgleich mit deiner Zielzeit |
| **Was schiefläuft** | Konkrete Warnungen: zu schnelle Steigerung, HRV im Keller, Ruhepuls erhöht, zu wenig Schlaf, zu viel Mitteltempo, langer Lauf zu kurz |
| **Der Plan** | Periodisiert bis zum Renntag: Grundlage → Aufbau → Peak → Taper. Drei Wochen steigern, eine entlasten. Trainingspaces aus deiner Zielzeit abgeleitet |
| **Jeder Lauf** | Kilometersplits, HF-Zonen, aerobe Entkopplung, plus deine eigene Bewertung |

### Aerobe Entkopplung — die Zahl, die deine Uhr nicht zeigt

Wandert dein Puls in der zweiten Hälfte eines langen Laufs bei **gleicher Pace**
nach oben, kostet dich dasselbe Tempo gegen Ende mehr als am Anfang. Genau das
passiert im Marathon ab Kilometer 30.

- **unter 5 %** — die Grundlage trägt diese Dauer
- **über 5 %** — erst mehr ruhige Kilometer, dann Tempoarbeit

Das Dashboard rechnet den Wert für jeden längeren Lauf aus den Rohdaten aus.
Garmin selbst weist ihn nicht aus.

---

## Einrichtung in sechs Schritten

Du brauchst: einen kleinen Server mit öffentlicher IP, eine Domain (oder
Subdomain), und dein Garmin-Connect-Login.

> **Warum ein öffentlicher Server?** Der Claude-Connector verbindet sich aus
> Anthropics Cloud, nicht von deinem Handy. Das Dashboard muss also erreichbar
> sein und durchlaufen. Ohne Claude-Zugang reicht dein eigener Rechner —
> Details in [docs/hosting.md](docs/hosting.md).

### 1 · Server mieten · 5 Min

Bei [Hetzner Cloud](https://www.hetzner.com/cloud) einen **CX22** (rund
4,50 €/Monat) in Nürnberg oder Falkenstein anlegen, Betriebssystem **Ubuntu**.
IP-Adresse notieren.

Kostenlose Alternativen und ihre Haken: [docs/hosting.md](docs/hosting.md).

### 2 · Domain drauf zeigen lassen · 5 Min + 20 Min warten

Beim Domain-Anbieter einen **A-Record** anlegen: Name `marathon`, Wert deine
Server-IP. Dann prüfen:

```bash
dig +short marathon.deine-domain.at
```

Kommt deine IP zurück, weiter. **Vorher nicht** — der Server holt sonst
vergeblich ein HTTPS-Zertifikat und wird danach eine Weile ausgebremst.

### 3 · Server vorbereiten · 5 Min

Auf dem Server als root:

```bash
apt update && apt upgrade -y
apt install -y docker.io docker-compose-plugin git ufw

ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable

adduser --disabled-password --gecos "" marathon
usermod -aG docker marathon
su - marathon
```

### 4 · Projekt holen und Zugangsdaten eintragen · 10 Min

```bash
git clone https://github.com/SimonBassani/marathon-dashboard.git marathon-dashboard
cd marathon-dashboard
cp .env.example .env

# Die zwei Geheimnisse erzeugen und ins .env eintragen
python3 -c "import secrets; print('APP_PASSWORD=' + secrets.token_urlsafe(18))"
python3 -c "import secrets; print('SECRET_KEY='   + secrets.token_urlsafe(48))"

nano .env
chmod 600 .env
```

Ausgefüllt sieht `.env` so aus:

```ini
DOMAIN=marathon.deine-domain.at
PUBLIC_URL=https://marathon.deine-domain.at
APP_PASSWORD=Xk3p…              # das tippst du beim Anmelden
SECRET_KEY=9fQ2…                # brauchst du dir nicht zu merken
GARMIN_EMAIL=du@example.com
GARMIN_PASSWORD=dein-garmin-passwort
TZ=Europe/Vienna
```

> **`APP_PASSWORD` merken.** Damit gibst du später auch Claude den Zugriff frei.
> Änderst du es, fliegen alle Geräte raus — das ist dein Notaus.

> Ja, dein Garmin-Passwort steht im Klartext in der Datei. Anders kann sich der
> Sync nicht anmelden. Die Datei gehört nur dir und landet nie in Git. Zu heikel?
> Leg dir ein zweites Garmin-Konto nur fürs Dashboard an.

### 5 · Starten · 5 Min

```bash
docker compose up -d --build
docker compose logs -f          # mit Strg+C wieder raus, Container laufen weiter
```

Dann `https://marathon.deine-domain.at` aufrufen, mit `APP_PASSWORD` anmelden,
und unter **Setup** eintragen:

1. **Zielrennen** — Name, Renntag, Zielzeit (z. B. `3:45`)
2. **Trainingsplan erzeugen** — aktueller Wochenumfang und deine Lauftage

Beim Umfang ehrlich sein: was du *wirklich* gelaufen bist, nicht was du dir
vornimmst. Der ganze Aufbau rechnet auf dieser Zahl.

Einmalig noch die volle Historie nachladen (läuft ein paar Minuten):

```bash
docker compose exec app python -m app.garmin 365
```

**Am Handy:** Seite öffnen → Teilen → „Zum Home-Bildschirm". Startet dann wie
eine App.

### 6 · Claude verbinden · 2 Min

In Claude: **Einstellungen → Connectors → Eigenen Connector hinzufügen**, als
Adresse deine Domain mit `/mcp` am Ende:

```
https://marathon.deine-domain.at/mcp
```

Claude schickt dich auf dein eigenes Dashboard, dort steht wer Zugriff will und
wohin es zurückleitet. `APP_PASSWORD` eingeben, freigeben, fertig. Gilt danach
für alle deine Geräte.

Details, Werkzeugliste und wie du den Zugang wieder entziehst:
[docs/claude-verbinden.md](docs/claude-verbinden.md).

---

## So nutzt du's dann

Das meiste geht direkt im Dashboard und kostet keine Claude-Nutzung. Für Claude
hebst du dir die Fragen auf, bei denen jemand nachdenken soll:

| Wann | Was du sagst |
|---|---|
| Sonntag | „Plan mir die kommende Woche. Mittwoch hab ich keine Zeit, der lange Lauf soll auf Sonntag." |
| Nach dem langen Lauf | „Der lange Lauf gestern war zäh. Schau ihn dir an." |
| Wenn's zwickt | „Merk dir: rechte Achillessehne zwickt seit dem Tempolauf." |
| Alle paar Wochen | „Ist 3:45 realistisch, so wie es gerade läuft?" |

In Claude Code gibt es dafür fertige Befehle: `/woche` plant die Woche,
`/formcheck` gibt eine ehrliche Einschätzung, `/check` prüft vor einem Update.

**Wenn dir die Claude-Nutzung ausgeht:** die vier wirksamsten Hebel stehen in
[docs/tokens-sparen.md](docs/tokens-sparen.md). Kurzfassung: nach jeder
erledigten Sache `/clear`, und im Chat offene Fragen stellen statt Listen
anfordern — ein Trainingsstand-Briefing kostet so rund 220 Tokens statt 30.000.

---

## Wenn was klemmt

| Symptom | Wahrscheinlich |
|---|---|
| Seite nicht erreichbar, Zertifikatsfehler | A-Record war beim ersten Start noch nicht aktiv. `docker compose restart caddy`, paar Minuten warten |
| „Passwort stimmt nicht", obwohl es stimmt | Nach `.env`-Änderung fehlt `docker compose up -d`. Oder: nach 8 Fehlversuchen sperrt es 5 Minuten |
| Keine Garmin-Daten | `docker compose logs app \| grep -i garmin`. Bei 401/403 einmal in der Garmin-App anmelden, dann `docker compose restart app` |
| Claude sagt, es gäbe keine Daten | Sync läuft nicht — Stand steht unten auf der Startseite |
| Container startet ständig neu | `docker compose logs app --tail 50`. Manchmal Absturz, manchmal ein Einbruchsversuch, der am schreibgeschützten Dateisystem scheitert |

Ausführlich mit allen Befehlen: [SETUP.md](SETUP.md#es-läuft-nicht--woran-liegts).

---

## Wartung — einmal im Monat, fünf Minuten

```bash
docker compose exec app pip-audit -r requirements.txt
docker compose pull && docker compose up -d --build
```

Der Punkt, den alle vergessen — und der Grund, warum die meisten gekaperten
Hobby-Server gekapert werden. Nicht wegen schlechtem Code, sondern wegen einer
veralteten Abhängigkeit mit bekannter Lücke.

Sicherung (die ganze Datenbank ist eine Datei), siehe [SETUP.md](SETUP.md#sicherung).

---

## Aufbau

```
Browser / Handy ─┐
                 ├──→ Caddy (HTTPS) ──→ FastAPI ──→ SQLite-Datei
Claude (MCP)   ──┘                         │
                                           └──→ Garmin Connect (alle 45 min)
```

| Datei | Inhalt |
|---|---|
| `app/main.py` | Routen, Login, Sicherheits-Header, MCP-Einbindung |
| `app/db.py` | SQLite-Schema und Zugriff |
| `app/garmin.py` | Garmin-Sync, Berechnung der aeroben Entkopplung |
| `app/analysis.py` | Umfang, ACWR, Prognose, Warnungen |
| `app/plan.py` | Trainingsplan-Generator |
| `app/mcp_server.py` | Die elf Werkzeuge, die Claude sieht |
| `app/oauth.py` | OAuth 2.1 für den Claude-Connector |
| `app/views.py` | Diagramme als SVG, serverseitig gerendert |

Kein Node, kein Build-Schritt, keine Chart-Bibliothek. Das ist kein Purismus:
weniger, das kaputtgehen kann; weniger, das monatlich auf Sicherheitslücken
geprüft werden muss; und deutlich weniger Code, durch den Claude Code sich lesen
muss, wenn du etwas änderst.

**Tests:** `.venv/bin/pytest -q` — 53 Stück, unter zwei Sekunden. Rechenlogik,
Login, Schutz des MCP-Endpunkts und der komplette OAuth-Ablauf inklusive der
Fälle, die scheitern müssen.

---

## Grenzen, ehrlich gesagt

- **Garmin hat keine offizielle Schnittstelle für Privatpersonen.**
  `garminconnect` spricht dieselben Endpunkte wie die Website. Läuft seit Jahren
  stabil, kann aber jederzeit brechen. Deshalb ist jeder Abrufblock einzeln
  abgesichert: fällt einer aus, laufen die anderen weiter.
- **Die Prognose ist eine Schätzung.** Ohne langen Lauf über 30 km in den Beinen
  ist jede Marathonhochrechnung optimistisch — das Dashboard sagt dir das auch
  (Feld „Verlässlichkeit").
- **Der Trainingsplan ersetzt keinen Trainer.** Er ist ein solider
  Standardaufbau. Bei Verletzungshistorie oder ambitionierten Zielen ist ein
  Mensch besser.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
