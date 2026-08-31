import {
  loadOperatorContext,
  loadPartnershipCommandCenter,
  loadPartnerships,
} from './api.js';
import { showView } from './views.mjs';

const $ = selector => document.querySelector(selector);
const elements = {
  select: $('#partnership-select'),
  refresh: $('#btn-partnership-refresh'),
  status: $('#partnership-status'),
  heading: $('#partnership-heading'),
  purpose: $('#partnership-purpose'),
};

let registries = [];
let activePartnershipId = '';
let center = null;
let context = null;
let workspace = null;
let workspacePromise = null;
let workspaceMarkupPromise = null;

function escapeHTML(value) {
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}
function labeledContextValue(value, fallback = 'Unavailable') {
  const normalized = String(value ?? '').trim();
  return normalized ? normalized : fallback;
}

function eventTimelineStamp(event = {}) {
  const timestamp = new Date(String(event.created_at || ''));
  if (Number.isNaN(timestamp.getTime())) return '';
  return `${timestamp.toLocaleString()}`;
}

function eventTimelineKey(event = {}) {
  const actorId = String(event.actor_id || '');
  const actorRole = String(event.actor_role || '');
  const eventType = String(event.event_type || '');
  return [
    eventTimelineStamp(event),
    eventType,
    actorId,
    actorRole,
  ].join('|');
}

function setStatus(message, error = false) {
  elements.status.textContent = message;
  elements.status.classList.toggle('error', error);
}

function dedupeRecentEvents(events = []) {
  const buckets = new Map();
  const ordered = [];
  for (const event of events || []) {
    if (!event) continue;
    const key = eventTimelineKey(event) || event.id || event.event_id;
    const existing = buckets.get(key);
    if (!existing) {
      const bucket = { ...event, __count: 1 };
      buckets.set(key, bucket);
      ordered.push(bucket);
      continue;
    }
    existing.__count += 1;
  }
  return ordered;
}

function initializeLogoutCsrf() {
  const logoutCsrf = $('#logout-csrf');
  if (!logoutCsrf) return;
  const csrfCookie = document.cookie.split(';').map(item => item.trim())
    .find(item => item.startsWith('hospes_csrf='));
  logoutCsrf.value = csrfCookie ? decodeURIComponent(csrfCookie.split('=', 2)[1]) : '';
}

function renderContext() {
  if (!context) return;
  $('#context-actor').textContent = labeledContextValue(context.actor_id);
  $('#context-role').textContent = labeledContextValue(context.role?.replaceAll('_', ' '));
  $('#context-tenant').textContent = labeledContextValue(context.tenant_id);
  $('#context-database').textContent = labeledContextValue(context.scenario_label || context.database);
  $('#context-verified').textContent = context.last_verification
    ? labeledContextValue(new Date(context.last_verification).toLocaleString())
    : labeledContextValue('');
  if (context.runtime_kind === 'synthetic_demo') {
    const syntheticScenario = labeledContextValue(context.demo_scenario, 'synthetic demo');
    $('#mode-badge').textContent = `Synthetic ${syntheticScenario.replaceAll('_', ' ')} · synthetic data`;
    $('#mode-badge').classList.add('demo');
  }
  document.body.classList.toggle('read-only-specimen', context.demo_scenario === 'complete');
}

function compactItems(items) {
  if (!items?.length) return '<p class="empty">None.</p>';
  return `<ul>${items.map(item => `<li><b>${escapeHTML(item.title || item.label)}</b>${item.owner ? ` · ${escapeHTML(item.owner)}` : ''}</li>`).join('')}</ul>`;
}

