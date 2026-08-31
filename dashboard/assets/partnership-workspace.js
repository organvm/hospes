import {
  authorizeDistribution,
  decideClearance,
  loadClearanceBadges,
  loadClearanceReport,
  loadClearances,
  loadPublishBoard,
  markDistributionPublished,
  previewDistribution,
  queueClip,
  recordDistributionFailure,
  saveDistributionDraft,
  scheduleDistribution,
  verifyDistributionAdapters,
  approveSponsorClaim,
  exportRevenueCsv,
  loadAdSlots,
  loadOperatorContext,
  loadContactRoster,
  loadGuestHistory,
  saveGuestHistory,
  loadNetworkMap,
  loadOpportunityDetail,
  loadOpportunities,
  loadTouchpoints,
  loadPilotPlan,
  loadPartnerships,
  loadPartnershipCommandCenter,
  loadRevenue,
  loadSponsors,
  previewDraft,
  releaseSlotAssignment,
  saveAdSlots,
  saveAssets,
  saveBrief,
  saveCommitment,
  savePartnershipItem,
  savePartnershipReview,
  saveReceipt,
  saveClearance,
  saveResourceLink,
  saveReviewedDraft,
  saveSlotAssignment,
  saveSponsor,
  saveSponsorClaim,
  saveStudioRoute,
  saveTouchpoint,
  selectPilotCandidate,
  savePilotDecision,
  startPilotRun,
  supersedePartnershipItem,
  updatePartnershipItem,
} from './api.js';
import {
  loadAnalyticsExport,
  loadAnalyticsReceipts,
  loadAnalyticsTrends,
  loadNetworkPortfolio,
  loadNetworkReceipts,
  loadNetworkReport,
} from './api.js';
import { capabilitiesFor, plannerCapability } from './capabilities.mjs';
import { clipboardText } from './clipboard.mjs';
import { resolveSelectedOpportunity, subscribeDecisionCommitted } from './decisions.mjs';
import { plannerDecisionKinds, rankedDecisionKind, shouldRequestPilotPlan } from './planner.mjs';
import { plannerDecisionPayload, receiptPayload } from './payloads.mjs';
import { VIEWS, viewTitle } from './views.mjs';

const categories = ['engine', 'plan', 'role', 'agreement', 'deal', 'obligation', 'decision', 'receipt', 'risk', 'unknown'];
const writableStates = ['current', 'planned', 'agreed', 'in_progress', 'blocked', 'done', 'unknown'];
const readableStates = [...writableStates, 'superseded'];
const draftableStates = new Set(['APPROVED', 'CONTACT_ROUTE_IDENTIFIED', 'OUTREACH_DRAFTED', 'OUTREACH_APPROVED']);
const requiredAssetKinds = new Set(['environmental_master', 'host_singles', 'safety_microphone', 'backup_recorder', 'room_tone', 'slate']);
const labels = {
  engine: 'Engine and capabilities', plan: 'Plans and runway', role: 'Roles and authority',
  agreement: 'Agreements', deal: 'Deals and economics', obligation: 'Standing obligations',
  decision: 'Decisions', receipt: 'Receipts and custody', risk: 'Risks and boundaries',
  unknown: 'What might we be forgetting?',
};
const blueprint = {
  agreement: ['purpose and scope', 'contributions and decision rights', 'confidentiality, publicity, and consent', 'amendments, conflict handling, and exit'],
  deal: ['economics, expenses, and recoupment', 'sponsorship authority'],
  receipt: ['IP, brand, media, and data custody', 'canonical evidence ownership'],
  obligation: ['operating cadence'], risk: ['risk and compliance'],
};
const receiptFields = {
  'outreach.sent': [],
  'reply.classified': [{ name: 'classification', type: 'select', options: [] }],
  'booking.confirmed': [{ name: 'studio_ref' }, { name: 'producer_ref' }, { name: 'recording_time', type: 'datetime-local' }],
  'consent.signed': [{ name: 'private_pilot', type: 'checkbox', checked: true }, { name: 'clip_scope', type: 'select', options: ['none', 'approved_clips'] }],
  'recording.ready': [{ name: 'preflight_ref' }, { name: 'asset_package_id' }],
  'recording.completed': [{ name: 'session_kind', type: 'select', options: ['guest_pilot', 'technical_rehearsal'] }],
  'media.ingested': [{ name: 'master_ref' }, { name: 'checksum_ref' }],
};

const $ = selector => document.querySelector(selector);
const elements = {
  select: $('#partnership-select'), refresh: $('#btn-partnership-refresh'), status: $('#partnership-status'),
  heading: $('#partnership-heading'), purpose: $('#partnership-purpose'), sections: $('#partnership-sections'),
  itemForm: $('#partnership-item-form'), resourceForm: $('#resource-form'), reviewForm: $('#review-form'),
  opportunity: $('#workbench-opportunity'), receiptType: $('#receipt-type'), receiptDetails: $('#receipt-detail-fields'),
  networkForm: $('#network-map-form'),
};
const logoutCsrf = $('#logout-csrf');
if (logoutCsrf) {
  const csrfCookie = document.cookie.split(';').map(item => item.trim())
    .find(item => item.startsWith('hospes_csrf='));
  logoutCsrf.value = csrfCookie ? decodeURIComponent(csrfCookie.split('=', 2)[1]) : '';
}
let registries = [];
let activePartnershipId = '';
let center = null;
let context = null;
let opportunities = [];
let opportunityDetail = null;
let contactRoster = [];
let invitationRoutePrefill = null;
let touchpointTimeline = [];
let clearanceItems = [];
let clearanceBadges = [];
let clearanceStatusFilter = '';
let guestHistory = [];
let networkMap = null;
let networkRequestVersion = 0;
let pilotPlan = null;
let revenue = null;
let slotInventory = [];
let sponsorRoster = [];
let publishBoard = null;
let capabilities = capabilitiesFor();
let activeView = 'overview';
let workbenchQueueHydrated = false;
let requestedOpportunityId = '';
let analyticsTrends = null;
let analyticsReceipts = [];
let analyticsRequestVersion = 0;
let networkPortfolio = null;
let networkReceipts = [];
let networkPortfolioRequestVersion = 0;

