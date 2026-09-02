# Mit Claude Pro auskommen

Wenn dir beim Bauen ständig die Nutzung ausgeht, liegt das fast nie am Modell und
fast immer daran, **wie** gearbeitet wird. Dieses Dokument ist die Sammlung der
Hebel, sortiert nach Wirkung.

Der größte davon ist schon eingebaut: Du musst dieses Dashboard nicht bauen. Es ist
fertig. Claude Code muss es nur noch für dich einrichten und anpassen — und das ist
ein Bruchteil des Aufwands.

---

## Warum die Nutzung so schnell weg ist

Ein Chat kostet nicht pro Nachricht, sondern **pro gelesenem Zeichen — und zwar
jedes Mal aufs Neue**. Der gesamte bisherige Gesprächsverlauf wird bei jeder
weiteren Nachricht wieder mitgeschickt.

Das heißt: Öffnet Claude Code in einer Sitzung fünf große Dateien, zahlst du diese
fünf Dateien nicht einmal, sondern bei jeder folgenden Nachricht erneut. Eine lange
Sitzung wird pro Nachricht immer teurer. Das ist der eigentliche Grund, warum es
„auf einmal" aus ist.

Daraus folgt fast alles Weitere.

---

## Die fünf wirksamsten Hebel

### 1. Nach jeder abgeschlossenen Aufgabe `/clear`

Der mit Abstand größte Hebel, und der, den kaum jemand nutzt.

`/clear` wirft den bisherigen Verlauf weg und startet frisch. Eine Sitzung, in der
du drei zusammenhanglose Dinge erledigst, kostet ein Vielfaches von drei kurzen
Sitzungen mit demselben Inhalt.

**Faustregel:** Sobald etwas funktioniert und du zum nächsten Thema wechselst →
`/clear`. Nicht später.

### 2. Sag, welche Datei gemeint ist

```
❌  "Die Prognose stimmt nicht"
       → Claude durchsucht das halbe Projekt, um zu finden, wo das steht

✅  "In app/analysis.py gewichtet marathon_prediction() Garmin mit 40 %.
     Mach 30 % draus."
       → Eine Datei, ein gezielter Blick
```

Das Projekt ist absichtlich so aufgebaut, dass sich das leicht sagen lässt: eine
Zuständigkeit pro Datei. Die Tabelle im [README](../README.md) sagt dir, welche.

### 3. Fürs Routinehafte das kleinere Modell

Mit `/model` umschalten. Texte anpassen, Farben ändern, einen Wert korrigieren,
einen Tippfehler beheben — dafür braucht es nicht das größte Modell. Heb dir
das für Dinge auf, bei denen wirklich nachgedacht werden muss.

### 4. Lass Claude die Tests laufen statt raten

```
Ändere X in app/analysis.py und lass danach .venv/bin/pytest -q laufen.
```

Die 44 Tests laufen in unter zwei Sekunden und sagen sofort, ob etwas kaputt ist.
Das ist erheblich billiger als die Runde „hat nicht geklappt" → „hier ist der
Fehler" → „neuer Versuch".

### 5. Ein Auftrag pro Sitzung

Fünf Wünsche in einer Nachricht sind verlockend, aber Claude arbeitet sie
nacheinander ab und schleppt dabei den Kontext von allen mit. Eine Sache, `/clear`,
nächste Sache.

---

## Und im Chat mit Claude (nicht Claude Code)?

Dafür ist der MCP-Zugang gebaut, und zwar gezielt sparsam. Jedes Werkzeug rechnet
die Auswertung **auf dem Server** und gibt fertigen Text zurück statt Rohdaten.

Konkret: `trainingsstand` liefert Renn-Countdown, Trainingsphase, Wochenumfang,
Belastungsverhältnis, Intensitätsverteilung, Marathonprognose, Zielabgleich,
Gesundheitswerte und alle Warnungen — in **rund 220 Tokens**.

Würde dasselbe als Rohdaten kommen (90 Tage Aktivitäten plus Tagesmetriken als
JSON), wären es leicht 30.000. Das ist der Unterschied zwischen „ich frag mal kurz"
und „das mach ich lieber nicht nochmal".

**Frag deshalb offen und lass Claude das passende Werkzeug wählen:**

```
✅  "Wie schaut mein Trainingsstand aus?"
✅  "Plan mir die kommende Woche."
✅  "War der lange Lauf am Sonntag gut?"

❌  "Zeig mir alle meine Läufe der letzten drei Monate"
       → das ist genau der Rohdaten-Dump, den die Werkzeuge vermeiden
```

---

## Was du zum Einrichten wirklich brauchst

Der ganze Aufbau sollte dich ein bis zwei kurze Sitzungen kosten. Vorgeschlagener
Ablauf, jeweils mit `/clear` dazwischen:

| Sitzung | Auftrag |
|---|---|
| 1 | „Ich hab einen Server bei Hetzner. Geh mit mir SETUP.md durch." |
| 2 | „Trag mein Rennen ein: Wien Marathon am 12.04., Ziel 3:45, ich lauf gerade 35 km die Woche an Di/Do/Sa/So." |
| 3 | nach ein paar Wochen: „Die Startseite soll auch X zeigen." |

Sitzung 2 kannst du dir sparen — das geht schneller direkt im Dashboard unter
**Setup**.

---

## Wenn es trotzdem knapp wird

- **Vieles braucht Claude gar nicht.** Rennen anlegen, Plan erzeugen, Läufe
  bewerten, Notizen schreiben: alles direkt im Dashboard, kostet keine Nutzung.
- **Die Grenze setzt sich regelmäßig zurück.** Wenn du mitten in etwas steckst,
  schreib den Stand kurz auf; nach dem Zurücksetzen bist du in zwei Sätzen wieder
  drin, statt die halbe Sitzung zu rekonstruieren.
- **Claude Code gibt es auch im Browser** unter claude.ai/code, mit demselben
  Zugang. Praktisch, wenn du gerade nicht am eigenen Rechner sitzt.
