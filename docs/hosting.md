# Wo soll das Ding laufen?

Kurze Antwort: **ein 4-Euro-VPS bei Hetzner.** Die längere Antwort erklärt, warum
die kostenlosen Varianten hier meistens nicht passen — und wann doch.

---

## Die Bedingung, die alles entscheidet

Wenn Claude im Browser oder am Handy auf dein Dashboard zugreifen soll, verbindet
sich **nicht dein Handy** damit, sondern Anthropics Server. Der Connector läuft aus
der Cloud, nicht vom Gerät aus.

Daraus folgt hart:

- Das Dashboard muss **öffentlich über HTTPS erreichbar** sein.
- Es muss **durchgehend laufen** — ein Dienst, der nach 15 Minuten Leerlauf
  einschläft, ist mitten im Chat nicht da.
- Es braucht eine **stabile Adresse**, weil sie einmalig im Connector hinterlegt wird.

Genau an diesen drei Punkten scheitern die meisten Gratis-Angebote.

Willst du **nur das Dashboard** und keinen Claude-Zugang, entfällt das alles: dann
reicht dein eigener Rechner oder ein Raspberry Pi zuhause, und es kostet nichts.

---

## Die Optionen

### 1. Hetzner Cloud CX22 — rund 4,50 €/Monat · **Empfehlung**

2 vCPU, 4 GB RAM, 40 GB SSD. Rechenzentren in Nürnberg, Falkenstein und Helsinki
(kurze Wege, DSGVO unkompliziert). Für dieses Dashboard ist die Maschine
großzügig überdimensioniert — der Container braucht keine 200 MB.

Vorteile: läuft einfach, stundenweise Abrechnung, Snapshots, saubere Konsole für
den Fall, dass du dich aussperrst.

Der einzige Nachteil ist, dass es Geld kostet. Über die neun Monate bis zum
Marathon sind das etwa 40 Euro — ungefähr ein Paar Laufschuhsohlen.

### 2. Oracle Cloud „Always Free" — 0 € · die ernstzunehmende Gratis-Option

Oracle vergibt dauerhaft kostenlos ARM-Instanzen mit bis zu 4 Kernen und 24 GB RAM.
Das ist mit Abstand das großzügigste Gratis-Angebot, das es gibt, und es ist kein
Zeitlimit-Trick.

Was du wissen solltest, bevor du dich darauf verlässt:

- Die Registrierung ist zäh (Kreditkarte zur Verifizierung, manchmal manuelle Prüfung).
- Die ARM-Kapazität ist in beliebten Regionen oft ausgebucht — „Out of capacity"
  beim Anlegen ist normal, manchmal über Tage.
- Oracle behält sich vor, **ungenutzte** Always-Free-Instanzen bei Kapazitätsengpässen
  zurückzuholen. Unser Container läuft rund um die Uhr und synct alle 45 Minuten,
  gilt also nicht als untätig — aber eine Zusage ist das nicht.

**Wenn du sparen willst und einen Nachmittag Geduld hast: nimm das.** Wenn dir
wichtiger ist, dass das Ding bis April einfach läuft, nimm Hetzner.

### 3. Zuhause auf einem Raspberry Pi oder alten Rechner — 0 € plus Strom

Funktioniert gut, und die Daten bleiben bei dir. Für die öffentliche Erreichbarkeit
brauchst du einen Tunnel — **Cloudflare Tunnel** ist dafür kostenlos und
verlangt keine offenen Ports im Router.

Ehrlicher Haken: Stromausfall, Router-Neustart oder ein Wohnungswechsel bedeuten,
dass Claude gerade nichts sieht. Für ein Hobbyprojekt in Ordnung, aber du bist
der Systemadministrator.

### 4. Was hier **nicht** funktioniert

| Angebot | Warum nicht |
|---|---|
| **Fly.io** | Der kostenlose Tarif wurde im Oktober 2024 eingestellt. Der kleinste bezahlte Einstieg liegt bei 5 $/Monat — also teurer als Hetzner bei weniger Leistung. |
| **Render Free** | Der Dienst schläft nach Leerlauf ein und braucht danach fast eine Minute zum Aufwachen. Der Garmin-Sync läuft dann nicht, und Claude läuft in eine Zeitüberschreitung. |
| **Vercel / Netlify** | Für statische Seiten und kurze Funktionsaufrufe gebaut. Kein dauerhafter Prozess, kein Dateisystem, das eine SQLite-Datei überlebt. |
| **Railway Free** | Gibt es nicht mehr; es ist ein einmaliges Guthaben. |
| **Kostenlose Datenbank plus kostenloses Hosting** | Löst das Problem nicht — es fehlt weiterhin der durchgehend laufende Prozess für Sync und MCP. |

---

## Empfehlung nach Situation

| Du bist … | Nimm |
|---|---|
| … jemand, der will, dass es bis April läuft und sich nicht kümmern mag | **Hetzner CX22**, rund 4,50 €/Monat |
| … bereit, für 0 € einen zähen Nachmittag zu investieren | **Oracle Always Free** |
| … noch unsicher, ob du das Dashboard überhaupt nutzt | **erst lokal ausprobieren** (siehe README), später umziehen |
| … nur am Dashboard interessiert, Claude-Zugang egal | **Raspberry Pi oder eigener Rechner**, kostet nichts |

Ein Umzug ist übrigens billig: Die gesamte Datenbank ist **eine Datei**. Kopieren,
`.env` mitnehmen, `docker compose up -d`. Fang also ruhig klein an.

---

## Was der Betrieb sonst noch kostet

- **Domain**: 5–15 €/Jahr. Hast du schon eine, reicht eine Subdomain — kostet nichts extra.
- **HTTPS-Zertifikat**: 0 €, Caddy holt es automatisch bei Let's Encrypt.
- **Datenverkehr**: vernachlässigbar. Das Dashboard überträgt pro Aufruf rund 30 KB.

---

## Absicherung, wenn es öffentlich steht

Ist bereits eingebaut, aber zum Nachlesen:

- **Passwortschutz** auf allen Seiten, Sperre nach 8–10 Fehlversuchen
- **OAuth 2.1 mit PKCE** für den Claude-Zugang, Tokens nur als Hash gespeichert
- **Schreibgeschütztes Dateisystem** im Container, alle Berechtigungen abgegeben
  (`cap_drop: ALL`), `no-new-privileges`, `/tmp` ohne Ausführungsrecht
- **Nicht als root**, feste Speicher- und CPU-Grenze
- **Absender-IP nur vom eigenen Reverse-Proxy** akzeptiert, plus ein Auffangzähler,
  der sich nicht durch gefälschte Adressen zurücksetzen lässt
- **Zugang je Client entziehbar**, Passwortwechsel beendet alle Sitzungen sofort
- **Sicherheits-Header** inklusive Content-Security-Policy ohne externe Quellen

Was du selbst tun musst:

1. `.env` auf `chmod 600` und niemals in Git.
2. Firewall an — nur 22, 80, 443 (steht in [SETUP.md](../SETUP.md)).
3. **Einmal im Monat:** `pip-audit -r requirements.txt`, dann
   `docker compose pull && docker compose up -d --build`.

Punkt 3 ist der wichtigste und der, den alle vergessen. Die meisten gekaperten
Hobby-Server werden nicht über schlechten eigenen Code übernommen, sondern über
eine veraltete Abhängigkeit mit bekannter Lücke.