function escapeHTML(value) {
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}
function labeledContextValue(value, fallback = 'Unavailable') {
  const normalized = String(value ?? '').trim();
  return normalized ? normalized : fallback;
}
function setStatus(message, error = false) { elements.status.textContent = message; elements.status.classList.toggle('error', error); }
function asIso(localValue) { return new Date(localValue).toISOString(); }
function localDateTimeValue(value = new Date()) {
  const shifted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

function eventTimelineStamp(event = {}, touchpointById) {
  const touchpoint = event.event_type === 'touchpoint.recorded'
    ? touchpointById.get(event.details?.touchpoint_id)
    : null;
  const timestampValue = touchpoint ? touchpoint.occurred_at : event.created_at;
  const timestamp = new Date(String(timestampValue || ''));
  if (Number.isNaN(timestamp.getTime())) return '';
  return `${timestamp.toLocaleString()}`;
}

function eventTimelineKey(event = {}, touchpointById = new Map()) {
  const actorId = String(event.actor_id || '');
  const actorRole = String(event.actor_role || '');
  const eventType = String(event.event_type || '');
  return [
    eventTimelineStamp(event, touchpointById),
    eventType,
    actorId,
    actorRole,
  ].join('|');
}
function can(...roles) { return context && roles.includes(context.role); }
function selectedOpportunity() { return opportunities.find(item => item.id === elements.opportunity.value); }
function reviewedDraftExists() { return opportunityDetail?.drafts?.some(draft => draft.status === 'REVIEWED') || false; }
function dedupeRecentEvents(events = [], touchpointById = new Map()) {
  const buckets = new Map();
  const ordered = [];
  for (const event of events || []) {
    if (!event) continue;
    const key = eventTimelineKey(event, touchpointById);
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

function isCompletePackage(assetPackage) {
  const kinds = new Set((assetPackage?.assets || []).map(asset => asset.kind));
  return [...requiredAssetKinds].every(kind => kinds.has(kind));
}
function primaryOpportunityId() {
  return center?.pilot_readiness?.candidate_slate?.find(candidate => Number(candidate.slot) === 1)?.opportunity_id || '';
}
function newestCompletePrimaryPackage() {
  if (!opportunityDetail || opportunityDetail.id !== primaryOpportunityId()) return null;
  return opportunityDetail.asset_packages?.slice().sort((a, b) => b.created_at.localeCompare(a.created_at)).find(isCompletePackage) || null;
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
  elements.itemForm.classList.toggle('hidden', !capabilities.canManageRegister);
  elements.resourceForm.classList.toggle('hidden', !capabilities.canManageRegister);
  $('#btn-log-touchpoint').classList.toggle('hidden', !capabilities.canLogTouchpoint);
  // The Network tab lives in the shell markup, which is rendered before any
  // role is known, so the role gate is applied here alongside every other
  // capability-driven control. The server refuses the routes regardless.
  document.querySelector('.view-tab[data-view="network"]')?.classList.toggle('hidden', !capabilities.canViewNetwork);
}

function compactItems(items) {
  if (!items?.length) return '<p class="empty">None.</p>';
  return `<ul>${items.map(item => `<li><b>${escapeHTML(item.title || item.label)}</b>${item.owner ? ` · ${escapeHTML(item.owner)}` : ''}</li>`).join('')}</ul>`;
}

function renderOverview() {
  if (!center) return;
  elements.heading.textContent = center.partnership.label;
  elements.purpose.textContent = center.partnership.purpose;
  for (const key of ['total', 'active', 'unknown', 'overdue']) $(`#pc-${key}`).textContent = center.summary[key];
  $('#pc-coverage').textContent = `${center.coverage.covered}/${center.coverage.total}`;
  const agenda = center.agenda;
  $('#agenda').innerHTML = [
    ['Decisions required', agenda.decisions_required], ['Overdue obligations', agenda.overdue_obligations],
    ['Blockers', agenda.blockers], ['Risks', agenda.risks], ['Unknowns', agenda.unknowns],
  ].map(([label, items]) => `<details ${items.length ? 'open' : ''}><summary>${escapeHTML(label)} · ${items.length}</summary>${compactItems(items)}</details>`).join('')
    + `<div class="next-gate"><span>Next recording gate</span><b>${escapeHTML(agenda.next_recording_gate?.label || 'All pilot gates satisfied')}</b></div>`;
  $('#capabilities').innerHTML = Object.entries(center.engine_capabilities).map(([key, enabled]) => `<div class="check ${enabled ? 'met' : 'missing'}"><span aria-hidden="true">${enabled ? '✓' : '×'}</span><b>${escapeHTML(key.replaceAll('_', ' '))}</b></div>`).join('');
  const touchpointById = new Map(touchpointTimeline.map(item => [item.touchpoint_id, item]));
  const recentEvents = dedupeRecentEvents(center.recent_events, touchpointById).slice(0, 12);
  $('#audit-timeline').innerHTML = recentEvents.map(event => {
    const countLabel = event.__count > 1 ? ` (${event.__count}x)` : '';
    const touchpoint = event.event_type === 'touchpoint.recorded'
      ? touchpointById.get(event.details?.touchpoint_id)
      : null;
    if (touchpoint) {
      const guest = touchpoint.guest_name || touchpoint.guest_id;
      return `<li><time>${escapeHTML(new Date(touchpoint.occurred_at).toLocaleString())}</time><b>${escapeHTML(touchpoint.initiator)} → ${escapeHTML(guest)} · ${escapeHTML(touchpoint.channel)}${countLabel}</b><span>${escapeHTML(touchpoint.notes || 'Encrypted note unavailable')}</span><span class="sensitive-identity">${escapeHTML(touchpoint.initiator_role)}</span></li>`;
    }
    return `<li><time>${escapeHTML(new Date(event.created_at).toLocaleString())}</time><b>${escapeHTML(event.event_type.replaceAll('.', ' · '))}${countLabel}</b><span class="sensitive-identity">${escapeHTML(event.actor_id)} · ${escapeHTML(event.actor_role)}</span></li>`;
  }).join('') || '<li class="empty">No events yet.</li>';
}

function renderReadiness() {
  if (!center) return;
  const readiness = center.pilot_readiness;
  $('#readiness-score').textContent = `${readiness.met}/${readiness.total}`;
  $('#readiness-label').textContent = readiness.pilot_complete ? 'Pilot 1 predicates satisfied' : `Next: ${readiness.next_gate?.label || 'none'}`;
  $('#readiness-matrix').innerHTML = readiness.checks.map(check => `<div class="check ${check.met ? 'met' : 'missing'}"><span aria-hidden="true">${check.met ? '✓' : '○'}</span><b>${escapeHTML(check.label)}</b></div>`).join('');
  const bySlot = new Map(readiness.candidate_slate.map(candidate => [Number(candidate.slot), candidate]));
  $('#candidate-slate').innerHTML = [1, 2, 3].map(slot => {
    const candidate = bySlot.get(slot);
    return `<article class="slate-card"><div class="eyebrow">${slot === 1 ? 'Primary' : `Backup ${slot - 1}`}</div>${candidate ? `<h4>${escapeHTML(candidate.guest_name)}</h4><p>${escapeHTML(candidate.episode_thesis || 'Thesis missing')}</p><span>${escapeHTML(candidate.status)}</span>` : '<h4>Unfilled</h4><p>Select an approved candidate.</p>'}</article>`;
  }).join('');
}

function plannerItems(values, fallback = 'None.') {
  if (!values?.length) return `<span>${escapeHTML(fallback)}</span>`;
  return values.map(value => `<span>${escapeHTML(typeof value === 'string' ? value : JSON.stringify(value))}</span>`).join('');
}

function plannerRoleAllows(kind) {
  return plannerCapability(kind, pilotPlan, capabilities);
}

function renderPilotPlanner() {
  const execution = center?.pilot_execution || {};
  const policy = execution.current_policy;
  const run = execution.latest_run;
  $('#planner-policy').textContent = policy ? `${policy.policy_key} · v${policy.policy_version}` : 'No policy';
  $('#planner-deadline').textContent = policy ? new Date(policy.deadline_at).toLocaleString() : '—';
  $('#planner-run-state').textContent = run ? run.lifecycle_state.toLowerCase().replaceAll('_', ' ') : 'not started';
  $('#planner-revision').textContent = pilotPlan?.revision ?? run?.revision ?? '—';
  $('#planner-risk').textContent = pilotPlan?.risk_level || '—';
  const firstFourMet = center?.pilot_readiness?.checks?.slice(0, 4).every(check => check.met);
  const canStart = Boolean(
    policy && !run && firstFourMet && capabilities.canStartPilot
  );
  $('#btn-start-pilot').classList.toggle('hidden', !canStart);
  $('#planner-empty').classList.toggle('hidden', Boolean(pilotPlan));
  $('#planner-plan').classList.toggle('hidden', !pilotPlan);
  if (!pilotPlan) {
    $('#planner-empty').textContent = run
      ? 'The latest Pilot plan is unavailable.'
      : (firstFourMet ? 'The first four gates are green; the relationship owner may start the Pilot run.' : 'Complete the first four gates to start a revisioned Pilot run.');
    return;
  }
  const ranked = pilotPlan.ranked_action || {};
  const rankedKind = rankedDecisionKind(pilotPlan) || 'wait';
  const rankedRole = pilotPlan.ranked_action?.required_role || pilotPlan.required_role;
  $('#planner-action').textContent = rankedKind.replaceAll('_', ' ');
  $('#planner-action-detail').textContent = `Required role: ${rankedRole.replaceAll('_', ' ')} · remaining slack: ${pilotPlan.remaining_slack_minutes} minutes`;
  const assignmentNames = new Map(opportunities.map(item => [item.id, item.guest_name]));
  const assignmentById = new Map((pilotPlan.assignments || []).map(item => [item.id, item]));
  const rankedAssignments = (ranked.assignments || []).map(item => {
    const assignment = assignmentById.get(item.assignment_id);
    const label = assignment ? assignmentNames.get(assignment.opportunity_id) : item.assignment_id;
    return `${label || item.assignment_id} · not before ${new Date(item.not_before).toLocaleString()}`;
  });
  if (ranked.assignment_id) {
    const assignment = assignmentById.get(ranked.assignment_id);
    rankedAssignments.push(assignmentNames.get(assignment?.opportunity_id) || ranked.assignment_id);
  }
  if (ranked.promoted_assignment_id) {
    const promoted = assignmentById.get(ranked.promoted_assignment_id);
    rankedAssignments.push(`Promote ${assignmentNames.get(promoted?.opportunity_id) || ranked.promoted_assignment_id}`);
  }
  $('#planner-assignments').innerHTML = plannerItems(rankedAssignments, 'No assignment mutation.');
  $('#planner-timing').innerHTML = plannerItems(pilotPlan.candidate_timing, 'No additional timing.');
  $('#planner-alternatives').innerHTML = plannerItems(pilotPlan.alternatives);
  $('#planner-constraints').innerHTML = plannerItems(pilotPlan.constraints);
  $('#planner-rationale').innerHTML = plannerItems(pilotPlan.rationale);

  const uniqueKinds = plannerDecisionKinds(
    pilotPlan,
    capabilities,
    kind => plannerRoleAllows(kind),
  );
  $('#planner-decisions').innerHTML = uniqueKinds.map(kind => (
    `<button type="button" data-pilot-decision="${escapeHTML(kind)}" class="${kind === 'pause' ? 'danger-button' : ''}">Record ${escapeHTML(kind.replaceAll('_', ' '))} decision</button>`
  )).join('') || '<span class="empty">This session has no legal planner decision for the current state.</span>';
}

function editForm(item) {
  return `<details class="item-editor"><summary>Edit or supersede</summary><form class="item-edit-form" data-item-id="${escapeHTML(item.id)}"><input type="hidden" name="expected_revision" value="${item.revision}"><input type="hidden" name="item_key" value="${escapeHTML(item.item_key)}"><label>Title<input name="title" value="${escapeHTML(item.title)}" required></label><label>Owner<input name="owner" value="${escapeHTML(item.owner)}" required></label><label>Category<select name="category">${categories.map(value => `<option ${value === item.category ? 'selected' : ''}>${value}</option>`).join('')}</select></label><label>State<select name="state">${writableStates.map(value => `<option ${value === item.state ? 'selected' : ''}>${value}</option>`).join('')}</select></label><label class="span-2">Summary<textarea name="summary" required>${escapeHTML(item.summary)}</textarea></label><label>External reference<input name="external_reference" value="${escapeHTML(item.external_reference || '')}"></label><label>Due date<input name="due_at" type="date" value="${escapeHTML(item.due_at || '')}"></label><button type="submit">Save revision ${item.revision + 1}</button><label>Successor item id<input name="successor_item_id" placeholder="required to supersede"></label><button class="danger-button" data-supersede type="button">Supersede, never delete</button></form></details>`;
}

function itemHTML(item) {
  const reference = item.external_reference ? `<code class="partnership-ref sensitive-ref">${escapeHTML(item.external_reference)}</code>` : '';
  return `<article class="partnership-item" data-state="${escapeHTML(item.state)}" data-category="${escapeHTML(item.category)}"><div class="partnership-item-head"><h4>${escapeHTML(item.title)}</h4><span class="state-pill">${escapeHTML(item.state.replaceAll('_', ' '))}</span></div><p>${escapeHTML(item.summary)}</p><div class="partnership-meta"><span>Owner · ${escapeHTML(item.owner)}</span>${item.due_at ? `<span>Due ${escapeHTML(item.due_at)}</span>` : ''}<span>Revision ${item.revision}</span><span>${escapeHTML(item.item_key)}</span></div>${reference}${item.state !== 'superseded' && capabilities.canManageRegister ? editForm(item) : ''}</article>`;
}

function renderRegister() {
  if (!center) return;
  const query = $('#register-search').value.trim().toLowerCase();
  const owner = $('#register-owner').value.trim().toLowerCase();
  const categoryFilter = $('#register-category').value;
  const stateFilter = $('#register-state').value;
  elements.sections.innerHTML = categories.map(category => {
    const items = (center.categories[category] || []).filter(item => (!query || `${item.title} ${item.summary} ${item.item_key}`.toLowerCase().includes(query)) && (!owner || item.owner.toLowerCase().includes(owner)) && (!categoryFilter || category === categoryFilter) && (!stateFilter || item.state === stateFilter));
    if (!items.length && categoryFilter && category !== categoryFilter) return '';
    const missingBlueprint = blueprint[category] || [];
    return `<section class="partnership-group" data-category="${category}"><div class="partnership-group-head"><h3>${labels[category]}</h3><span class="partnership-group-count">${items.length} item${items.length === 1 ? '' : 's'}</span></div>${missingBlueprint.length ? `<p class="blueprint">Blueprint: ${missingBlueprint.map(escapeHTML).join(' · ')}</p>` : ''}<div class="partnership-grid">${items.map(itemHTML).join('') || '<p class="empty">No matching items; the domain remains visible.</p>'}</div></section>`;
  }).join('');
  $('#revision-history').innerHTML = center.item_history.slice().sort((a, b) => b.created_at.localeCompare(a.created_at)).map(revision => `<article><b>${escapeHTML(revision.item_key)}</b><span>r${revision.revision} · ${escapeHTML(revision.change_kind)} · ${escapeHTML(new Date(revision.created_at).toLocaleString())}</span><span class="sensitive-identity">${escapeHTML(revision.changed_by)} · ${escapeHTML(revision.changed_role)}</span></article>`).join('');
}

function renderOpportunities() {
  const previousOpportunityId = elements.opportunity.value;
  const requestedId = requestedOpportunityId;
  requestedOpportunityId = '';
  const options = opportunities.map(item => `<option value="${escapeHTML(item.id)}">${escapeHTML(item.guest_name)} · ${escapeHTML(item.status)}</option>`).join('');
  elements.opportunity.innerHTML = options;
  $('#touchpoint-opportunity').innerHTML = options;
  elements.opportunity.value = resolveSelectedOpportunity(opportunities, previousOpportunityId, requestedId);
  if (elements.opportunity.value !== previousOpportunityId) {
    networkRequestVersion += 1;
    networkMap = null;
    renderNetworkMap();
  }
  $('#touchpoint-opportunity').value = elements.opportunity.value;
  if (!elements.networkForm.elements.guest.value) {
    const selected = selectedOpportunity();
    elements.networkForm.elements.guest.value = selected?.guest_name || '';
  }
  renderLegalActions();
}

function activeShowId() {
  return context?.show_id
    || document.querySelector('meta[name="hospes-active-show"]')?.content
    || '';
}
function money(minor, currency) {
  const value = Number(minor || 0) / 100;
  return `${value.toFixed(2)} ${currency || revenue?.currency || 'USD'}`;
}
function minorUnits(value, field) {
  const amount = Math.round(Number(value) * 100);
  if (!Number.isFinite(amount) || amount < 0) throw new Error(`${field} must be a non-negative amount.`);
  return amount;
}
function revenueFilters() {
  const episode = $('#revenue-episode').value.trim();
  return episode ? { episode_id: episode } : {};
}
function renderSponsorOptions() {
  const options = sponsorRoster
    .map(sponsor => `<option value="${escapeHTML(sponsor.id)}">${escapeHTML(sponsor.name)}</option>`)
    .join('');
  for (const selector of ['#sponsorship-sponsor', '#claim-sponsor']) {
    const select = $(selector);
    if (!select) continue;
    const previous = select.value;
    select.innerHTML = options;
    if (previous) select.value = previous;
  }
}
function renderSponsors() {
  const list = $('#sponsor-list');
  if (!sponsorRoster.length) {
    list.innerHTML = '<p class="empty">No sponsor is registered for this show.</p>';
    return;
  }
  list.innerHTML = sponsorRoster.map(sponsor => {
    const claims = (sponsor.claims || []).map(claim => {
      const state = claim.approved_for_external_use ? 'approved' : 'unapproved';
      const control = capabilities.canApproveSponsorClaims
        ? `<button type="button" data-claim-approve="${escapeHTML(claim.claim_id)}" data-claim-sponsor="${escapeHTML(sponsor.id)}" data-claim-state="${claim.approved_for_external_use ? 'withdraw' : 'approve'}">${claim.approved_for_external_use ? 'Withdraw approval' : 'Approve for external use'}</button>`
        : '';
      return `<li class="${state}"><b>${escapeHTML(claim.claim)}</b><code>${escapeHTML(claim.source_url)}</code><span>verified ${escapeHTML(claim.verified_date)} · ${escapeHTML(state)}</span>${control}</li>`;
    }).join('') || '<li class="empty">No claim recorded.</li>';
    const unapproved = Number(sponsor.unapproved_claim_count || 0);
    return `<article class="sponsor-card"><h4>${escapeHTML(sponsor.name)}</h4><p class="sponsor-meta">${escapeHTML(sponsor.category || 'uncategorized')} · ${escapeHTML(sponsor.status)} · ${unapproved} unapproved claim(s)</p><p class="sponsor-meta sensitive-ref">${escapeHTML(sponsor.contact_ref)} · ${escapeHTML(sponsor.terms_ref)}</p><ul>${claims}</ul></article>`;
  }).join('');
}
function renderRevenue() {
  const rows = $('#revenue-rows');
  const inventory = $('#slot-inventory-rows');
  const gate = $('#revenue-gate');
  for (const selector of ['#btn-add-sponsor', '#ad-slot-form', '#sponsorship-form', '#sponsor-claim-form']) {
    $(selector).classList.toggle('hidden', !capabilities.canManageSponsors);
  }
  if (!revenue) {
    rows.innerHTML = '<tr class="empty-row"><td colspan="4">No sponsor inventory is declared for this show.</td></tr>';
    inventory.innerHTML = '<tr class="empty-row"><td colspan="6">No ad slot is declared for this show.</td></tr>';
    gate.innerHTML = '<li class="empty">No sponsor blocker on any episode.</li>';
    return;
  }
  const totals = revenue.totals;
  $('#rev-slots-sold').textContent = totals.slots_sold;
  $('#rev-slots-available').textContent = totals.slots_available;
  $('#rev-committed-unfilled').textContent = totals.committed_unfilled;
  $('#rev-total').textContent = money(totals.revenue_minor, revenue.currency);
  $('#revenue-policy-note').textContent = `policy: ${revenue.policy.source} · committed gate ${revenue.policy.block_publication_on_unfilled_committed_slots ? 'on' : 'off'}`;
  rows.innerHTML = revenue.episodes.map(episode => {
    const head = `<tr class="episode-row"><td${episode.publication_blocked ? ' class="blocked"' : ''}>${escapeHTML(episode.episode_id)}${episode.publication_blocked ? ' · blocked' : ''}</td><td>All sponsors</td><td class="numeric">${episode.slots_sold}/${episode.slots_total}</td><td class="numeric">${escapeHTML(money(episode.revenue_minor, episode.currency))}</td></tr>`;
    const sponsors = episode.sponsors.map(sponsor => (
      `<tr><td></td><td>${escapeHTML(sponsor.sponsor_name || sponsor.sponsor_id)}</td><td class="numeric">${sponsor.slots_sold}/${sponsor.slots_held}</td><td class="numeric">${escapeHTML(money(sponsor.revenue_minor, episode.currency))}</td></tr>`
    )).join('');
    return head + sponsors;
  }).join('') || '<tr class="empty-row"><td colspan="4">No sponsor inventory is declared for this show.</td></tr>';
  inventory.innerHTML = slotInventory.map(slot => {
    const release = capabilities.canManageSponsors && slot.sponsor_id
      ? `<button type="button" data-release-episode="${escapeHTML(slot.episode_id)}" data-release-slot="${escapeHTML(slot.slot_type)}">Release</button>`
      : '';
    const state = slot.committed ? `${slot.status} · committed` : slot.status;
    return `<tr><td>${escapeHTML(slot.episode_id)}</td><td>${escapeHTML(slot.slot_type)}</td><td class="numeric">${escapeHTML(money(slot.rate_minor, slot.currency))}</td><td>${escapeHTML(state)}</td><td>${escapeHTML(slot.sponsor_name || 'unsold')}</td><td>${release}</td></tr>`;
  }).join('') || '<tr class="empty-row"><td colspan="6">No ad slot is declared for this show.</td></tr>';
  const blockers = revenue.episodes.flatMap(episode => episode.publication_blockers);
  gate.innerHTML = blockers.map(blocker => (
    `<li><time>${escapeHTML(blocker.episode_id)}</time><b>${escapeHTML(blocker.kind.replaceAll('_', ' '))}</b><span>${escapeHTML(blocker.detail)}</span></li>`
  )).join('') || '<li class="empty">No sponsor blocker on any episode.</li>';
}
async function refreshRevenue() {
  const showId = activeShowId();
  if (!showId) {
    setStatus('An active show is required before reading revenue.', true);
    return;
  }
  const filters = revenueFilters();
  setStatus('Loading sponsor inventory and revenue…');
  const [report, slots, roster] = await Promise.all([
    loadRevenue(showId, filters),
    loadAdSlots(showId, filters),
    loadSponsors(showId),
  ]);
  revenue = report;
  slotInventory = slots;
  sponsorRoster = roster;
  renderSponsorOptions();
  renderSponsors();
  renderRevenue();
  setStatus(`Revenue loaded: ${report.totals.slots_sold} sold of ${report.totals.slots_total} declared slots.`);
}

function publishFilters() {
  const episode = $('#publish-episode').value.trim();
  return episode ? { episode_id: episode } : {};
}
function parseChapters(value) {
  return String(value || '').split('\n').map(line => line.trim()).filter(Boolean).map(line => {
    const [start, ...rest] = line.split(/\s+/);
    const seconds = Number(start);
    if (!Number.isInteger(seconds) || seconds < 0 || !rest.length) {
      throw new Error(`Each chapter line must be "seconds title": ${line}`);
    }
    return { start_seconds: seconds, title: rest.join(' ') };
  });
}
function parseList(value) {
  return String(value || '').split(',').map(item => item.trim()).filter(Boolean);
}
function setPreview(note, body) {
  $('#publish-preview-note').textContent = note;
  $('#publish-preview-output').value = body;
}
function publishActionForms(card) {
  if (!capabilities.canManagePublishing) return '';
  const id = escapeHTML(card.distribution_id);
  const forms = [
    `<form class="publish-action-form" data-publish-action="preview" data-distribution-id="${id}"><button type="submit">Preview package</button></form>`,
  ];
  if (['draft', 'blocked', 'failed'].includes(card.status) && capabilities.canAuthorizePublication) {
    forms.push(
      `<form class="publish-action-form" data-publish-action="authorize" data-distribution-id="${id}"><label>Human authorization reference<input name="authorization_ref" required autocomplete="off" spellcheck="false" placeholder="receipt://owner/publish-12"></label><button type="submit">Authorize publication</button></form>`,
    );
  }
  if (['authorized', 'scheduled', 'failed'].includes(card.status)) {
    forms.push(
      `<form class="publish-action-form" data-publish-action="schedule" data-distribution-id="${id}"><label>Publish at<input name="scheduled_at" type="datetime-local" required></label><button type="submit">Schedule</button></form>`,
    );
    if (capabilities.canAuthorizePublication) {
      forms.push(
        `<form class="publish-action-form" data-publish-action="publish" data-distribution-id="${id}"><label>External id<input name="external_id_ref" required autocomplete="off" spellcheck="false" placeholder="youtube://video/abc123"></label><button class="btn-primary" type="submit">Mark published</button></form>`,
      );
    }
    forms.push(
      `<form class="publish-action-form" data-publish-action="fail" data-distribution-id="${id}"><label>Failure evidence<input name="error_ref" required autocomplete="off" spellcheck="false" placeholder="error://provider/timeout"></label><button class="danger-button" type="submit">Record failed attempt</button></form>`,
    );
  }
  return `<div class="publish-actions">${forms.join('')}</div>`;
}
function publishCard(card) {
  const outstanding = card.completeness.missing.length
    ? `incomplete · missing ${card.completeness.missing.join(', ')}`
    : 'package complete';
  const window = card.family === 'clip' ? ` · ${escapeHTML(card.target_platform || 'unassigned')}` : '';
  const evidence = [
    card.authorized ? 'authorized' : 'not authorized',
    card.scheduled_at ? `scheduled ${new Date(card.scheduled_at).toLocaleString()}` : '',
    card.published_at ? `published ${new Date(card.published_at).toLocaleString()}` : '',
    card.external_id_ref ? `external ${card.external_id_ref}` : '',
    card.attempt_count ? `${card.attempt_count} attempt(s)` : '',
    card.last_error_ref ? `last failure ${card.last_error_ref}` : '',
  ].filter(Boolean).map(item => `<span>${escapeHTML(item)}</span>`).join('');
  return `<article class="publish-card" data-status="${escapeHTML(card.status)}" data-distribution-id="${escapeHTML(card.distribution_id)}">
    <div class="publish-card-head"><h4>${escapeHTML(card.platform)}${window}</h4><span class="state-pill">${escapeHTML(card.status)}</span></div>
    <p>${escapeHTML(card.title || card.caption_ref || 'Untitled package')}</p>
    <div class="publish-meta">${evidence}<span>${escapeHTML(outstanding)}</span></div>
    ${publishActionForms(card)}
  </article>`;
}
function renderPublishBoard() {
  const container = $('#publish-cards');
  const gate = $('#publish-gate');
  const adapters = $('#publish-adapters');
  $('#publish-view').classList.toggle('hidden', activeView !== 'publish');
  for (const selector of ['#rss-form', '#youtube-form', '#clip-form', '#btn-verify-adapters']) {
    $(selector).classList.toggle('hidden', !capabilities.canManagePublishing);
  }
  if (!publishBoard) {
    container.innerHTML = '<p class="empty">No publication package is drafted for this show.</p>';
    gate.innerHTML = '<li class="empty">No blocker on any episode with a package.</li>';
    adapters.innerHTML = '<li class="empty">No adapter state loaded.</li>';
    return;
  }
  const totals = publishBoard.totals;
  $('#pub-published').textContent = totals.published;
  $('#pub-scheduled').textContent = totals.scheduled;
  $('#pub-failed').textContent = totals.failed;
  $('#pub-blocked').textContent = totals.blocked_episodes;
  $('#publish-webhook').textContent = `webhook: ${publishBoard.webhook.status}${publishBoard.webhook.reason ? ` · ${publishBoard.webhook.reason}` : ''}`;
  container.innerHTML = publishBoard.episodes.map(episode => {
    const cards = [...episode.platforms, ...episode.clips].map(publishCard).join('')
      || '<p class="empty">No package for this episode.</p>';
    const state = episode.publishable ? 'publishable' : `blocked · ${episode.blockers.length} blocker(s)`;
    return `<section class="publish-episode"><div class="panel-head"><h4>${escapeHTML(episode.episode_id)}</h4><span class="${episode.publishable ? '' : 'blocked'}">${escapeHTML(state)}</span></div><div class="publish-card-grid">${cards}</div></section>`;
  }).join('') || '<p class="empty">No publication package is drafted for this show.</p>';
  gate.innerHTML = publishBoard.episodes.flatMap(episode => episode.blockers.map(blocker => (
    `<li><time>${escapeHTML(episode.episode_id)}</time><b>${escapeHTML(String(blocker.kind || blocker.clearance_type || 'clearance').replaceAll('_', ' '))}</b><span>${escapeHTML(blocker.detail || `${blocker.status} rights item`)}</span></li>`
  ))).join('') || '<li class="empty">No blocker on any episode with a package.</li>';
  adapters.innerHTML = Object.values(publishBoard.adapters).map(adapter => (
    `<li class="adapter ${escapeHTML(adapter.status)}"><b>${escapeHTML(adapter.platform)}</b><span>${escapeHTML(adapter.provider || 'no adapter')} · ${escapeHTML(adapter.status)} · ${escapeHTML(adapter.delivery_mode)}</span><span>${escapeHTML(adapter.reason || 'ready for a manual receipt')}</span></li>`
  )).join('');
}
async function refreshPublish() {
  const showId = activeShowId();
  if (!showId) {
    setStatus('An active show is required before reading the publish board.', true);
    return;
  }
  if (!capabilities.canViewPublishing) {
    publishBoard = null;
    renderPublishBoard();
    return;
  }
  setStatus('Loading publication packages and delivery evidence…');
  publishBoard = await loadPublishBoard(showId, publishFilters());
  renderPublishBoard();
  setStatus(`Publish board loaded: ${publishBoard.totals.jobs} package(s), ${publishBoard.totals.published} published, ${publishBoard.totals.clips} clip(s) queued.`);
}

function renderGuestTouchpoints() {
  const opportunityId = elements.opportunity.value;
  const items = touchpointTimeline.filter(item => item.opportunity_id === opportunityId);
  $('#guest-touchpoint-panel').classList.toggle('hidden', !capabilities.canViewTouchpoints);
  $('#guest-touchpoint-timeline').innerHTML = items.map(item => {
    const guest = item.guest_name || item.guest_id;
    return `<li><time>${escapeHTML(new Date(item.occurred_at).toLocaleString())}</time><b>${escapeHTML(item.initiator)} → ${escapeHTML(guest)} · ${escapeHTML(item.channel)}</b><span>${escapeHTML(item.notes || 'Encrypted note unavailable')}</span><span class="sensitive-identity">${escapeHTML(item.initiator_role)}</span></li>`;
  }).join('') || '<li class="empty">No informal touchpoints for this guest.</li>';
}

function scopedShowIds() {
  return [...new Set(opportunities.map(item => item.show_id).filter(Boolean))];
}

function clearanceDecisionForm(item) {
  if (!capabilities.canManageClearances) return '';
  const options = ['pending', 'cleared', 'denied']
    .map(value => `<option value="${value}" ${value === item.status ? 'selected' : ''}>${value}</option>`)
    .join('');
  return `<form class="clearance-decision-form" data-clearance-id="${escapeHTML(item.clearance_id)}" data-show-id="${escapeHTML(item.show_id)}"><label>Status<select name="status">${options}</select></label><label>Evidence reference<input name="evidence_ref" required autocomplete="off" spellcheck="false" placeholder="registry://owner/licence" value="${escapeHTML(item.evidence_ref || '')}"></label><button type="submit">Save decision</button></form>`;
}

function renderClearances() {
  const panel = $('#clearance-panel');
  panel.classList.toggle('hidden', !capabilities.canViewClearances);
  $('#clearance-capture').classList.toggle('hidden', !capabilities.canManageClearances);
  $('#btn-clearance-report').classList.toggle('hidden', !capabilities.canViewClearances);
  $('#clearance-badges').innerHTML = clearanceBadges.length
    ? clearanceBadges.map(badge => `<span class="clearance-badge ${badge.publishable ? 'cleared' : 'blocking'}"><b>${escapeHTML(badge.episode_id)}</b><span>${escapeHTML(badge.badge)}</span></span>`).join('')
    : '<span class="empty">No clearances recorded for this show.</span>';
  const items = clearanceStatusFilter
    ? clearanceItems.filter(item => item.status === clearanceStatusFilter)
    : clearanceItems;
  $('#clearance-list').innerHTML = items.map(item => {
    const holder = item.rights_holder_available ? item.rights_holder : item.rights_holder_ref;
    const terms = item.license_terms_available ? item.license_terms : item.license_terms_ref;
    const due = item.due_date ? `due ${item.due_date}` : 'no due date';
    const custody = item.custody_mode === 'sealed' ? 'encrypted custody' : 'external custody reference';
    return `<li data-clearance-id="${escapeHTML(item.clearance_id)}"><time>${escapeHTML(due)}</time><b>${escapeHTML(item.episode_id)} · ${escapeHTML(item.type)} · ${escapeHTML(item.status)}${item.blocks_publication ? ' · blocks publication' : ''}</b><span class="sensitive-ref">${escapeHTML(holder)}</span><span class="sensitive-ref">${escapeHTML(terms)}</span><span>${escapeHTML(custody)} · ${item.cost_minor} minor units${item.decided_by ? ` · decided by ${escapeHTML(item.decided_by)}` : ''}</span>${clearanceDecisionForm(item)}</li>`;
  }).join('') || '<li class="empty">No rights items match this filter.</li>';
}

async function refreshClearances() {
  if (!capabilities.canViewClearances) {
    clearanceItems = [];
    clearanceBadges = [];
    renderClearances();
    return;
  }
  const showIds = scopedShowIds();
  try {
    const [lists, badges] = await Promise.all([
      Promise.all(showIds.map(showId => loadClearances(showId, { limit: 200 }))),
      Promise.all(showIds.map(showId => loadClearanceBadges(showId))),
    ]);
    clearanceItems = lists.flat();
    clearanceBadges = badges.flat();
  } catch (error) {
    clearanceItems = [];
    clearanceBadges = [];
    setStatus(error.message, true);
  }
  renderClearances();
}

function guestOptions() {
  return opportunities
    .map(item => `<option value="${escapeHTML(item.id)}">${escapeHTML(item.guest_name)}</option>`)
    .join('');
}

function renderGuestRegister() {
  const panel = $('#guest-register-panel');
  if (!panel) return;
  panel.classList.toggle('hidden', !capabilities.canViewGuestHistory);
  const selector = $('#guest-history-guest');
  const previous = selector.value;
  selector.innerHTML = guestOptions();
  if (opportunities.some(item => item.id === previous)) selector.value = previous;
  const recorder = $('#guest-history-opportunity');
  recorder.innerHTML = guestOptions();
  recorder.value = selector.value;
  $('#guest-history-form').classList.toggle('hidden', !capabilities.canManageRegister);
  $('#guest-history-disposition').innerHTML = (context?.guest_history_dispositions || [])
    .map(value => `<option>${escapeHTML(value)}</option>`).join('');
  const selected = opportunities.find(item => item.id === selector.value);
  $('#guest-history-summary').textContent = selected
    ? `${guestHistory.length} recorded interaction${guestHistory.length === 1 ? '' : 's'} for ${selected.guest_name}${selected.do_not_contact ? ' · do not contact' : ''}`
    : 'No candidate exists in this show yet.';
  $('#guest-history-timeline').innerHTML = guestHistory.map(item => (
    `<li><time>${escapeHTML(item.date)}</time><b>${escapeHTML(item.season)}${escapeHTML(item.episode)} · ${escapeHTML(item.disposition)}</b><span>${escapeHTML(item.notes || 'No revealed note for this entry.')}</span><span class="sensitive-identity">${escapeHTML(item.recorded_by)} · ${escapeHTML(item.recorded_by_role)}</span></li>`
  )).join('') || '<li class="empty">No cross-season history for this guest.</li>';
}

async function refreshGuestRegister() {
  const selector = $('#guest-history-guest');
  if (!selector || !capabilities.canViewGuestHistory) { guestHistory = []; renderGuestRegister(); return; }
  const opportunityId = selector.value || opportunities[0]?.id || '';
  if (!opportunityId) { guestHistory = []; renderGuestRegister(); return; }
  try {
    guestHistory = await loadGuestHistory(opportunityId, {
      season: $('#guest-history-season').value.trim(),
      limit: 200,
    });
  } catch (error) {
    guestHistory = [];
    setStatus(error.message, true);
  }
  renderGuestRegister();
}

function renderNetworkMap() {
  const summary = $('#network-map-summary');
  const paths = $('#network-target-paths');
  if (!networkMap) {
    summary.textContent = 'Choose a public guest name or scoped opaque alias. Private labels remain redacted.';
    paths.innerHTML = '<li class="empty">No path requested.</li>';
    return;
  }
  const counts = networkMap.summary;
  summary.textContent = `${counts.node_count} scoped people · ${counts.edge_count} evidence edges · ${counts.reachable_target_count} reachable C2/C3 target paths`;
  const labelsById = new Map(networkMap.nodes.map(node => [node.id, node.label]));
  paths.innerHTML = networkMap.target_paths.map(path => {
    const labels = path.node_ids.map(nodeId => labelsById.get(nodeId) || nodeId);
    return `<li><b>${escapeHTML(path.target_label)}</b><span class="path-chain">${labels.map(escapeHTML).join(' → ')}</span><code>${path.edge_ids.map(escapeHTML).join(' · ')}</code></li>`;
  }).join('') || '<li class="empty">No C2/C3 archive target is reachable within this depth.</li>';
}

function resetDraftConsole() {
  $('#draft-preview-output').value = '';
  $('#draft-preview-output').classList.add('hidden');
  $('#btn-review-draft').classList.add('hidden');
  $('#btn-copy-draft').classList.add('hidden');
}

function renderAssetPackages() {
  const packages = opportunityDetail?.asset_packages || [];
  $('#asset-package-list').innerHTML = packages.length
    ? packages.slice().sort((a, b) => b.created_at.localeCompare(a.created_at)).map(assetPackage => `<div><code>${escapeHTML(assetPackage.id)}</code><span>${isCompletePackage(assetPackage) ? 'complete six-kind package' : 'incomplete package'}</span></div>`).join('')
    : '<p class="empty">No package UUIDs for this candidate.</p>';
}

function renderContactRoster() {
  const panel = $('#contact-roster-panel');
  const references = opportunityDetail?.contact_roster_refs || [];
  panel.classList.toggle('hidden', !capabilities.canViewContacts || !references.length);
  const pills = $('#contact-roster-pills');
  if (!capabilities.canViewContacts || !references.length) {
    pills.innerHTML = '<span class="empty">No encrypted contact routes on file.</span>';
    $('#invitation-route-prefill').value = 'No verified route selected';
    return;
  }
  pills.innerHTML = contactRoster.length
    ? contactRoster.map(item => {
      const routeKind = item.email ? 'email' : (item.phone ? 'phone' : 'no usable route');
      const state = item.usable && item.verified_at ? 'verified' : item.permission_status.replaceAll('_', ' ');
      return `<span class="contact-pill sensitive-ref"><b>${escapeHTML(item.role)}</b><span>${escapeHTML(item.name)}</span><small>${escapeHTML(routeKind)} · ${escapeHTML(state)}</small></span>`;
    }).join('')
    : references.map(item => `<span class="contact-pill"><b>${escapeHTML(item.role)}</b><span>encrypted route on file</span><small>${escapeHTML(item.permission_status.replaceAll('_', ' '))}</small></span>`).join('');
  const prefill = invitationRoutePrefill;
  $('#invitation-route-prefill').value = prefill
    ? `${prefill.role} · ${prefill.name} · ${prefill.route_kind}: ${prefill.route_value}`
    : 'No verified route selected';
}

async function refreshOpportunityDetail() {
  const opportunityId = elements.opportunity.value;
  opportunityDetail = null;
  contactRoster = [];
  invitationRoutePrefill = null;
  resetDraftConsole();
  if (!opportunityId) { renderLegalActions(); renderReceiptFields(); renderAssetPackages(); renderContactRoster(); renderGuestTouchpoints(); return; }
  try {
    opportunityDetail = await loadOpportunityDetail(opportunityId);
    if (capabilities.canViewContacts && opportunityDetail.contact_roster_refs?.length) {
      const payload = await loadContactRoster(opportunityId);
      contactRoster = payload.items || [];
      invitationRoutePrefill = payload.invitation_prefill || null;
    }
  } catch (error) {
    setStatus(error.message, true);
  }
  renderLegalActions();
  renderReceiptFields();
  renderAssetPackages();
  renderContactRoster();
  renderGuestTouchpoints();
}

function renderLegalActions() {
  const item = selectedOpportunity();
  const actions = [];
  const readOnly = capabilities.completed;
  if (!item) actions.push('No candidate selected');
  if (item && !readOnly && can('host', 'editorial_owner', 'relationship_owner') && item.status === 'EDITORIAL_REVIEW') actions.push('approve, reject, or protect in the queue');
  if (item && !readOnly && can('host', 'editorial_owner', 'relationship_owner') && item.disposition === 'APPROVED' && !['C4', 'C5'].includes(item.relationship_class)) actions.push('assign primary or backup position');
  if (item && capabilities.canDraft && item.disposition === 'APPROVED' && !['C4', 'C5'].includes(item.relationship_class) && draftableStates.has(item.status)) actions.push('preview, review, and copy an invitation');
  if (item && capabilities.canLogTouchpoint) actions.push('log an informational chat without changing workflow state');
  if (item && capabilities.canRecordEvidence) actions.push('record studio, brief, assets, receipts, and commitments when lifecycle-valid');
  $('#legal-actions').innerHTML = actions.map(action => `<span>${escapeHTML(action)}</span>`).join('') || '<span>No write action is legal for this role and state.</span>';
  $('#slot-form').classList.toggle('hidden', readOnly || !can('host', 'editorial_owner', 'relationship_owner') || item?.disposition !== 'APPROVED' || ['C4', 'C5'].includes(item?.relationship_class));
  $('#draft-console').classList.toggle('hidden', !capabilities.canDraft || item?.disposition !== 'APPROVED' || ['C4', 'C5'].includes(item?.relationship_class) || !draftableStates.has(item?.status));
  for (const id of ['#studio-form', '#brief-form', '#asset-form', '#commitment-form', '#receipt-form']) $(id).classList.toggle('hidden', !capabilities.canRecordEvidence);
  $('#review-form').classList.toggle('hidden', !capabilities.canReviewPartnership);
  $('#btn-log-touchpoint').classList.toggle('hidden', !capabilities.canLogTouchpoint || !item);
}

function renderReceiptFields() {
  elements.receiptDetails.innerHTML = receiptFields[elements.receiptType.value].map(field => {
    const label = field.name.replaceAll('_', ' ');
    const options = field.name === 'classification' ? (context?.reply_classifications || []) : field.options;
    if (field.type === 'select') return `<label>${label}<select name="${field.name}">${options.map(option => `<option>${escapeHTML(option)}</option>`).join('')}</select></label>`;
    if (field.type === 'checkbox') return `<label class="checkbox"><input name="${field.name}" type="checkbox" ${field.checked ? 'checked' : ''}>${label}</label>`;
    const value = field.name === 'asset_package_id' ? newestCompletePrimaryPackage()?.id || '' : '';
    return `<label>${label}<input name="${field.name}" type="${field.type || 'text'}" value="${escapeHTML(value)}" required placeholder="${field.name.endsWith('_ref') ? 'registry://owner/reference' : ''}"></label>`;
  }).join('') || '<p class="empty">This receipt type accepts no details.</p>';
  const missingReviewedDraft = elements.receiptType.value === 'outreach.sent' && !reviewedDraftExists();
  $('#receipt-submit').disabled = missingReviewedDraft;
  $('#receipt-draft-warning').classList.toggle('hidden', !missingReviewedDraft);
}

async function refreshCenter() {
  if (!activePartnershipId) return;
  elements.refresh.disabled = true;
  setStatus('Loading live partnership, pilot, and authority state…');
  try {
    [center, opportunities] = await Promise.all([loadPartnershipCommandCenter(activePartnershipId), loadOpportunities()]);
    const showIds = scopedShowIds();
    touchpointTimeline = capabilities.canViewTouchpoints
      ? (await Promise.all(showIds.map(showId => loadTouchpoints(showId, {
        partnership_id: activePartnershipId,
        limit: 200,
      })))).flat().sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
      : [];
    await hydrateActiveView();
    setStatus(`Loaded ${center.summary.total} current items, ${center.pilot_readiness.met}/${center.pilot_readiness.total} pilot gates, and ${center.coverage.covered}/${center.coverage.total} register domains.`);
  } catch (error) { setStatus(error.message, true); }
  finally { elements.refresh.disabled = false; }
}

// A decision committed in the approval queue writes the same candidate
// lifecycle this workspace projects, so it is reconciled exactly like the
// workspace's own writes: re-read live truth, then land the selector on the
// candidate the decision just decided. Reconciles are serialized so a rapid
// run of decisions cannot stampede the API with overlapping reloads.
let decisionReconcile = Promise.resolve();

async function reconcileCommittedDecision(opportunityId) {
  requestedOpportunityId = opportunityId;
  await refreshCenter();
}

async function loadRegistry() {
  setStatus('Loading operator context and partnership registry…');
  try {
    const previousPartnershipId = activePartnershipId;
    [context, registries] = await Promise.all([loadOperatorContext(), loadPartnerships()]);
    capabilities = capabilitiesFor(context);
    renderContext();
    elements.select.innerHTML = registries.map(partnership => `<option value="${escapeHTML(partnership.id)}">${escapeHTML(partnership.label)}</option>`).join('');
    activePartnershipId = registries.some(partnership => partnership.id === previousPartnershipId) ? previousPartnershipId : registries[0]?.id || '';
    if (!activePartnershipId) { setStatus('No partnership exists in this tenant.', true); return; }
    elements.select.value = activePartnershipId;
    await refreshCenter();
  } catch (error) { setStatus(error.message, true); }
}

async function hydrateWorkbenchQueue() {
  const queueModule = await import('./app.js');
  if (workbenchQueueHydrated) return;
  await queueModule.hydrateApprovalQueue();
  workbenchQueueHydrated = true;
}

// --- Analytics view ------------------------------------------------------
// A read surface: it charts and exports what providers reported and shows the
// receipt behind each read. It has no control that mutates a provider.

function analyticsShowId() {
  return context?.show_id
    || document.querySelector('meta[name="hospes-active-show"]')?.content
    || opportunities.find(item => item.show_id)?.show_id
    || '';
}

function setAnalyticsStatus(message, error = false) {
  const target = $('#analytics-status');
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('error', error);
}

function formatMetric(value, suffix = '') {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString()}${suffix}` : String(value);
}

function renderTrendChart(points) {
  const target = $('#analytics-trend-chart');
  if (!target) return;
  const series = points.map(point => Number(point.downloads)).map(value => (Number.isFinite(value) ? value : 0));
  if (!points.length || series.every(value => value === 0)) {
    target.innerHTML = '<p class="empty">No numeric download trend has been reported yet.</p>';
    return;
  }
  const peak = Math.max(...series, 1);
  const width = Math.max(points.length * 44, 220);
  const bars = points.map((point, index) => {
    const height = Math.max(Math.round((series[index] / peak) * 120), 2);
    const x = index * 44 + 8;
    const label = escapeHTML(point.episode_id);
    const value = escapeHTML(String(point.downloads ?? 0));
    return `<g><title>${label} · ${value} downloads</title>`
      + `<rect class="trend-bar" x="${x}" y="${132 - height}" width="28" height="${height}" rx="3"></rect>`
      + `<text class="trend-label" x="${x + 14}" y="148" text-anchor="middle">${label.slice(-4)}</text></g>`;
  }).join('');
  // Values are numbers derived above and labels are escaped, so this markup
  // carries no provider-controlled HTML.
  target.innerHTML = `<svg class="trend-svg" viewBox="0 0 ${width} 156" role="img" `
    + `aria-label="Downloads for the last ${points.length} reported episodes" preserveAspectRatio="xMinYMid meet">`
    + `<line class="trend-axis" x1="0" y1="132" x2="${width}" y2="132"></line>${bars}</svg>`;
}

function renderAnalytics() {
  const rows = $('#analytics-rows');
  const view = $('#analytics-view');
  if (!rows || !view) return;
  const exportButton = $('#btn-analytics-export');
  if (exportButton) exportButton.disabled = !capabilities.canExportAnalytics;
  if (!capabilities.canViewAnalytics) {
    rows.innerHTML = '<tr><td colspan="6" class="empty">This role cannot read show analytics.</td></tr>';
    $('#analytics-trend-chart').innerHTML = '<p class="empty">This role cannot read show analytics.</p>';
    $('#analytics-receipt-list').innerHTML = '<li class="empty">This role cannot read provider receipts.</li>';
    return;
  }
  const points = analyticsTrends?.episodes || [];
  const totals = analyticsTrends?.totals || {};
  $('#an-downloads').textContent = formatMetric(totals.downloads);
  $('#an-retention').textContent = formatMetric(totals.retention_pct);
  $('#an-clips').textContent = formatMetric(totals.top_clip_views);
  $('#an-episodes').textContent = formatMetric(analyticsTrends?.episodes_available ?? points.length);
  $('#analytics-trend-caption').textContent = points.length
    ? `last ${points.length} of ${analyticsTrends.episodes_available} reported episodes`
    : 'awaiting first read';
  rows.innerHTML = points.slice().reverse().map(point => (
    `<tr><th scope="row">${escapeHTML(point.episode_id)}</th>`
    + `<td>${escapeHTML(formatMetric(point.downloads))}</td>`
    + `<td>${escapeHTML(formatMetric(point.retention_pct, '%'))}</td>`
    + `<td>${escapeHTML(formatMetric(point.top_clip_views))}</td>`
    + `<td>${escapeHTML(`${point.period_start} → ${point.period_end}`)}</td>`
    + `<td>${escapeHTML((point.providers || []).join(', '))}</td></tr>`
  )).join('') || '<tr><td colspan="6" class="empty">No analytics have been imported for this show.</td></tr>';
  renderTrendChart(points);
  $('#analytics-receipt-list').innerHTML = analyticsReceipts.slice(0, 12).map(receipt => (
    `<li><time>${escapeHTML(new Date(receipt.created_at).toLocaleString())}</time>`
    + `<b>${escapeHTML(receipt.provider)} · ${escapeHTML(receipt.status)}</b>`
    + `<span>${escapeHTML(receipt.receipt_ref)}</span></li>`
  )).join('') || '<li class="empty">No provider verification has been recorded for this show.</li>';
}

async function hydrateAnalytics() {
  const showId = analyticsShowId();
  if (!capabilities.canViewAnalytics || !showId) {
    analyticsTrends = null;
    analyticsReceipts = [];
    renderAnalytics();
    setAnalyticsStatus(showId ? 'This role cannot read show analytics.' : 'No show scope is active.', true);
    return;
  }
  const requestVersion = ++analyticsRequestVersion;
  setAnalyticsStatus('Loading provider metrics, trends, and receipts…');
  const limit = Number($('#analytics-window')?.value || 12);
  try {
    const [trends, receipts] = await Promise.all([
      loadAnalyticsTrends(showId, limit),
      loadAnalyticsReceipts(showId),
    ]);
    if (requestVersion !== analyticsRequestVersion) return;
    analyticsTrends = trends;
    analyticsReceipts = receipts;
    renderAnalytics();
    setAnalyticsStatus(
      `Loaded ${trends.episodes.length} of ${trends.episodes_available} reported episodes `
      + `and ${receipts.length} provider receipt${receipts.length === 1 ? '' : 's'}.`,
    );
  } catch (error) {
    if (requestVersion !== analyticsRequestVersion) return;
    setAnalyticsStatus(error.message, true);
  }
}

async function exportAnalytics() {
  if (!capabilities.canExportAnalytics) {
    setAnalyticsStatus('This session cannot export analytics.', true);
    return;
  }
  const showId = analyticsShowId();
  if (!showId) {
    setAnalyticsStatus('No show scope is active.', true);
    return;
  }
  try {
    const csv = await loadAnalyticsExport(showId);
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `analytics-${showId}.csv`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    setAnalyticsStatus('Exported the reporting CSV for this show.');
  } catch (error) {
    setAnalyticsStatus(error.message, true);
  }
}

// --- Network portfolio view ----------------------------------------------
// The one cross-show surface. It reads the tenant the session is already
// authenticated for — no show id is sent — and every control it offers is a
// read: refresh, export the health report, or open a show's own dashboard.

function setNetworkStatus(message, error = false) {
  const target = $('#network-status');
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('error', error);
}

function networkDrilldownButton(showId, label) {
  return `<button class="quiet network-drilldown" type="button" data-network-show="${escapeHTML(showId)}"`
    + ` aria-label="Open the ${escapeHTML(label)} dashboard">Open dashboard</button>`;
}

function renderNetworkPortfolio() {
  const rows = $('#network-rows');
  const overlapRows = $('#network-overlap-rows');
  if (!rows || !overlapRows) return;
  const exportButton = $('#btn-network-export');
  if (exportButton) exportButton.disabled = !capabilities.canExportNetworkReport;
  if (!capabilities.canViewNetwork) {
    rows.innerHTML = '<tr><td colspan="8" class="empty">Only a network operator may read the portfolio.</td></tr>';
    overlapRows.innerHTML = '<tr><td colspan="3" class="empty">Only a network operator may read guest overlap.</td></tr>';
    $('#network-receipt-list').innerHTML = '<li class="empty">Only a network operator may read these receipts.</li>';
    return;
  }
  const shows = networkPortfolio?.shows || [];
  const totals = networkPortfolio?.totals || {};
  $('#nw-shows').textContent = formatMetric(totals.shows);
  $('#nw-candidates').textContent = formatMetric(totals.candidates);
  $('#nw-booked').textContent = formatMetric(totals.booked);
  $('#nw-velocity').textContent = formatMetric(totals.bookings_per_week);
  $('#nw-revenue').textContent = networkPortfolio ? money(totals.revenue_minor, totals.currency) : '—';
  $('#network-portfolio-caption').textContent = networkPortfolio
    ? `booking velocity over the last ${networkPortfolio.window_days} days`
    : 'awaiting first read';
  rows.innerHTML = shows.map(show => (
    `<tr><th scope="row">${escapeHTML(show.label)}</th>`
    + `<td class="numeric">${escapeHTML(formatMetric(show.pipeline.candidates))}</td>`
    + `<td class="numeric">${escapeHTML(formatMetric(show.pipeline.approved))}</td>`
    + `<td class="numeric">${escapeHTML(formatMetric(show.pipeline.booked))}</td>`
    + `<td class="numeric">${escapeHTML(formatMetric(show.booking_velocity.bookings_per_week))}</td>`
    + `<td class="numeric">${escapeHTML(money(show.revenue.revenue_minor, show.revenue.currency))}</td>`
    + `<td class="numeric">${escapeHTML(formatMetric(show.summary.pending_clearances))}</td>`
    + `<td>${networkDrilldownButton(show.show_id, show.label)}</td></tr>`
  )).join('') || '<tr><td colspan="8" class="empty">No show is registered active in this tenant.</td></tr>';
  overlapRows.innerHTML = (networkPortfolio?.overlap || []).map(entry => (
    `<tr><th scope="row">${escapeHTML(entry.guest_name)}</th>`
    + `<td>${escapeHTML((entry.shows || []).map(show => show.label).join(' + '))}</td>`
    + `<td class="numeric">${escapeHTML(formatMetric(entry.show_count))}</td></tr>`
  )).join('') || '<tr><td colspan="3" class="empty">No guest appears on more than one show.</td></tr>';
  $('#network-receipt-list').innerHTML = networkReceipts.slice(0, 12).map(receipt => (
    `<li><time>${escapeHTML(new Date(receipt.created_at).toLocaleString())}</time>`
    + `<b>${escapeHTML(receipt.report_format)} · ${escapeHTML(receipt.actor_role)}</b>`
    + `<span class="sensitive-ref">${escapeHTML(receipt.document_checksum)}</span>`
    + `<span>${escapeHTML(formatMetric(receipt.show_count))} shows · `
    + `${escapeHTML(formatMetric(receipt.overlap_count))} shared guests</span></li>`
  )).join('') || '<li class="empty">No health report has been exported for this tenant.</li>';
}

async function hydrateNetworkPortfolio() {
  if (!capabilities.canViewNetwork) {
    networkPortfolio = null;
    networkReceipts = [];
    renderNetworkPortfolio();
    setNetworkStatus('Only a network operator may read the portfolio.', true);
    return;
  }
  const requestVersion = ++networkPortfolioRequestVersion;
  setNetworkStatus('Loading portfolio metrics, guest overlap, and export receipts…');
  try {
    const [portfolio, receipts] = await Promise.all([loadNetworkPortfolio(), loadNetworkReceipts()]);
    if (requestVersion !== networkPortfolioRequestVersion) return;
    networkPortfolio = portfolio;
    networkReceipts = receipts;
    renderNetworkPortfolio();
    const shared = portfolio.totals.overlapping_guests;
    setNetworkStatus(
      `Loaded ${portfolio.totals.shows} show${portfolio.totals.shows === 1 ? '' : 's'}, `
      + `${portfolio.totals.candidates} candidates, ${portfolio.totals.booked} booked, `
      + `and ${shared} guest${shared === 1 ? '' : 's'} on more than one show.`,
    );
  } catch (error) {
    if (requestVersion !== networkPortfolioRequestVersion) return;
    setNetworkStatus(error.message, true);
  }
}

function openShowDashboard(showId) {
  // Drilldown reuses the cockpit's own show scope: the shell already reloads
  // into a show when ?show= changes, so a portfolio row opens exactly the
  // dashboard the operator would reach from the switcher.
  const url = new URL(window.location.href);
  url.searchParams.set('show', showId);
  window.location.assign(url.toString());
}

async function exportNetworkReport() {
  if (!capabilities.canExportNetworkReport) {
    setNetworkStatus('This session cannot export the network health report.', true);
    return;
  }
  const format = $('#network-report-format')?.value || 'pdf';
  try {
    const blob = await loadNetworkReport(format);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `network-health.${format}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    networkReceipts = await loadNetworkReceipts();
    renderNetworkPortfolio();
    setNetworkStatus(`Exported the ${format.toUpperCase()} health report; the receipt is recorded below.`);
  } catch (error) {
    setNetworkStatus(error.message, true);
  }
}

async function hydrateActiveView() {
  if (!center) return;
  if (activeView === 'overview') {
    renderOverview();
    return;
  }
  if (activeView === 'register') {
    renderRegister();
    renderGuestRegister();
    await refreshGuestRegister();
    return;
  }
  if (activeView === 'revenue') {
    await refreshRevenue();
    return;
  }
  if (activeView === 'analytics') {
    await hydrateAnalytics();
    return;
  }
  if (activeView === 'publish') {
    await refreshPublish();
    return;
  }
  if (activeView === 'network') {
    await hydrateNetworkPortfolio();
    return;
  }
  const run = center.pilot_execution?.latest_run;
  pilotPlan = shouldRequestPilotPlan(context, run)
    ? await loadPilotPlan(activePartnershipId, run.id)
    : null;
  renderReadiness();
  renderOpportunities();
  renderPilotPlanner();
  await refreshClearances();
  await refreshOpportunityDetail();
}

async function activateView(view) {
  activeView = view;
  for (const name of VIEWS) $(`#${name}-view`)?.classList.toggle('hidden', name !== view);
  document.querySelectorAll('.view-tab').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  const [title, subtitle] = viewTitle(view);
  $('#page-title').textContent = title; $('#page-subtitle').textContent = subtitle;
  try {
    if (view === 'workbench') {
      await Promise.all([hydrateActiveView(), hydrateWorkbenchQueue()]);
    } else {
      await hydrateActiveView();
    }
  } catch (error) {
    setStatus(error.message, true);
  }
}

for (const category of categories) { elements.itemForm.elements.category.add(new Option(labels[category], category)); $('#register-category').add(new Option(labels[category], category)); }
for (const state of writableStates) elements.itemForm.elements.state.add(new Option(state.replaceAll('_', ' '), state));
for (const state of readableStates) $('#register-state').add(new Option(state.replaceAll('_', ' '), state));
elements.itemForm.elements.state.value = 'unknown';
subscribeDecisionCommitted(document, detail => {
  decisionReconcile = decisionReconcile
    .catch(() => {})
    .then(() => reconcileCommittedDecision(detail.opportunityId));
});
elements.opportunity.addEventListener('change', () => {
  networkRequestVersion += 1;
  networkMap = null;
  renderNetworkMap();
  $('#touchpoint-opportunity').value = elements.opportunity.value;
  refreshOpportunityDetail();
});
elements.networkForm.addEventListener('submit', async event => {
  event.preventDefault();
  const opportunity = selectedOpportunity();
  if (!opportunity?.show_id) {
    setStatus('A scoped opportunity is required before tracing relationship paths.', true);
    return;
  }
  const guest = event.currentTarget.elements.guest.value.trim();
  const depth = Number(event.currentTarget.elements.depth.value);
  const requestVersion = ++networkRequestVersion;
  const opportunityId = opportunity.id;
  const showId = opportunity.show_id;
  networkMap = null;
  renderNetworkMap();
  try {
    setStatus('Tracing scoped relationship paths…');
    const result = await loadNetworkMap(showId, guest, depth);
    const current = selectedOpportunity();
    if (
      requestVersion !== networkRequestVersion
      || current?.id !== opportunityId
      || current?.show_id !== showId
    ) return;
    networkMap = result;
    renderNetworkMap();
    setStatus(`Relationship map loaded with ${networkMap.target_paths.length} highlighted target paths.`);
  } catch (error) {
    if (requestVersion !== networkRequestVersion) return;
    networkMap = null;
    renderNetworkMap();
    setStatus(error.message, true);
  }
});
elements.receiptType.addEventListener('change', renderReceiptFields);
$('#btn-register-filter').addEventListener('click', renderRegister);
$('#btn-start-pilot').addEventListener('click', async event => {
  event.currentTarget.disabled = true;
  try {
    const policyId = center?.pilot_execution?.current_policy?.id;
    if (!policyId) throw new Error('No current Pilot policy is available.');
    pilotPlan = await startPilotRun(activePartnershipId, policyId);
    await refreshCenter();
    setStatus('Pilot run started. Inspect the ranked plan before recording a decision.');
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    event.currentTarget.disabled = false;
  }
});
$('#planner-decisions').addEventListener('click', async event => {
  const button = event.target.closest('[data-pilot-decision]');
  if (!button || !pilotPlan) return;
  button.disabled = true;
  try {
    const kind = button.dataset.pilotDecision;
    pilotPlan = await savePilotDecision(
      activePartnershipId,
      pilotPlan.run_id,
      plannerDecisionPayload(pilotPlan, kind),
    );
    await refreshCenter();
    setStatus(`Recorded ${kind.replaceAll('_', ' ')} against the current Pilot revision.`);
  } catch (error) {
    setStatus(error.message, true);
    button.disabled = false;
  }
});

elements.itemForm.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(elements.itemForm)); for (const key of ['external_reference', 'due_at']) if (!data[key]) delete data[key]; try { setStatus('Creating bounded register item…'); await savePartnershipItem(activePartnershipId, data); elements.itemForm.reset(); elements.itemForm.elements.state.value = 'unknown'; await refreshCenter(); } catch (error) { setStatus(error.message, true); } });
elements.resourceForm.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(elements.resourceForm)); if (!data.item_id) delete data.item_id; try { await saveResourceLink(activePartnershipId, data); elements.resourceForm.reset(); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });
elements.reviewForm.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(elements.reviewForm)); for (const key of ['decisions_count', 'coverage_met', 'coverage_total']) data[key] = Number(data[key]); data.occurred_at = asIso(data.occurred_at); if (!data.external_reference) delete data.external_reference; try { await savePartnershipReview(activePartnershipId, data); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });

document.querySelectorAll('.clearance-filter').forEach(button => button.addEventListener('click', () => {
  clearanceStatusFilter = button.dataset.clearanceFilter || '';
  document.querySelectorAll('.clearance-filter').forEach(other => other.classList.toggle('active', other === button));
  renderClearances();
}));
$('#clearance-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const showId = scopedShowIds()[0];
  if (!showId) { setStatus('A scoped show is required before recording a clearance.', true); return; }
  const payload = {
    episode_id: data.episode_id.trim(),
    type: data.type,
    rights_holder: data.rights_holder,
    license_terms: data.license_terms,
    cost: Number(data.cost || 0),
  };
  if (data.due_date) payload.due_date = data.due_date;
  try {
    await saveClearance(showId, payload);
    form.reset();
    await refreshClearances();
    setStatus('Rights clearance recorded as pending; the episode cannot publish until it is cleared.');
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#clearance-list').addEventListener('submit', async event => {
  const form = event.target.closest('.clearance-decision-form');
  if (!form) return;
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  try {
    await decideClearance(form.dataset.showId, form.dataset.clearanceId, {
      status: data.status,
      evidence_ref: data.evidence_ref.trim(),
    });
    await refreshClearances();
    setStatus(`Clearance recorded as ${data.status} against its evidence reference.`);
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#btn-clearance-report').addEventListener('click', async () => {
  const showId = scopedShowIds()[0];
  if (!showId) { setStatus('A scoped show is required before exporting a clearance report.', true); return; }
  const output = $('#clearance-report-output');
  try {
    output.value = await loadClearanceReport(showId, clearanceStatusFilter ? { status: clearanceStatusFilter } : {});
    output.classList.remove('hidden');
    setStatus('Clearance report rendered as CSV with opaque references only.');
  } catch (error) {
    setStatus(error.message, true);
  }
});

$('#btn-log-touchpoint').addEventListener('click', () => {
  const dialog = $('#touchpoint-dialog');
  const form = $('#touchpoint-form');
  form.elements.opportunity_id.value = elements.opportunity.value;
  form.elements.occurred_at.value = localDateTimeValue();
  dialog.showModal();
  form.elements.notes.focus();
});
$('#btn-cancel-touchpoint').addEventListener('click', () => $('#touchpoint-dialog').close());
$('#touchpoint-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const opportunity = opportunities.find(item => item.id === data.opportunity_id);
  if (!opportunity) { setStatus('Select a current guest before logging a chat.', true); return; }
  const payload = {
    opportunity_id: opportunity.id,
    guest_id: opportunity.source_key || opportunity.id,
    partnership_id: activePartnershipId,
    channel: data.channel,
    notes: data.notes,
    occurred_at: asIso(data.occurred_at),
  };
  try {
    await saveTouchpoint(opportunity.show_id, payload);
    elements.opportunity.value = opportunity.id;
    form.reset();
    $('#touchpoint-dialog').close();
    await refreshCenter();
    setStatus('Informal touchpoint encrypted and added to both timelines; workflow state is unchanged.');
  } catch (error) {
    setStatus(error.message, true);
  }
});

