# Claude mit dem Dashboard verbinden

Danach kannst du im Chat — am Rechner wie am Handy — nach deinem Trainingsstand
fragen und deine Wochen planen lassen. Der Plan landet direkt im Dashboard.

**Voraussetzung:** Das Dashboard läuft öffentlich unter HTTPS (siehe
[SETUP.md](../SETUP.md)). Lokal auf `localhost` funktioniert es nicht — warum,
steht in [hosting.md](hosting.md).

---

## Einrichten (einmalig, 2 Minuten)

1. In Claude auf **Einstellungen → Connectors**.
2. **Connector hinzufügen** → **Eigenen Connector hinzufügen**.
3. Als Adresse eintragen — mit `/mcp` am Ende:

   ```
   https://marathon.deine-domain.at/mcp
   ```

4. Claude leitet dich auf dein eigenes Dashboard weiter. Dort erscheint
   „Claude möchte auf dein Marathon-Dashboard zugreifen".
5. Dein **`APP_PASSWORD`** eingeben → **Zugriff erlauben**.
6. Fertig. Der Connector heißt jetzt `marathon-dashboard`.

Der Zugang gilt für alle deine Claude-Geräte — einmal am Rechner einrichten reicht,
das Handy kann es danach auch.

---

## Was du damit machen kannst

**Trainingsstand abfragen**

> Wie schaut mein Trainingsstand aus?

Countdown, Trainingsphase, Wochenumfang, Belastungsverhältnis, Prognose,
Zielabgleich, Gesundheitswerte, Warnungen — in einem Aufruf.

**Die Woche planen lassen — das ist der eigentliche Punkt**

> Plan mir die kommende Woche. Am Mittwoch hab ich keine Zeit,
> und der Sonntag soll der lange Lauf sein.

Claude ruft erst `trainingsstand` ab, sieht Phase, Umfang und Warnungen, und
schreibt die Woche dann per `plan_schreiben` direkt in den Plan. Öffne danach
das Dashboard — sie steht drin.

**Eine Einheit besprechen**

> Der lange Lauf am Sonntag hat sich zäh angefühlt. Schau ihn dir an.

Claude sieht Splits, Herzfrequenzverlauf und die aerobe Entkopplung und kann
sagen, ob es zu schnell war oder ob die Grundlage noch fehlt.

**Ehrliche Einschätzung einholen**

> Ist 3:45 realistisch, so wie es gerade läuft?

**Etwas festhalten**

> Merk dir: rechte Achillessehne zwickt seit dem Tempolauf.

Landet im Trainingstagebuch und ist beim nächsten Planen berücksichtigt.

---

## Die Werkzeuge im Einzelnen

Du musst sie nicht kennen — Claude wählt selbst. Zur Einordnung:

| Werkzeug | Was es tut |
|---|---|
| `trainingsstand` | Das Gesamtbild in einem Aufruf. Der übliche Einstieg. |
| `wochen` | Wochenübersicht, geplant gegen tatsächlich gelaufen |
| `laeufe` | Die letzten Läufe, eine Zeile pro Lauf |
| `lauf` | Ein Lauf im Detail inklusive Splits |
| `gesundheit` | Schlaf, HRV, Ruhepuls, Readiness im Verlauf |
| `plan_lesen` | Was ist geplant |
| `plan_schreiben` | Einheiten in den Plan schreiben |
| `plan_generieren` | Kompletten Plan bis zum Renntag erzeugen |
| `ziel_setzen` | Rennen und Zielzeit festlegen |
| `notiz` | Eintrag ins Trainingstagebuch |
| `einheit_bewerten` | Anstrengung, Gefühl und Notiz zu einem Lauf |

Alle antworten mit fertig ausgewertetem Text statt mit Rohdaten. Das ist Absicht
und spart erheblich Nutzung — mehr dazu in [tokens-sparen.md](tokens-sparen.md).

