import { activateQueueView, startNotifications } from './notifications.js';
import { showView } from './views.mjs';

const $ = selector => document.querySelector(selector);
const status = $('#partnership-status');
let modulePromise = null;

function setStatus(message, error = false) {
  status.textContent = message;
  status.classList.toggle('error', error);
}

function initializeLogoutCsrf() {
  const input = $('#logout-csrf');
  if (!input || input.value) return;
  const cookie = document.cookie.split(';').map(item => item.trim())
    .find(item => item.startsWith('hospes_csrf='));
  input.value = cookie ? decodeURIComponent(cookie.split('=', 2)[1]) : '';
}

async function loadPartnershipModule() {
  if (!modulePromise) {
    modulePromise = import('./partnership.js').catch(error => {
      modulePromise = null;
      throw error;
    });
  }
  return modulePromise;
}

async function activate(view) {
  if (view === 'queue') {
    await activateQueueView();
    return;
  }
  if (view === 'overview' && !modulePromise) {
    showView('overview');
    return;
  }
  try {
    const module = await loadPartnershipModule();
    await module.activatePartnershipShell(view);
  } catch (error) {
    setStatus(error.message, true);
  }
}

initializeLogoutCsrf();
startNotifications();
$('#show-select').addEventListener('change', event => {
  const url = new URL(window.location.href);
  url.searchParams.set('show', event.currentTarget.value);
  window.location.assign(url.toString());
});
document.querySelectorAll('.view-tab').forEach(button => button.addEventListener('click', () => {
  void activate(button.dataset.view);
}));
$('#partnership-select').addEventListener('change', async event => {
  try {
    const module = await loadPartnershipModule();
    await module.selectPartnershipShell(event.currentTarget.value);
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#btn-partnership-refresh').addEventListener('click', async () => {
  try {
    const module = await loadPartnershipModule();
    await module.reloadPartnershipShell();
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#btn-presentation').addEventListener('click', event => {
  const enabled = !document.body.classList.contains('presentation');
  document.body.classList.toggle('presentation', enabled);
  event.currentTarget.setAttribute('aria-pressed', String(enabled));
  event.currentTarget.textContent = enabled ? 'Exit presentation mode' : 'Presentation mode';
});

// Set the synthetic mode chip on initial load without waiting for the
// full partnership module. The body attribute is set server-side when
// --synthetic-demo is active.
if (document.body.dataset.runtimeKind === 'synthetic') {
  const badge = document.querySelector('#mode-badge');
  badge.textContent = 'Synthetic demo mode';
  badge.classList.add('demo');
}
