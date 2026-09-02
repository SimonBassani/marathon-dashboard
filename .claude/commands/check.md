---
description: Prüft Tests, Abhängigkeiten und Container-Zustand vor einem Deploy
---

Prüf das Projekt vor dem Ausrollen. Führ der Reihe nach aus und berichte knapp:

1. `.venv/bin/pytest -q` — alle Tests grün?
2. `.venv/bin/pip-audit -r requirements.txt` — bekannte Lücken in den Abhängigkeiten?
3. `git status --short` und `git diff --stat` — was ist geändert?

Wenn `pip-audit` etwas Kritisches oder Hohes in einer Laufzeit-Abhängigkeit meldet:
sag mir die betroffene Version, die behobene Version und was der Fehler praktisch
bedeutet. **Nicht** ungefragt aktualisieren.

Bei geänderten Dateien: sieh dir den Diff sicherheitsbezogen an, mit Blick auf
`app/oauth.py`, `app/security.py` und die `SecurityMiddleware` in `app/main.py`.

Am Ende ein klares Urteil: ausrollen oder nicht, und warum.
