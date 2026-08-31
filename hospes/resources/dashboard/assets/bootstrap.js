const fragment = new URLSearchParams(window.location.hash.slice(1));
const nonce = fragment.get('bootstrap');

// Set the synthetic mode chip on the login page if the body marker is present.
if (document.body.dataset.runtimeKind === 'synthetic') {
  const badge = document.querySelector('#mode-badge');
  if (badge) {
    badge.textContent = 'Synthetic demo mode';
    badge.classList.add('demo');
  }
}

if (nonce) {
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  const status = document.querySelector('#bootstrap-status');
  if (status) status.textContent = 'Opening the one-time synthetic session…';
  try {
    const response = await fetch('/operator/bootstrap', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nonce }),
    });
    if (!response.ok) throw new Error('The one-time synthetic link expired or was already used.');
    window.location.replace('/operator/');
  } catch (error) {
    if (status) status.textContent = error.message;
  }
}
