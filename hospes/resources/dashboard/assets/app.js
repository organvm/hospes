import {
  annotateResearch,
  loadApprovalQueue,
  loadOperatorContext,
  loadPortalStatus,
  lockResearch,
  recordDecision,
  reviewResearch,
  runResearch,
  sendPortalLink,
  setDoNotContact,
  startResearch,
} from './api.js';
import { capabilitiesFor } from './capabilities.mjs';
import { publishDecisionCommitted } from './decisions.mjs';
import {
  applyDemoDecision,
  csvToCandidates,
  exportDemoDecisions,
  isProtectedCandidate,
} from './demo.js';

const elements = {
  cards: document.querySelector('#cards'),
  status: document.querySelector('#status'),
  modeBadge: document.querySelector('#mode-badge'),
  authorityNotice: document.querySelector('#authority-notice'),
  demoPanel: document.querySelector('#demo-panel'),
  fileInput: document.querySelector('#file-input'),
  demoButton: document.querySelector('#btn-demo'),
  liveButton: document.querySelector('#btn-live'),
  exportButton: document.querySelector('#btn-export'),
  refreshButton: document.querySelector('#btn-refresh'),
  applyFiltersButton: document.querySelector('#btn-apply-filters'),
  stateFilter: document.querySelector('#state-filter'),
  ownerFilter: document.querySelector('#owner-filter'),
  loadDemoButton: document.querySelector('#btn-load-demo'),
  researchConsole: document.querySelector('#research-console'),
  researchProvider: document.querySelector('#research-provider'),
  researchStatus: document.querySelector('#research-status'),
  researchBrief: document.querySelector('#research-brief'),
  researchControls: document.querySelector('#research-controls'),
  researchCounterargument: document.querySelector('#research-counterargument'),
  researchAnnotate: document.querySelector('#btn-research-annotate'),
  researchApprove: document.querySelector('#btn-research-approve'),
  researchLock: document.querySelector('#btn-research-lock'),
};

let mode = 'live';
let candidates = [];
let decisionFilter = 'all';
let operatorContext = null;
let capabilities = capabilitiesFor();
const demoDecisions = new Map();
let researchJob = null;

async function loadDemoData() {
  if (!elements.loadDemoButton) return;
  elements.loadDemoButton.disabled = true;
  setStatus('Loading demo pipeline…');
  try {
    const resp = await fetch('/operator/demo-pipeline.csv', {
      credentials: 'same-origin',
    });
    if (!resp.ok) throw new Error(`Failed to load demo CSV: ${resp.status}`);
    const text = await resp.text();
    candidates = csvToCandidates(text).map(normalizeCandidate);
    demoDecisions.clear();
    render();
    setStatus(`Loaded ${candidates.length} demo candidate${candidates.length === 1 ? '' : 's'} in memory.`);
  } catch (error) {
    candidates = [];
    render();
    setStatus(error.message, true);
  } finally {
    elements.loadDemoButton.disabled = false;
  }
}

