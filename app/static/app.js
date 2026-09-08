// Der Aktualisieren-Knopf soll nicht die Seite neu laden und dabei den Scroll
// verlieren — also per fetch, und danach einmal neu rendern.
document.addEventListener('submit', async (event) => {
  const form = event.target;
  if (form.id !== 'syncform') return;
  event.preventDefault();
  const button = form.querySelector('button');
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Synct …';
  try {
    const res = await fetch('/sync', { method: 'POST' });
    const data = await res.json();
    button.textContent = data.ok ? 'Fertig' : 'Fehler';
    if (data.ok) {
      verbergeMeldung();
      setTimeout(() => location.reload(), 600);
    } else {
      // Der Grund steht im Server-Text. Ihn wegzuwerfen und nur "Fehler" zu
      // zeigen, lässt den Nutzer ratlos zurück — also anzeigen.
      zeigeMeldung(data.error);
      setTimeout(() => { button.textContent = original; button.disabled = false; }, 2500);
    }
  } catch {
    button.textContent = 'Fehler';
    zeigeMeldung('');   // Netzwerk weg oder Server antwortet nicht
    setTimeout(() => { button.textContent = original; button.disabled = false; }, 2500);
  }
});

// Übersetzt die technische Serverantwort in einen Satz, mit dem man etwas
// anfangen kann. Unbekanntes wird durchgereicht statt verschluckt.
function erklaere(fehler) {
  const roh = String(fehler || '').trim();
  if (!roh) {
    return ['Der Server war nicht erreichbar.',
            'Läuft er noch? Sonst neu starten und es erneut versuchen.'];
  }
  if (/GARMIN_EMAIL|GARMIN_PASSWORD/.test(roh)) {
    return ['Keine Garmin-Zugangsdaten hinterlegt.',
            'Das Dashboard läuft im Demo-Betrieb. Für echte Daten GARMIN_EMAIL ' +
            'und GARMIN_PASSWORD setzen — siehe SETUP.md.'];
  }
  if (/401|403|[Uu]nauthorized|[Ll]ogin|[Cc]redential/.test(roh)) {
    return ['Garmin hat die Anmeldung abgelehnt.',
            'Zugangsdaten prüfen. Bei aktiver Zwei-Faktor-Anmeldung schlägt der ' +
            'Sync derzeit fehl.'];
  }
  if (/[Tt]imeout|[Cc]onnection|[Nn]etwork|[Rr]esolve/.test(roh)) {
    return ['Garmin war nicht erreichbar.',
            'Meist vorübergehend — in ein paar Minuten nochmal probieren.'];
  }
  return ['Der Sync ist fehlgeschlagen.', roh];
}

function zeigeMeldung(fehler) {
  const box = document.getElementById('syncmeldung');
  if (!box) return;
  const [titel, text] = erklaere(fehler);
  // textContent statt innerHTML: der Servertext wird nie als Markup gedeutet.
  box.replaceChildren();
  const alert = document.createElement('div');
  alert.className = 'alert hoch';
  const t = document.createElement('strong');
  t.textContent = titel;
  const s = document.createElement('span');
  s.textContent = text;
  alert.append(t, s);
  box.append(alert);
  box.hidden = false;
  clearTimeout(zeigeMeldung.timer);
  zeigeMeldung.timer = setTimeout(verbergeMeldung, 12000);
}

function verbergeMeldung() {
  const box = document.getElementById('syncmeldung');
  if (!box) return;
  clearTimeout(zeigeMeldung.timer);
  box.hidden = true;
  box.replaceChildren();
}
