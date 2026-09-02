# Marathon-Dashboard — Arbeitsanweisungen

> Diese Datei wird in **jeder** Sitzung mitgelesen. Sie bleibt deshalb kurz.
> Details stehen in `README.md`, `SETUP.md` und `docs/` — dort nachschlagen,
> wenn es gebraucht wird, nicht vorsorglich.

## Was das ist

Persönliches Marathon-Trainings-Dashboard. FastAPI + SQLite, **ein** Prozess.
Holt Daten von Garmin Connect, wertet sie aus, und stellt über MCP einen
Remote-Zugang für Claude bereit.

## Wo was liegt

| Aufgabe | Datei |
|---|---|
| Routen, Login, Sicherheits-Header, MCP-Einbindung | `app/main.py` |
| SQLite-Schema und Zugriff | `app/db.py` |
| Garmin-Sync, aerobe Entkopplung | `app/garmin.py` |
| Auswertung: Umfang, ACWR, Prognose, Warnungen | `app/analysis.py` |
| Trainingsplan-Generator | `app/plan.py` |
| MCP-Werkzeuge für Claude | `app/mcp_server.py` |
| OAuth 2.1 für den Connector | `app/oauth.py` |
| Passwort, Cookie, Token-Hilfen | `app/security.py` |
| Diagramme als SVG | `app/views.py` |
| Oberfläche | `app/templates/`, `app/static/` |

Bei einer Änderung **zuerst hier nachsehen und nur die betroffene Datei öffnen.**
Das Projekt ist so geschnitten, dass das reicht.

## Regeln

**Immer Tests laufen lassen, bevor du „fertig" sagst:**

```bash
.venv/bin/pytest -q
```

Läuft in unter zwei Sekunden. Eine Behauptung über Funktionieren ohne
Testlauf gilt nicht.

**Neue Abhängigkeiten:** nur wenn es ohne wirklich nicht geht, und dann exakt
gepinnt in `requirements.txt` (kein `>=`, kein `^`). Danach
`.venv/bin/pip-audit -r requirements.txt`. Der Verzicht auf Node, Chart-Bibliotheken
und Fremd-OAuth-Pakete ist eine bewusste Entscheidung, keine Lücke.

**Sicherheitsrelevant — hier nichts vereinfachen:**
- `app/oauth.py` — PKCE-Pflicht, exakter Abgleich der Rückleitungsziele,
  einmalige Codes, Tokens nur als Hash. Jede dieser vier Eigenschaften hat einen
  eigenen Test.
- `app/security.py` — Passwortvergleich in konstanter Laufzeit, signierte Cookies.
- `SecurityMiddleware` in `app/main.py` — der 401 auf `/mcp` **muss** den
  `WWW-Authenticate`-Header behalten, sonst findet Claude den OAuth-Ablauf nicht.
- Nutzereingaben nie ungeprüft in SQL. Es wird ausschließlich mit Platzhaltern
  gearbeitet (`?`), nirgends mit String-Verkettung.

**Antworten der MCP-Werkzeuge kompakt halten.** Sie geben ausgewerteten Text
zurück, keine Rohdaten — das ist der Grund, warum eine Frage im Chat rund 200
statt 30.000 Tokens kostet. `test_mcp_tools_produce_compact_text` wacht darüber.
Wenn du ein Werkzeug ergänzt: gleiche Bauart.

**Sprache:** Alles, was der Nutzer liest — Oberfläche, Warnungen,
Werkzeugbeschreibungen, Fehlermeldungen — auf Deutsch mit echten Umlauten.
Bezeichner im Code bleiben, wie sie sind.

## Handgriffe

```bash
# Tests
.venv/bin/pytest -q

# Lokal starten (mit Demodaten, ohne Garmin)
DB_PATH=./demo.db .venv/bin/python scripts/seed_demo.py
DB_PATH=./demo.db APP_PASSWORD=demo-passwort-1234 PUBLIC_URL=http://localhost:8000 \
  SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))") \
  .venv/bin/uvicorn app.main:app --port 8000 --reload

# Garmin-Sync von Hand (Argument = Tage rückwärts)
docker compose exec app python -m app.garmin 30

# Auf dem Server neu ausrollen
docker compose up -d --build
```

## Was nicht dazugehört

Das hier ist ein Marathon-Dashboard, kein Lebens-Betriebssystem. Aufgaben,
Kalender, Mail, Gewohnheiten, Finanzen: bewusst nicht drin. Schlank bleiben ist
hier eine Anforderung, kein Zwischenstand.