elements.sections.addEventListener('submit', async event => { const form = event.target.closest('.item-edit-form'); if (!form) return; event.preventDefault(); const data = Object.fromEntries(new FormData(form)); delete data.successor_item_id; data.expected_revision = Number(data.expected_revision); for (const key of ['external_reference', 'due_at']) if (!data[key]) delete data[key]; try { await updatePartnershipItem(activePartnershipId, form.dataset.itemId, data); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });
elements.sections.addEventListener('click', async event => { const button = event.target.closest('[data-supersede]'); if (!button) return; const form = button.closest('.item-edit-form'); const successor = form.elements.successor_item_id.value.trim(); if (!successor) { setStatus('A successor item id is required before superseding.', true); return; } try { await supersedePartnershipItem(activePartnershipId, form.dataset.itemId, { expected_revision: Number(form.elements.expected_revision.value), successor_item_id: successor }); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });

$('#slot-form').addEventListener('submit', async event => { event.preventDefault(); try { await selectPilotCandidate(activePartnershipId, Number(event.currentTarget.elements.slot.value), elements.opportunity.value); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });
$('#btn-preview-draft').addEventListener('click', async () => { try { const draft = await previewDraft(elements.opportunity.value); const output = $('#draft-preview-output'); const reviewed = reviewedDraftExists(); output.value = `${draft.subject}\n\n${draft.body}`; output.classList.remove('hidden'); $('#btn-review-draft').classList.toggle('hidden', reviewed); $('#btn-copy-draft').classList.toggle('hidden', !reviewed); setStatus(reviewed ? 'Preview rendered; prior human-reviewed metadata permits copy.' : 'Preview rendered transiently; approve safe review metadata before copy.'); } catch (error) { setStatus(error.message, true); } });
$('#btn-review-draft').addEventListener('click', async () => { try { const draft = await saveReviewedDraft(elements.opportunity.value); const output = $('#draft-preview-output'); output.value = `${draft.subject}\n\n${draft.body}`; $('#btn-review-draft').classList.add('hidden'); $('#btn-copy-draft').classList.remove('hidden'); await refreshOpportunityDetail(); output.value = `${draft.subject}\n\n${draft.body}`; output.classList.remove('hidden'); $('#btn-copy-draft').classList.remove('hidden'); setStatus('Human-reviewed metadata persisted; the message body remains external and copy is enabled.'); } catch (error) { setStatus(error.message, true); } });
$('#btn-copy-draft').addEventListener('click', async () => {
  try {
    const value = clipboardText($('#draft-preview-output').value, {
      completed: capabilities.completed,
      canDraft: capabilities.canDraft,
      reviewed: reviewedDraftExists(),
    });
    await navigator.clipboard.writeText(value);
    setStatus('Preview copied. External sending remains a human action.');
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#studio-form').addEventListener('submit', async event => { event.preventDefault(); try { await saveStudioRoute(elements.opportunity.value, Object.fromEntries(new FormData(event.currentTarget))); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });
$('#receipt-form').addEventListener('submit', async event => {
  event.preventDefault();
  const raw = Object.fromEntries(new FormData(event.currentTarget));
  for (const field of receiptFields[raw.receipt_type] || []) {
    const element = event.currentTarget.elements[field.name];
    raw[field.name] = field.type === 'checkbox' ? element.checked : element.value;
  }
  const payload = receiptPayload(raw, receiptFields, asIso);
  try {
    await saveReceipt(elements.opportunity.value, payload);
    await refreshCenter();
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#brief-form').addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); const payload = { research_claims: [{ claim: data.claim, evidence_source: data.evidence_source, verified: true }], segments: [{ title: data.segment_title, objective: data.segment_objective }] }; try { await saveBrief(elements.opportunity.value, payload); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });
$('#asset-form').addEventListener('submit', async event => { event.preventDefault(); const root = event.currentTarget.elements.custody_root.value.replace(/\/$/, ''); const kinds = ['environmental_master', 'host_singles', 'safety_microphone', 'backup_recorder', 'room_tone', 'slate']; try { await saveAssets(elements.opportunity.value, { assets: kinds.map(kind => ({ kind, custody_target: `${root}/${kind}` })) }); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });
$('#guest-history-filter').addEventListener('submit', async event => { event.preventDefault(); $('#guest-history-opportunity').value = $('#guest-history-guest').value; await refreshGuestRegister(); });
$('#guest-history-form').addEventListener('submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  if (!data.notes.trim()) delete data.notes;
  const opportunity = opportunities.find(item => item.id === data.opportunity_id);
  if (!opportunity) { setStatus('Choose a candidate before recording a prior season.', true); return; }
  try {
    await saveGuestHistory(opportunity.show_id, data);
    event.currentTarget.reset();
    $('#guest-history-guest').value = opportunity.id;
    await refreshGuestRegister();
    setStatus('Cross-season interaction recorded. No workflow state moved.');
  } catch (error) { setStatus(error.message, true); }
});
$('#commitment-form').addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.due_at = asIso(data.due_at); try { await saveCommitment(elements.opportunity.value, data); await refreshCenter(); } catch (error) { setStatus(error.message, true); } });

$('#btn-revenue-refresh').addEventListener('click', async () => {
  try {
    await refreshRevenue();
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#revenue-episode').addEventListener('change', async () => {
  try {
    await refreshRevenue();
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#btn-add-sponsor').addEventListener('click', () => {
  const form = $('#sponsor-form');
  form.reset();
  $('#sponsor-dialog').showModal();
  form.elements.name.focus();
});
$('#btn-cancel-sponsor').addEventListener('click', () => $('#sponsor-dialog').close());
$('#sponsor-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  if (!data.category) delete data.category;
  try {
    await saveSponsor(activeShowId(), data);
    form.reset();
    $('#sponsor-dialog').close();
    await refreshRevenue();
    setStatus('Sponsor registered with opaque contact and terms custody references.');
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#btn-export-revenue').addEventListener('click', async () => {
  try {
    const csv = await exportRevenueCsv(activeShowId(), revenueFilters());
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'hospes-revenue.csv';
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    setStatus('Accounting CSV exported: Episode, Sponsor, Slot, Rate, Date.');
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#ad-slot-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    const slots = ['pre', 'mid', 'post']
      .filter(slotType => data[`offer_${slotType}`] !== 'none')
      .map(slotType => ({
        slot_type: slotType,
        rate_minor: minorUnits(data[`rate_${slotType}`], `${slotType}-roll rate`),
        committed: data[`offer_${slotType}`] === 'committed',
      }));
    await saveAdSlots(activeShowId(), { episode_id: data.episode_id, slots });
    $('#revenue-episode').value = data.episode_id;
    await refreshRevenue();
    setStatus(`Declared ${slots.length} ad slot(s) for ${data.episode_id}.`);
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#sponsorship-form').addEventListener('submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await saveSlotAssignment(activeShowId(), data);
    $('#revenue-episode').value = data.episode_id;
    await refreshRevenue();
    setStatus(`Recorded ${data.slot_type}-roll as ${data.status}. HOSPES never invoices or signs.`);
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#sponsor-claim-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    await saveSponsorClaim(activeShowId(), data.sponsor_id, {
      claim: data.claim,
      source_url: data.source_url,
      verified_date: data.verified_date,
    });
    form.reset();
    await refreshRevenue();
    setStatus('Claim recorded. It stays unapproved, and blocks publication, until an owner approves it.');
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#sponsor-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-claim-approve]');
  if (!button) return;
  try {
    await approveSponsorClaim(
      activeShowId(),
      button.dataset.claimSponsor,
      button.dataset.claimApprove,
      button.dataset.claimState === 'approve',
    );
    await refreshRevenue();
    setStatus('Claim approval recorded with an attributable receipt.');
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#slot-inventory-rows').addEventListener('click', async event => {
  const button = event.target.closest('[data-release-slot]');
  if (!button) return;
  try {
    await releaseSlotAssignment(activeShowId(), {
      episode_id: button.dataset.releaseEpisode,
      slot_type: button.dataset.releaseSlot,
    });
    await refreshRevenue();
    setStatus('Slot released to available inventory; the release is receipted.');
  } catch (error) {
    setStatus(error.message, true);
  }
});

$('#analytics-window').addEventListener('change', () => { void hydrateAnalytics(); });
$('#btn-analytics-refresh').addEventListener('click', () => { void hydrateAnalytics(); });
$('#btn-analytics-export').addEventListener('click', () => { void exportAnalytics(); });
$('#btn-network-refresh').addEventListener('click', () => { void hydrateNetworkPortfolio(); });
$('#btn-network-export').addEventListener('click', () => { void exportNetworkReport(); });
$('#network-table').addEventListener('click', event => {
  const button = event.target.closest('[data-network-show]');
  if (button) openShowDashboard(button.dataset.networkShow);
});

$('#btn-publish-refresh').addEventListener('click', async () => {
  try {
    await refreshPublish();
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#publish-episode').addEventListener('change', async () => {
  try {
    await refreshPublish();
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#btn-verify-adapters').addEventListener('click', async () => {
  try {
    const receipts = await verifyDistributionAdapters(activeShowId());
    await refreshPublish();
    setStatus(`Recorded ${receipts.length} distribution provider receipt(s); an unconfigured adapter stays visible.`);
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#rss-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    const metadata = { title: data.title };
    for (const [key, value] of [['description', data.description], ['audio_url', data.audio_url], ['duration', data.duration]]) {
      if (String(value || '').trim()) metadata[key] = String(value).trim();
    }
    const chapters = parseChapters(data.chapters);
    if (chapters.length) metadata.chapters = chapters;
    const rendered = await previewDistribution(activeShowId(), { platform: 'rss', metadata });
    setPreview(`rss · ${rendered.completeness.complete ? 'complete' : `missing ${rendered.completeness.missing.join(', ')}`}`, rendered.body);
    await saveDistributionDraft(activeShowId(), {
      episode_id: data.episode_id.trim(),
      platform: 'rss',
      metadata,
      idempotency_key: `${data.episode_id.trim()}:rss`,
    });
    $('#publish-episode').value = data.episode_id.trim();
    await refreshPublish();
    setStatus('RSS item rendered and drafted. Publishing still needs a human authorization receipt.');
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#youtube-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    const metadata = { title: data.title };
    for (const [key, value] of [['description', data.description], ['playlist', data.playlist], ['thumbnail_ref', data.thumbnail_ref]]) {
      if (String(value || '').trim()) metadata[key] = String(value).trim();
    }
    const tags = parseList(data.tags);
    if (tags.length) metadata.tags = tags;
    const chapters = parseChapters(data.chapters);
    if (chapters.length) metadata.chapters = chapters;
    const rendered = await previewDistribution(activeShowId(), { platform: 'youtube', metadata });
    setPreview(`youtube · ${rendered.completeness.complete ? 'complete' : `missing ${rendered.completeness.missing.join(', ')}`}`, rendered.body);
    await saveDistributionDraft(activeShowId(), {
      episode_id: data.episode_id.trim(),
      platform: 'youtube',
      metadata,
      idempotency_key: `${data.episode_id.trim()}:youtube`,
    });
    $('#publish-episode').value = data.episode_id.trim();
    await refreshPublish();
    setStatus('Video metadata drafted. The thumbnail stays an opaque reference to its external owner.');
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#clip-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    const payload = {
      episode_id: data.episode_id.trim(),
      target_platform: data.target_platform,
      start_seconds: Number(data.start_seconds),
      end_seconds: Number(data.end_seconds),
      caption_ref: data.caption_ref.trim(),
      idempotency_key: `${data.episode_id.trim()}:${data.target_platform}:${data.start_seconds}-${data.end_seconds}`,
    };
    const hashtags = parseList(data.hashtags);
    if (hashtags.length) payload.hashtags = hashtags;
    await queueClip(activeShowId(), payload);
    $('#publish-episode').value = payload.episode_id;
    await refreshPublish();
    setStatus(`Queued a ${payload.end_seconds - payload.start_seconds}s ${data.target_platform} clip. Uploading it stays a human action.`);
  } catch (error) {
    setStatus(error.message, true);
  }
});
$('#publish-cards').addEventListener('submit', async event => {
  const form = event.target.closest('.publish-action-form');
  if (!form) return;
  event.preventDefault();
  const showId = activeShowId();
  const distributionId = form.dataset.distributionId;
  const data = Object.fromEntries(new FormData(form));
  try {
    if (form.dataset.publishAction === 'preview') {
      const card = publishBoard.episodes
        .flatMap(episode => [...episode.platforms, ...episode.clips])
        .find(item => item.distribution_id === distributionId);
      const rendered = await previewDistribution(showId, { platform: card.platform, metadata: card.metadata });
      setPreview(`${card.platform} · ${card.status}`, rendered.body);
      setStatus('Package rendered. A preview reads nothing and writes nothing.');
      return;
    }
    if (form.dataset.publishAction === 'authorize') {
      await authorizeDistribution(showId, distributionId, {
        authorization_ref: data.authorization_ref.trim(),
        idempotency_key: `${distributionId}:publish`,
      });
      await refreshPublish();
      setStatus('Human authorization receipt recorded. HOSPES still publishes nothing.');
      return;
    }
    if (form.dataset.publishAction === 'schedule') {
      await scheduleDistribution(showId, distributionId, { scheduled_at: asIso(data.scheduled_at) });
      await refreshPublish();
      setStatus('Publication scheduled against its existing authorization receipt.');
      return;
    }
    if (form.dataset.publishAction === 'publish') {
      await markDistributionPublished(showId, distributionId, { external_id_ref: data.external_id_ref.trim() });
      await refreshPublish();
      setStatus('Publication recorded with an immutable delivery receipt; the external id can never be rewritten.');
      return;
    }
    await recordDistributionFailure(showId, distributionId, { error_ref: data.error_ref.trim() });
    await refreshPublish();
    setStatus('Failed attempt recorded. The retry is numbered and keeps its own evidence row.');
  } catch (error) {
    setStatus(error.message, true);
  }
});

export async function startPartnershipWorkspace(view = 'workbench', partnershipId = '') {
  activePartnershipId = partnershipId;
  renderReceiptFields();
  await loadRegistry();
  await activateView(view);
}

export async function activatePartnershipWorkspace(view) {
  await activateView(view);
}

export async function reloadPartnershipWorkspace() {
  await loadRegistry();
}

export async function selectPartnershipWorkspace(partnershipId) {
  activePartnershipId = partnershipId;
  elements.select.value = partnershipId;
  await refreshCenter();
}