---

## Sicherheit

Wer den Connector einrichtet, gibt Claude Lese- **und Schreibzugriff** auf die
Trainingsdaten. Konkret heißt das:

- Claude kann alle Läufe, Gesundheitswerte, Pläne und Notizen lesen.
- Claude kann Plan-Einträge, Notizen und Bewertungen schreiben und Plan-Einträge
  im angefragten Zeitraum **ersetzen**.
- Claude kann **keine** Läufe oder Gesundheitsdaten löschen oder verändern —
  die kommen von Garmin und bleiben, wie sie sind.

Wie der Zugang abgesichert ist:

- **OAuth 2.1 mit PKCE.** Freigabe nur nach Eingabe deines `APP_PASSWORD` auf
  deinem eigenen Server.
- **Nur Anthropic-Adressen** sind als Rückleitungsziel zugelassen. Eine fremde
  Seite kann sich nicht als Client registrieren und den Code abfangen.
- **Tokens werden nur als Hash gespeichert.** Wer die Datenbankdatei liest, kann
  sich damit nicht anmelden.
- **Zugriffstoken laufen nach 24 Stunden ab**, Refresh-Tokens nach 60 Tagen und
  werden bei jeder Erneuerung ausgetauscht.
- Wird ein Autorisierungscode zweimal eingelöst — ein Zeichen dafür, dass er
  abgefangen wurde —, werden **alle** Tokens dieses Clients entwertet.
- **Die Zustimmungsseite nennt Client-Namen und Rückleitungsziel.** Bestätige nur,
  was du gerade selbst in Claude gestartet hast — jeder kann sich als Client
  registrieren, aber ohne dein Passwort bekommt niemand einen Token.
- **Ein Passwortwechsel beendet sofort alle Sitzungen** auf allen Geräten.

**Zugang wieder entziehen:** Im Dashboard unter **Setup → Claude verbinden** steht
jeder verbundene Client mit einem **Entziehen**-Knopf. Das wirkt sofort und auch auf
bereits ausgestellte Tokens — nicht erst, wenn sie ablaufen.

Erkennst du dort einen Client nicht wieder, entzieh ihm den Zugang und ändere
danach dein `APP_PASSWORD`. Das meldet zusätzlich alle Browser-Sitzungen ab.

Alles auf einmal entwerten geht auch von der Kommandozeile:

```bash
docker compose exec app python -c "
from app import db; db.init_db()
db.run('UPDATE oauth_token SET revoked = 1')
print('Alle Zugänge entwertet.')
"
```

---

## Es klappt nicht

**„Verbindung fehlgeschlagen" beim Hinzufügen**

Prüf die Adresse: `https://` und `/mcp` am Ende, kein Schrägstrich dahinter.
Dann:

```bash
curl -i https://marathon.deine-domain.at/.well-known/oauth-protected-resource
```

Erwartet: `200` und ein JSON mit deiner Adresse. Kommt `500`, ist `PUBLIC_URL`
in der `.env` nicht oder falsch gesetzt — sie muss exakt der öffentlichen Adresse
entsprechen, inklusive `https://` und ohne Schrägstrich am Ende.

**Anmeldeseite erscheint, Passwort wird nicht angenommen**

Es ist das `APP_PASSWORD` aus der `.env`, nicht dein Garmin- oder Claude-Passwort.
Nach acht Fehlversuchen wird fünf Minuten gesperrt.

**Verbunden, aber Claude sagt, es gäbe keine Daten**

Dann läuft der Garmin-Sync nicht. Unten auf der Dashboard-Startseite steht, wann
er zuletzt lief und ob er erfolgreich war. Fehlersuche in
[SETUP.md](../SETUP.md#es-läuft-nicht--woran-liegts).

**Ging, geht jetzt nicht mehr**

Meistens ein abgelaufener Token. In Claude den Connector einmal trennen und neu
verbinden.