function escapeHTML(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function first(value, fallback = '') {
  return value === null || value === undefined || value === '' ? fallback : value;
}

function normalizeCandidate(raw, index) {
  const source = raw.opportunity || raw;
  const routes = source.contact_routes || raw.contact_routes || [];
  const flatRoute = source.route_type ? {
    route_type: source.route_type,
    route_label: source.route_label,
    source_provenance: source.route_provenance,
    verified_at: source.route_verified_at,
    usable: source.route_usable !== 0,
  } : {};
  const route = raw.contact_route || source.contact_route || routes.at(-1) || flatRoute;
  const contactRoster = source.contact_roster || raw.contact_roster || [];
  const contactRosterRefs = source.contact_roster_refs || raw.contact_roster_refs || [];
  const studios = source.studio_routes || raw.studio_routes || [];
  const studio = raw.studio_route || studios.at(-1) || {};
  return {
    ...source,
    id: first(source.id, first(raw.opportunity_id, `queue-${index + 1}`)),
    guest_name: first(source.guest_name, 'Unnamed candidate'),
    episode_thesis: first(source.episode_thesis, raw.thesis),
    proposed_artifact: first(source.proposed_artifact, raw.artifact),
    relationship_owner: first(
      raw.relationship_owner,
      first(source.relationship_owner, first(raw.owner, source.owner)),
    ),
    social_cost: first(raw.social_cost, first(source.social_cost, source.social_cost_1_5)),
    ari_effort: first(raw.ari_effort, source.ari_effort),
    preferred_city: first(
      raw.city,
      first(source.preferred_city, first(studio.city, source.city)),
    ),
    next_action: first(raw.next_action, source.next_action),
    contact_route: route,
    contact_roster: contactRoster,
    contact_roster_refs: contactRosterRefs,
    disposition: first(raw.disposition, source.disposition),
    status: first(raw.state, source.status),
    guest_id: first(source.guest_id, first(raw.guest_id, source.source_key)),
    guest_history: source.guest_history || raw.guest_history || [],
    guest_history_badge: first(source.guest_history_badge, raw.guest_history_badge, null),
    do_not_contact: source.do_not_contact === true || raw.do_not_contact === true,
  };
}

// The badge is season memory, not contact data: it renders from the queue
// projection alone, so a card shows "Previously: S2E3 — SOFT_DECLINE" without
// a custody key and without revealing a single private note.
function memoryPills(candidate) {
  const pills = [];
  if (candidate.guest_history_badge) {
    pills.push(`<span class="pill guest-memory" data-guest-memory>${escapeHTML(candidate.guest_history_badge)}</span>`);
  }
  if (candidate.do_not_contact) {
    pills.push('<span class="pill do-not-contact" data-do-not-contact>Do not contact</span>');
  }
  return pills.join('');
}

const PORTAL_LABELS = {
  sent: ['Portal link sent', ''],
  opened: ['Guest opened portal', ''],
  completed: ['Guest completed portal', 'low'],
  expired: ['Portal link expired', 'stale'],
};

let portalByGuest = new Map();

function portalFor(candidate) {
  const key = String(candidate.source_key || '').trim().toLowerCase();
  return key ? portalByGuest.get(key) : undefined;
}

function portalPill(candidate) {
  const status = portalFor(candidate);
  const entry = status && PORTAL_LABELS[status.state];
  if (!entry) return '';
  const [label, tone] = entry;
  return `<span class="pill portal-pill ${tone}">${escapeHTML(label)}</span>`;
}

function contactPills(candidate) {
  if (!capabilities.canViewContacts) return '';
  const privateItems = candidate.contact_roster || [];
  const publicItems = candidate.contact_roster_refs || [];
  const items = privateItems.length ? privateItems : publicItems;
  return items.map(item => {
    const label = item.name
      ? `${item.role}: ${item.name}`
      : `${item.role}: route on file`;
    const state = item.usable && item.verified_at
      ? 'verified'
      : String(item.permission_status || 'pending verification').replaceAll('_', ' ');
    return `<span class="pill contact-route-pill sensitive-ref">${escapeHTML(label)} · ${escapeHTML(state)}</span>`;
  }).join('');
}

function candidateDecision(candidate) {
  if (mode === 'demo') return demoDecisions.get(candidate.id)?.decision || 'pending';
  const disposition = String(candidate.disposition || '').toUpperCase();
  if (disposition === 'APPROVED') return 'approve';
  if (disposition === 'REJECTED') return 'reject';
  if (disposition === 'PROTECTED') return 'protect';
  return 'pending';
}

function decisionLabel(decision) {
  return {
    approve: 'Approved',
    reject: 'Rejected',
    protect: 'Protected',
    pending: 'Pending',
  }[decision] || 'Pending';
}

function routeSummary(route) {
  if (!route || !Object.keys(route).length) return 'Missing';
  const suggestedLabels = {
    ari_direct: 'Ari direct',
    anthony_warm_intro: 'Anthony warm intro',
    producer_cold: 'Producer cold',
  };
  const label = route.usable === false && suggestedLabels[route.route_type]
    ? suggestedLabels[route.route_type]
    : first(route.route_label, first(route.label, route.route_type));
  const verified = route.usable !== false && Boolean(route.verified_at || route.source_provenance);
  return `${label || 'Route on file'} · ${verified ? 'verified' : 'unverified'}`;
}

function renderCard(candidate) {
  const decision = candidateDecision(candidate);
  const relationshipClass = first(candidate.relationship_class);
  const note = mode === 'demo' ? demoDecisions.get(candidate.id)?.note || '' : '';
  const fields = [
    ['Thesis', first(candidate.episode_thesis, 'Not yet locked')],
    ['Artifact', first(candidate.proposed_artifact, 'Not yet declared')],
    ['Relationship owner', first(candidate.relationship_owner, 'Not assigned')],
    ['Verified route', routeSummary(candidate.contact_route)],
    ['Provenance', first(candidate.source_provenance, 'Not recorded')],
    ['Next action', first(candidate.next_action, first(candidate.status, 'Review'))],
  ];
  const fieldRows = fields.map(([label, value]) => (
    `<dt>${escapeHTML(label)}</dt><dd class="${label === 'Verified route' ? 'sensitive-ref' : ''} ${label === 'Relationship owner' ? 'sensitive-identity' : ''}">${escapeHTML(value)}</dd>`
  )).join('');
  const protectedClass = isProtectedCandidate(candidate);
  const demoProtectionActive = mode === 'demo' && (
    protectedClass || decision === 'protect'
  );
  const readOnly = capabilities.completed;
  const canDecide = mode === 'demo' || capabilities.canDecideCandidates;
  const canProtect = mode === 'demo' || capabilities.canProtect;
  const canResearch = mode === 'live' && capabilities.canResearch;
  const canSendPortal = mode === 'live'
    && capabilities.canSendPortalLink
    && String(candidate.status || '').toUpperCase() === 'BOOKED';
  return `
    <article class="card" data-decision="${decision}" data-id="${escapeHTML(candidate.id)}">
      <div class="card-badge">${decisionLabel(decision)}</div>
      <div class="card-eyebrow">${escapeHTML(first(candidate.status, 'Approval review'))}</div>
      <h2>${escapeHTML(candidate.guest_name)}</h2>
      <div class="card-city">${escapeHTML(first(candidate.preferred_city, 'City not set'))}</div>
      <div class="pills">
        ${relationshipClass ? `<span class="pill ${protectedClass ? 'protected' : ''}">${escapeHTML(relationshipClass)}</span>` : ''}
        ${candidate.social_cost ? `<span class="pill ${Number(candidate.social_cost) <= 2 ? 'low' : ''}">Social cost: ${escapeHTML(candidate.social_cost)}/5</span>` : ''}
        ${candidate.ari_effort ? `<span class="pill">Ari effort: ${escapeHTML(candidate.ari_effort)}</span>` : ''}
        ${memoryPills(candidate)}
        ${portalPill(candidate)}
        ${contactPills(candidate)}
      </div>
      <dl>${fieldRows}</dl>
      <textarea class="note-area" maxlength="4000" placeholder="Decision note or relationship context">${escapeHTML(note)}</textarea>
      <div class="actions">
        ${canDecide && !demoProtectionActive ? '<button class="act-approve" data-action="approve" type="button">Approve</button><button class="act-reject" data-action="reject" type="button">Reject</button>' : ''}
        ${canProtect ? '<button class="act-protect" data-action="protect" type="button">Protect</button>' : ''}
        ${mode !== 'demo' && capabilities.canSetDoNotContact ? `<button class="danger-button act-dnc" data-action="${candidate.do_not_contact ? 'allow-contact' : 'do-not-contact'}" type="button">${candidate.do_not_contact ? 'Allow contact' : 'Do not contact'}</button>` : ''}
        ${canResearch ? '<button class="act-research" data-action="research" type="button">Research</button>' : ''}
        ${canSendPortal ? '<button class="act-portal" data-action="portal-link" type="button">Send portal link</button>' : ''}
        ${readOnly ? '' : '<button data-action="note" type="button">Save note</button>'}
      </div>
    </article>`;
}

function setStatus(message, error = false) {
  elements.status.textContent = message;
  elements.status.classList.toggle('error', error);
}

function updateCounters() {
  const decisions = candidates.map(candidateDecision);
  document.querySelector('#cnt-total').textContent = candidates.length;
  document.querySelector('#cnt-pending').textContent = decisions.filter(value => value === 'pending').length;
  document.querySelector('#cnt-approve').textContent = decisions.filter(value => value === 'approve').length;
  document.querySelector('#cnt-reject').textContent = decisions.filter(value => value === 'reject').length;
  document.querySelector('#cnt-protect').textContent = decisions.filter(value => value === 'protect').length;
}

function render() {
  const visible = decisionFilter === 'all'
    ? candidates
    : candidates.filter(candidate => candidateDecision(candidate) === decisionFilter);
  if (!candidates.length) {
    const empty = mode === 'live'
      ? 'The authenticated approval queue is empty.'
      : 'Choose a CSV to load the in-memory demo.';
    elements.cards.innerHTML = `<p class="state-msg">${empty}</p>`;
  } else if (!visible.length) {
    elements.cards.innerHTML = '<p class="state-msg">No candidates match this decision filter.</p>';
  } else {
    elements.cards.innerHTML = visible.map(renderCard).join('');
  }
  updateCounters();
}

function filters() {
  return {
    state: elements.stateFilter.value.trim(),
    owner: elements.ownerFilter.value.trim(),
  };
}

async function refreshLive() {
  if (mode !== 'live') return;
  elements.refreshButton.disabled = true;
  setStatus('Loading authenticated approval queue…');
  try {
    const [items, liveContext, portal] = await Promise.all([
      loadApprovalQueue(filters()),
      operatorContext ? Promise.resolve(operatorContext) : loadOperatorContext(),
      loadPortalStatus().catch(() => []),
    ]);
    portalByGuest = new Map((portal || []).map(entry => [String(entry.guest_id || ''), entry]));
    operatorContext = liveContext;
    capabilities = capabilitiesFor(operatorContext);
    if (operatorContext.runtime_kind === 'synthetic_demo') {
      const syntheticScenario = operatorContext.demo_scenario?.replaceAll('_', ' ') || 'synthetic demo';
      elements.modeBadge.textContent = `Synthetic ${syntheticScenario} · synthetic data`;
      elements.modeBadge.classList.add('demo');
      elements.authorityNotice.textContent = operatorContext.demo_scenario === 'complete'
        ? 'Completed synthetic specimen: all writes are blocked in the synthetic dataset.'
        : 'Synthetic decisions persist only in the isolated demo database and move no real Pilot gate.';
    }
    if (capabilities.completed) {
      elements.demoButton.classList.add('hidden');
      elements.exportButton.classList.add('hidden');
      elements.demoPanel.classList.add('hidden');
    }
    candidates = items.map(normalizeCandidate);
    render();
    setStatus(`Loaded ${candidates.length} live candidate${candidates.length === 1 ? '' : 's'} from SQLite.`);
  } catch (error) {
    candidates = [];
    render();
    setStatus(error.message, true);
  } finally {
    elements.refreshButton.disabled = false;
  }
}

function setMode(nextMode) {
  if (nextMode === 'demo' && capabilities.completed) {
    setStatus('Completed specimens expose no write or export surfaces.', true);
    return;
  }
  mode = nextMode;
  const demo = mode === 'demo';
  elements.modeBadge.textContent = demo ? 'Synthetic demo CSV mode' : 'Live SQLite mode';
  elements.modeBadge.classList.toggle('demo', demo);
  elements.authorityNotice.classList.toggle('hidden', demo);
  elements.demoPanel.classList.toggle('hidden', !demo);
  elements.demoButton.classList.toggle('hidden', demo);
  elements.liveButton.classList.toggle('hidden', !demo || window.location.protocol === 'file:');
  elements.exportButton.classList.toggle('hidden', !demo);
  elements.refreshButton.classList.toggle('hidden', demo);
  elements.applyFiltersButton.classList.toggle('hidden', demo);
  elements.stateFilter.closest('label').classList.toggle('hidden', demo);
  elements.ownerFilter.closest('label').classList.toggle('hidden', demo);
  candidates = [];
  setStatus(demo
    ? 'Demo mode is in-memory only and records in synthetic-only data.'
    : 'Live API mode restored.');
  render();
  if (!demo) refreshLive();
}


// --- Bounded research console ---------------------------------------------
// The card starts the job; the console shows its status and the cited brief.
// Approval and lock stay two separate human clicks: nothing here auto-commits.

function activeShowId() {
  return document.querySelector('meta[name="hospes-active-show"]')?.content || '';
}

function researchGuestId(candidate) {
  return String(candidate?.source_key || candidate?.guest_name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
}

function setResearchStatus(message, error = false) {
  if (!elements.researchStatus) return;
  elements.researchStatus.textContent = message;
  elements.researchStatus.classList.toggle('error', error);
}

function briefSections(job) {
  const claims = job.brief?.candidate_claims || [];
  return [
    ['Verified claims', claims.map(claim => `${claim.claim} · ${claim.verified ? 'verified' : 'unverified'} · ${claim.source}`)],
    ['Citations', (job.citations || []).map(item => `${item.title} — ${item.url} · ${item.provider}`)],
    ['Counterarguments', job.counterarguments || []],
    ['Evidence receipts', (job.evidence || []).map(item => `${item.kind} · ${item.source} · ${item.retrieved_at}`)],
    ['Segment candidates', (job.segment_candidates || []).map(item => `${item.title} — ${item.objective}`)],
    ['Risk flags', (job.risk_flags || []).map(item => `${item.kind} · ${item.source || 'no parseable source'}`)],
  ];
}

function renderResearchBrief(job) {
  researchJob = job;
  if (!elements.researchBrief) return;
  if (!job) {
    elements.researchBrief.innerHTML = '<p class="empty">No research brief is ready for review.</p>';
    elements.researchControls?.classList.add('hidden');
    return;
  }
  const claims = job.brief?.candidate_claims || [];
  const claimBoxes = claims.map((claim, index) => (
    `<label class="research-claim"><input type="checkbox" class="research-claim-check" value="${index}"${claim.verified ? ' checked disabled' : ''}> ${escapeHTML(claim.claim)}</label>`
  )).join('');
  elements.researchBrief.innerHTML = [
    `<div class="research-head"><b>${escapeHTML(job.provider)}</b> · ${escapeHTML(job.status)} · cost ${escapeHTML(job.cost_minor)}/${escapeHTML(job.max_cost_minor)}</div>`,
    ...briefSections(job).map(([label, items]) => (
      `<details ${items.length ? 'open' : ''}><summary>${escapeHTML(label)} · ${items.length}</summary>${
        items.length
          ? `<ul>${items.map(item => `<li>${escapeHTML(item)}</li>`).join('')}</ul>`
          : '<p class="empty">None.</p>'
      }</details>`
    )),
    claimBoxes ? `<div class="research-verify">${claimBoxes}</div>` : '',
  ].join('');
  const reviewable = job.status === 'ready_for_review' || job.status === 'reviewed';
  elements.researchControls?.classList.toggle('hidden', !(reviewable && capabilities.canResearch));
  if (elements.researchAnnotate) elements.researchAnnotate.disabled = job.status !== 'ready_for_review';
  if (elements.researchApprove) elements.researchApprove.disabled = job.status !== 'ready_for_review';
  if (elements.researchLock) elements.researchLock.disabled = job.status !== 'reviewed';
}

async function requestResearch(candidate) {
  const showId = activeShowId();
  const guestId = researchGuestId(candidate);
  if (!showId || !guestId) {
    setResearchStatus('A research job needs an active show and an identifiable guest.', true);
    return;
  }
  setResearchStatus('Researching…');
  renderResearchBrief(null);
  try {
    const started = await startResearch(showId, {
      guest_id: guestId,
      provider: elements.researchProvider?.value || 'brave',
      query: `${candidate.guest_name} verified public claims`,
    });
    const job = started.status === 'queued' ? await runResearch(showId, started.id) : started;
    renderResearchBrief(job);
    setResearchStatus(job.status === 'ready_for_review'
      ? `Ready for review · ${(job.citations || []).length} allowlisted citations · ${job.cost_minor} minor units spent.`
      : `Research job ${job.status}${job.failure_reason ? `: ${job.failure_reason}` : '.'}`,
      job.status === 'failed');
  } catch (error) {
    setResearchStatus(error.message, true);
  }
}

async function researchStep(step) {
  const showId = activeShowId();
  if (!researchJob || !showId) {
    setResearchStatus('Start a research job before reviewing one.', true);
    return;
  }
  const jobId = researchJob.id;
  try {
    let updated = researchJob;
    if (step === 'annotate') {
      const indexes = [...document.querySelectorAll('.research-claim-check:checked:not(:disabled)')]
        .map(node => Number(node.value));
      const counterargument = (elements.researchCounterargument?.value || '').trim();
      if (!indexes.length && !counterargument) {
        setResearchStatus('Verify at least one claim or record a counterargument.', true);
        return;
      }
      updated = await annotateResearch(showId, jobId, {
        verified_claim_indexes: indexes,
        counterarguments: counterargument ? [counterargument] : [],
      });
      if (elements.researchCounterargument) elements.researchCounterargument.value = '';
    } else if (step === 'approve') {
      updated = await reviewResearch(showId, jobId, {
        review_ref: `receipt://research-review/${jobId}`,
        approved: true,
      });
    } else if (step === 'lock') {
      updated = await lockResearch(showId, jobId, {
        lock_ref: `receipt://research-lock/${jobId}`,
      });
    }
    renderResearchBrief(updated);
    setResearchStatus(`Research job ${updated.status}.`);
  } catch (error) {
    setResearchStatus(error.message, true);
  }
}

elements.researchAnnotate?.addEventListener('click', () => researchStep('annotate'));
elements.researchApprove?.addEventListener('click', () => researchStep('approve'));
elements.researchLock?.addEventListener('click', () => researchStep('lock'));

elements.cards.addEventListener('click', async event => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const card = button.closest('.card');
  const opportunityId = card.dataset.id;
  const action = button.dataset.action;
  const note = card.querySelector('.note-area').value.trim();
  if (action === 'note' && !note) {
    setStatus('A note action requires note text.', true);
    return;
  }
  if (action === 'research') {
    card.querySelectorAll('button').forEach(item => { item.disabled = true; });
    try {
      await requestResearch(candidates.find(item => item.id === opportunityId));
    } finally {
      card.querySelectorAll('button').forEach(item => { item.disabled = false; });
    }
    return;
  }
  if (mode === 'demo') {
    const candidate = candidates.find(item => item.id === opportunityId);
    const { saved, refused, reason } = applyDemoDecision(
      candidate,
      action,
      note,
      demoDecisions.get(opportunityId),
    );
    demoDecisions.set(opportunityId, saved);
    render();
    setStatus(reason === 'invalid-action'
      ? 'Unsupported demo action was refused; no decision was changed.'
      : (refused
        ? 'Protected synthetic candidate: approval or rejection was refused; no downstream work was created.'
        : 'Demo decision saved in memory only; export is required to carry it elsewhere.'));
    return;
  }
  card.querySelectorAll('button').forEach(item => { item.disabled = true; });
  const directive = action === 'do-not-contact' || action === 'allow-contact';
  if (action === 'portal-link') {
    setStatus('Minting a one-time guest portal link…');
    try {
      const link = await sendPortalLink(opportunityId);
      // Shown exactly once. HOSPES has no send capability: a human carries the
      // link to the guest through a channel they already have.
      const field = document.createElement('input');
      field.className = 'portal-link';
      field.readOnly = true;
      field.value = link.url;
      field.setAttribute('aria-label', 'One-time guest portal link — copy and send it yourself');
      card.querySelector('.actions').after(field);
      field.select();
      setStatus(`Portal link ready — copy it and send it to the guest yourself. It expires ${link.expires_at}.`);
    } catch (error) {
      setStatus(error.message, true);
    }
    card.querySelectorAll('button').forEach(item => { item.disabled = false; });
    return;
  }
  setStatus(directive
    ? 'Updating the guest do-not-contact directive through the authenticated API…'
    : `Recording ${action} through the authenticated API…`);
  try {
    if (directive) {
      const reasonRef = /^[a-z][a-z0-9_-]{1,31}:\/\//.test(note) ? note : '';
      await setDoNotContact(opportunityId, action === 'do-not-contact', reasonRef);
    } else {
      await recordDecision(opportunityId, action, note);
    }
    await refreshLive();
    if (directive) {
      setStatus(action === 'do-not-contact'
        ? 'Do-not-contact recorded. Imports, drafts, and outreach receipts are now refused for this guest.'
        : 'Do-not-contact lifted. The guest returns to the ordinary approval path.');
    }
    // The queue has now reconciled itself, but it is not the only surface that
    // projects this candidate: the workbench selector, draft console, and
    // contact roster read the same lifecycle state. Announce the commit so
    // they re-read it too, instead of leaving a stale list that reads as a
    // missing feature (organvm/hospes#59).
    publishDecisionCommitted(document, opportunityId, action);
  } catch (error) {
    setStatus(error.message, true);
    card.querySelectorAll('button').forEach(item => { item.disabled = false; });
  }
});

document.querySelector('.filter-bar').addEventListener('click', event => {
  const button = event.target.closest('[data-filter]');
  if (!button) return;
  decisionFilter = button.dataset.filter;
  document.querySelectorAll('.filter-btn').forEach(item => item.classList.remove('active'));
  button.classList.add('active');
  render();
});

elements.fileInput.addEventListener('change', event => {
  const [file] = event.target.files;
  if (!file) return;
  const reader = new FileReader();
  reader.addEventListener('load', () => {
    candidates = csvToCandidates(String(reader.result)).map(normalizeCandidate);
    demoDecisions.clear();
    render();
    setStatus(candidates.length
      ? `Loaded ${candidates.length} demo candidate${candidates.length === 1 ? '' : 's'} in memory.`
      : 'No valid guest_name rows were found in that CSV.',
      !candidates.length);
  });
  reader.readAsText(file);
});

elements.demoButton.addEventListener('click', () => setMode('demo'));
elements.liveButton.addEventListener('click', () => setMode('live'));
elements.refreshButton.addEventListener('click', refreshLive);
elements.applyFiltersButton.addEventListener('click', refreshLive);
elements.exportButton.addEventListener('click', () => {
  exportDemoDecisions(candidates, demoDecisions);
  setStatus('Exported demo decisions. The file is not live HOSPES state until deliberately imported.');
});

elements.loadDemoButton?.addEventListener('click', loadDemoData);

export async function hydrateApprovalQueue() {
  if (window.location.protocol === 'file:') {
    setMode('demo');
    return;
  }
  await refreshLive();
}

if (window.location.protocol === 'file:') setMode('demo');