function renderOverview() {
  if (!center) return;
  elements.heading.textContent = center.partnership.label;
  elements.purpose.textContent = center.partnership.purpose;
  for (const key of ['total', 'active', 'unknown', 'overdue']) {
    $(`#pc-${key}`).textContent = center.summary[key];
  }
  $('#pc-coverage').textContent = `${center.coverage.covered}/${center.coverage.total}`;
  const agenda = center.agenda;
  $('#agenda').innerHTML = [
    ['Decisions required', agenda.decisions_required],
    ['Overdue obligations', agenda.overdue_obligations],
    ['Blockers', agenda.blockers],
    ['Risks', agenda.risks],
    ['Unknowns', agenda.unknowns],
  ].map(([label, items]) => `<details ${items.length ? 'open' : ''}><summary>${escapeHTML(label)} · ${items.length}</summary>${compactItems(items)}</details>`).join('')
    + `<div class="next-gate"><span>Next recording gate</span><b>${escapeHTML(agenda.next_recording_gate?.label || 'All pilot gates satisfied')}</b></div>`;
  $('#capabilities').innerHTML = Object.entries(center.engine_capabilities)
    .map(([key, enabled]) => `<div class="check ${enabled ? 'met' : 'missing'}"><span aria-hidden="true">${enabled ? '✓' : '×'}</span><b>${escapeHTML(key.replaceAll('_', ' '))}</b></div>`)
    .join('');
  const recentEvents = dedupeRecentEvents(center.recent_events).slice(0, 12);
  $('#audit-timeline').innerHTML = recentEvents.map(event => {
    const countLabel = event.__count > 1 ? ` (${event.__count}x)` : '';
    return `<li><time>${escapeHTML(new Date(event.created_at).toLocaleString())}</time><b>${escapeHTML(event.event_type.replaceAll('.', ' · '))}${countLabel}</b><span class="sensitive-identity">${escapeHTML(event.actor_id)} · ${escapeHTML(event.actor_role)}</span></li>`;
  }).join('') || '<li class="empty">No events yet.</li>';
}

async function refreshOverview() {
  if (!activePartnershipId) return;
  elements.refresh.disabled = true;
  setStatus('Loading live partnership and authority state…');
  try {
    center = await loadPartnershipCommandCenter(activePartnershipId, 'overview');
    renderOverview();
    setStatus(`Loaded ${center.summary.total} current items and ${center.coverage.covered}/${center.coverage.total} register domains.`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    elements.refresh.disabled = false;
  }
}

async function loadOverviewRegistry() {
  setStatus('Loading operator context and partnership registry…');
  try {
    const previousPartnershipId = activePartnershipId;
    [context, registries] = await Promise.all([loadOperatorContext(), loadPartnerships()]);
    renderContext();
    elements.select.innerHTML = registries
      .map(partnership => `<option value="${escapeHTML(partnership.id)}">${escapeHTML(partnership.label)}</option>`)
      .join('');
    activePartnershipId = registries.some(partnership => partnership.id === previousPartnershipId)
      ? previousPartnershipId
      : registries[0]?.id || '';
    if (!activePartnershipId) {
      setStatus('No partnership exists in this tenant.', true);
      return;
    }
    elements.select.value = activePartnershipId;
    await refreshOverview();
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function loadWorkspaceMarkup() {
  if (!workspaceMarkupPromise) {
    workspaceMarkupPromise = fetch('./assets/partnership-workspace.html', {
      cache: 'no-store',
      credentials: 'same-origin',
    }).then(async response => {
      if (!response.ok) throw new Error(`Operator workspace returned HTTP ${response.status}.`);
      const template = document.createElement('template');
      template.innerHTML = await response.text();
      const placeholder = $('#deferred-workspace');
      if (!placeholder) throw new Error('Operator workspace placeholder is unavailable.');
      placeholder.replaceWith(template.content);
    }).catch(error => {
      workspaceMarkupPromise = null;
      throw error;
    });
  }
  await workspaceMarkupPromise;
}

async function ensureWorkspace(view) {
  if (!workspacePromise) {
    setStatus('Loading the private operator workspace…');
    workspacePromise = loadWorkspaceMarkup().then(() => import('./partnership-workspace.js')).then(async module => {
      showView(view);
      workspace = module;
      await workspace.startPartnershipWorkspace(view, activePartnershipId);
      return module;
    }).catch(error => {
      workspacePromise = null;
      setStatus(error.message, true);
      throw error;
    });
    await workspacePromise;
    return;
  }
  await workspacePromise;
  showView(view);
  await workspace.activatePartnershipWorkspace(view);
}

export async function activatePartnershipShell(view) {
  initializeLogoutCsrf();
  activePartnershipId = elements.select.value || activePartnershipId;
  if (view === 'overview') {
    if (workspace) {
      await ensureWorkspace(view);
    } else {
      showView(view);
      if (center) renderOverview();
      else await loadOverviewRegistry();
    }
  } else {
    await ensureWorkspace(view);
  }
}

export async function selectPartnershipShell(partnershipId) {
  activePartnershipId = partnershipId;
  elements.select.value = partnershipId;
  if (workspace) {
    await workspace.selectPartnershipWorkspace(partnershipId);
  } else {
    showView('overview');
    await refreshOverview();
  }
}

export async function reloadPartnershipShell() {
  if (workspace) {
    await workspace.reloadPartnershipWorkspace();
  } else {
    await loadOverviewRegistry();
  }
}
