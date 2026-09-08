# Auf einen Server bringen — und danach nie wieder anfassen

Ziel: Das Dashboard läuft unter deiner Domain mit HTTPS, und jede Änderung im
Repo landet von selbst darauf. Du machst zwei Dinge einmal, danach nichts mehr.

Ohne Server geht der Claude-Zugang nicht: Der Connector verbindet sich aus
Anthropics Cloud, nicht von deinem Handy. Warum das so ist und welche Anbieter
in Frage kommen, steht in [hosting.md](hosting.md).

---

## Einmal: Server aufsetzen

Auf einem frischen Ubuntu-Server (Hetzner CX22 reicht dicke):

```bash
curl -fsSL https://github.com/SimonBassani/marathon-dashboard/raw/main/scripts/server-setup.sh -o setup.sh
less setup.sh          # bitte wirklich lesen, bevor du etwas als root ausführst
sudo bash setup.sh
```

Es fragt nach Domain, Dashboard-Passwort und optional den Garmin-Zugangsdaten.
Alles andere macht es: Docker, eigener Benutzer ohne root-Rechte, Firewall auf
22/80/443, Container starten, HTTPS-Zertifikat holen, Gesundheitsprüfung.

**Vorher**: Ein A-Record deiner Domain muss auf die Server-IP zeigen. Ohne das
bekommt Caddy kein Zertifikat.

Läuft auf dem Server schon etwas auf Port 80 oder 443, bricht das Skript ab,
statt dir den bestehenden Dienst wegzunehmen.

---

## Einmal: Automatisches Ausrollen einschalten

Am Ende gibt das Skript fünf Werte aus. Die trägst du hier ein:

**Repo → Settings → Secrets and variables → Actions → New repository secret**

| Secret | Inhalt |
|---|---|
| `DEPLOY_HOST` | IP des Servers |
| `DEPLOY_USER` | `marathon` |
| `DEPLOY_PATH` | `/opt/marathon-dashboard` |
| `DEPLOY_KEY` | der private Schlüssel, komplett mit `BEGIN`- und `END`-Zeile |
| `DEPLOY_KNOWN_HOSTS` | die `ssh-keyscan`-Zeile |

Danach das Terminal schließen — der private Schlüssel stand dort im Klartext.

Ab jetzt: **jeder Push auf `main` rollt aus.** Der Workflow prüft danach nach,
ob die App wirklich antwortet, und schlägt fehl, wenn nicht — inklusive der
letzten 60 Log-Zeilen im Lauf-Protokoll.

Von Hand auslösen geht auch: **Actions → Deploy → Run workflow**.

---

## Wie sicher ist das?

Der Schlüssel liegt in den GitHub-Secrets, nicht bei mir und nicht im Repo. Was
im Workflow bewusst so entschieden ist:

- **Keine einzige fremde Action.** Nur `run:`-Schritte. Eine Action Dritter in
  einem Workflow mit Produktionsschlüssel wäre genau das Lieferkettenrisiko, das
  dieses Projekt sonst überall vermeidet.
- **Kein `pull_request` als Auslöser.** Das Repo ist öffentlich; sonst könnte
  jeder per Fork-PR einen Lauf anstoßen, der an die Secrets kommt.
- **`permissions: contents: read`**, mehr braucht der Workflow nicht.
- **Host-Schlüssel wird geprüft** (`known_hosts` statt
  `StrictHostKeyChecking=no`). Ein untergeschobener Server fliegt auf.
- **Der Deploy-Benutzer ist nicht root.** Er darf Docker steuern, sonst nichts.

Was du trotzdem selbst tun solltest: **einmal im Monat** `docker compose pull`
und neu bauen. Die meisten gekaperten Hobby-Server fallen nicht über schlechten
eigenen Code, sondern über eine veraltete Abhängigkeit.

---

## Zugang wieder entziehen

Wenn du den automatischen Deploy abschalten willst:

```bash
# auf dem Server
sudo sed -i '/github-actions-deploy/d' /home/marathon/.ssh/authorized_keys
```

Danach laufen die Workflows ins Leere. Die App selbst läuft weiter.

---

## Es klappt nicht

| Symptom | Ursache |
|---|---|
| Workflow rot bei „Zugang vorbereiten" | Ein Secret fehlt oder ist leer |
| `Host key verification failed` | `DEPLOY_KNOWN_HOSTS` passt nicht zur IP — Server neu aufgesetzt? Dann `ssh-keyscan -t ed25519 <IP>` neu ausführen |
| `Permission denied (publickey)` | `DEPLOY_KEY` unvollständig kopiert (die `BEGIN`/`END`-Zeilen gehören dazu) |
| Deploy grün, aber Seite lädt nicht | Meist das Zertifikat. `docker compose logs caddy` |
| „App antwortet nach dem Deploy nicht" | Die letzten 60 Zeilen stehen im Lauf-Protokoll |
