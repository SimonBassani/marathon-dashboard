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
    if (data.ok) setTimeout(() => location.reload(), 600);
    else setTimeout(() => { button.textContent = original; button.disabled = false; }, 2500);
  } catch {
    button.textContent = 'Fehler';
    setTimeout(() => { button.textContent = original; button.disabled = false; }, 2500);
  }
});
