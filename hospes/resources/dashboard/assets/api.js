const API_ROOT = '/operator/api';

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const match = document.cookie.split(';').map(item => item.trim())
    .find(item => item.startsWith(prefix));
  return match ? decodeURIComponent(match.slice(prefix.length)) : '';
}

async function send(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const activeShow = document.querySelector('meta[name="hospes-active-show"]')?.content || '';
  if (activeShow) headers.set('X-Session-Show', activeShow);
  if (options.body) headers.set('Content-Type', 'application/json');
  const method = String(options.method || 'GET').toUpperCase();
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = cookieValue('hospes_csrf');
    if (!csrf) throw new Error('Operator CSRF session is unavailable; sign in again.');
    headers.set('X-Hospes-CSRF', csrf);
  }
  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    headers,
    credentials: 'same-origin',
  });
  if (response.status === 401) {
    window.location.assign('/operator/login');
    throw new Error('Operator session expired.');
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // Keep the status-only message when an intermediary returned non-JSON.
    }
    throw new Error(detail);
  }
  return response;
}

async function request(path, options = {}) {
  const response = await send(path, options);
  return response.status === 204 ? null : response.json();
}

async function requestText(path, options = {}) {
  const response = await send(path, options);
  return response.status === 204 ? '' : response.text();
}

function queryString(filters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  return query.size ? `?${query.toString()}` : '';
}

export function loadRevenue(showId, filters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : '';
  return request(`/shows/${encodeURIComponent(showId)}/revenue${suffix}`);
}

export function loadAdSlots(showId, filters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : '';
  return request(`/shows/${encodeURIComponent(showId)}/ad-slots${suffix}`);
}

export function loadSponsors(showId) {
  return request(`/shows/${encodeURIComponent(showId)}/sponsors`);
}

export function saveSponsor(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/sponsors`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function saveSponsorClaim(showId, sponsorId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/sponsors/${encodeURIComponent(sponsorId)}/claims`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function approveSponsorClaim(showId, sponsorId, claimId, approved = true) {
  return request(`/shows/${encodeURIComponent(showId)}/sponsors/${encodeURIComponent(sponsorId)}/claims/${encodeURIComponent(claimId)}/approval`, {
    method: 'POST',
    body: JSON.stringify({ approved }),
  });
}

export function saveAdSlots(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/ad-slots`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function saveSlotAssignment(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/ad-slots/allocations`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function releaseSlotAssignment(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/ad-slots/releases`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function exportRevenueCsv(showId, filters = {}) {
  const query = new URLSearchParams({ format: 'csv' });
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  return requestText(`/shows/${encodeURIComponent(showId)}/revenue?${query.toString()}`);
}

export function loadPublishBoard(showId, filters = {}) {
  return request(`/shows/${encodeURIComponent(showId)}/distribution-board${queryString(filters)}`);
}

export function previewDistribution(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/distributions/preview`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function saveDistributionDraft(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/distributions`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function queueClip(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/clips`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function authorizeDistribution(showId, distributionId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/distributions/${encodeURIComponent(distributionId)}/authorize`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function scheduleDistribution(showId, distributionId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/distributions/${encodeURIComponent(distributionId)}/schedule`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function markDistributionPublished(showId, distributionId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/distributions/${encodeURIComponent(distributionId)}/publication-receipt`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function recordDistributionFailure(showId, distributionId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/distributions/${encodeURIComponent(distributionId)}/failures`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function loadDistributionReceipts(showId, distributionId) {
  return request(`/shows/${encodeURIComponent(showId)}/distributions/${encodeURIComponent(distributionId)}/receipts`);
}

export function verifyDistributionAdapters(showId) {
  return request(`/shows/${encodeURIComponent(showId)}/distribution-adapters/verification`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function loadApprovalQueue(filters = {}) {
  const query = new URLSearchParams();
  if (filters.state) query.set('state', filters.state);
  if (filters.owner) query.set('owner', filters.owner);
  const suffix = query.size ? `?${query.toString()}` : '';
  // The focused approval queue owns the default review. A canonical state
  // filter deliberately switches to the broader filtered opportunity list.
  const endpoint = filters.state ? '/opportunities' : '/approval-queue';
  const payload = await request(`${endpoint}${suffix}`);
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.items)) return payload.items;
  if (payload && Array.isArray(payload.opportunities)) return payload.opportunities;
  throw new Error('Approval queue returned an unsupported response shape.');
}

export function recordDecision(opportunityId, action, note) {
  const payload = { action };
  const normalizedNote = (note || '').trim();
  if (normalizedNote) payload.note = normalizedNote;
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/decisions`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function loadPartnerships() {
  return request('/partnerships');
}

export function loadPartnershipCommandCenter(partnershipId, surface = '') {
  const query = surface ? `?surface=${encodeURIComponent(surface)}` : '';
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/command-center${query}`);
}

export function loadOperatorContext() {
  return request('/operator-context');
}

export function loadOpportunities() {
  return request('/opportunities');
}

export function loadOpportunityDetail(opportunityId) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}`);
}

export function loadContactRoster(opportunityId) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/contact-roster`);
}

export function loadTouchpoints(showId, filters = {}) {
  return request(`/shows/${encodeURIComponent(showId)}/touchpoints${queryString(filters)}`);
}

export function loadClearances(showId, filters = {}) {
  return request(`/shows/${encodeURIComponent(showId)}/clearances${queryString(filters)}`);
}

export function loadClearanceBadges(showId, filters = {}) {
  return request(`/shows/${encodeURIComponent(showId)}/clearance-badges${queryString(filters)}`);
}

export function loadClearanceReport(showId, filters = {}) {
  return requestText(`/shows/${encodeURIComponent(showId)}/clearance-report${queryString(filters)}`);
}

export function saveClearance(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/clearances`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function decideClearance(showId, clearanceId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/clearances/${encodeURIComponent(clearanceId)}/decision`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function loadGuestHistory(opportunityId, filters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : '';
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/guest-history${suffix}`);
}

export function loadShowGuestHistory(showId, filters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : '';
  return request(`/shows/${encodeURIComponent(showId)}/guest-history${suffix}`);
}

export function saveGuestHistory(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/guest-history`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function setDoNotContact(opportunityId, doNotContact, reasonRef) {
  const payload = { do_not_contact: doNotContact };
  const reference = (reasonRef || '').trim();
  if (reference) payload.reason_ref = reference;
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/do-not-contact`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function loadAnalyticsTrends(showId, limit = 12) {
  const query = new URLSearchParams({ limit: String(limit) });
  return request(`/shows/${encodeURIComponent(showId)}/analytics/trends?${query.toString()}`);
}

export function loadAnalyticsReceipts(showId) {
  return request(`/shows/${encodeURIComponent(showId)}/analytics/receipts`);
}

export function loadAnalyticsExport(showId) {
  return requestText(`/shows/${encodeURIComponent(showId)}/analytics/export.csv`);
}

export function loadResearchQueue(showId, filters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : '';
  return request(`/shows/${encodeURIComponent(showId)}/research${suffix}`);
}

export function loadResearchBrief(showId, jobId) {
  return request(`/shows/${encodeURIComponent(showId)}/research/${encodeURIComponent(jobId)}`);
}

export function startResearch(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/research`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function runResearch(showId, jobId) {
  return request(`/shows/${encodeURIComponent(showId)}/research/${encodeURIComponent(jobId)}/run`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export function annotateResearch(showId, jobId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/research/${encodeURIComponent(jobId)}/annotate`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function reviewResearch(showId, jobId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/research/${encodeURIComponent(jobId)}/review`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function lockResearch(showId, jobId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/research/${encodeURIComponent(jobId)}/lock`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

// The network portfolio is tenant-scoped, so none of these carry a show id:
// the server derives scope from the authenticated identity and refuses any
// role but network_operator.
export function loadNetworkPortfolio() {
  return request('/network/portfolio');
}

export function loadNetworkShow(showId) {
  return request(`/network/shows/${encodeURIComponent(showId)}`);
}

export function loadNetworkReceipts(limit = 100) {
  return request(`/network/receipts?limit=${encodeURIComponent(limit)}`);
}

export async function loadNetworkReport(format = 'pdf') {
  const response = await send(`/network/report.${encodeURIComponent(format)}`);
  return response.blob();
}

export function loadNetworkMap(showId, guest, depth = 2) {
  const query = new URLSearchParams({ guest, depth: String(depth) });
  return request(`/shows/${encodeURIComponent(showId)}/network-map?${query.toString()}`);
}

export function saveTouchpoint(showId, payload) {
  return request(`/shows/${encodeURIComponent(showId)}/touchpoints`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function savePartnershipItem(partnershipId, payload) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/items`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function updatePartnershipItem(partnershipId, itemId, payload) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/items/${encodeURIComponent(itemId)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function supersedePartnershipItem(partnershipId, itemId, payload) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/items/${encodeURIComponent(itemId)}/supersede`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function saveResourceLink(partnershipId, payload) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/resources`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function savePartnershipReview(partnershipId, payload) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/reviews`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function selectPilotCandidate(partnershipId, slot, opportunityId) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/pilot-slots/${slot}`, {
    method: 'PUT',
    body: JSON.stringify({ opportunity_id: opportunityId }),
  });
}

export function startPilotRun(partnershipId, policyId) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/pilot-runs`, {
    method: 'POST',
    body: JSON.stringify({ policy_id: policyId }),
  });
}

export function loadPilotPlan(partnershipId, runId) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/pilot-runs/${encodeURIComponent(runId)}/plan`);
}

export function savePilotDecision(partnershipId, runId, payload) {
  return request(`/partnerships/${encodeURIComponent(partnershipId)}/pilot-runs/${encodeURIComponent(runId)}/decisions`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function previewDraft(opportunityId) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/draft-preview`, {
    method: 'POST',
    body: JSON.stringify({ kind: 'invitation' }),
  });
}

export function saveReviewedDraft(opportunityId) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/correspondence-drafts`, {
    method: 'POST',
    body: JSON.stringify({ kind: 'invitation' }),
  });
}

export function saveStudioRoute(opportunityId, payload) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/studio-routing`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function saveReceipt(opportunityId, payload) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/receipts`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function saveBrief(opportunityId, payload) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/briefs`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function saveAssets(opportunityId, payload) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/asset-packages`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function saveCommitment(opportunityId, payload) {
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/commitments`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function loadNotificationSummary(showId) {
  return request(`/shows/${encodeURIComponent(showId)}/notifications/summary`);
}

export function loadNotifications(showId, filters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : '';
  return request(`/shows/${encodeURIComponent(showId)}/notifications${suffix}`);
}

export function markNotificationRead(showId, notificationId) {
  return request(
    `/shows/${encodeURIComponent(showId)}/notifications/${encodeURIComponent(notificationId)}/read`,
    { method: 'POST' },
  );
}

export function markAllNotificationsRead(showId, notificationType = '') {
  const payload = {};
  if (notificationType) payload.notification_type = notificationType;
  return request(`/shows/${encodeURIComponent(showId)}/notifications/read-all`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function loadMyQueue(showId, filters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === true) query.set(key, 'true');
    else if (value !== undefined && value !== null && value !== '' && value !== false) {
      query.set(key, String(value));
    }
  }
  const suffix = query.size ? `?${query.toString()}` : '';
  return request(`/shows/${encodeURIComponent(showId)}/my-queue${suffix}`);
}

export function completeQueueTask(showId, taskId) {
  return request(
    `/shows/${encodeURIComponent(showId)}/queue-tasks/${encodeURIComponent(taskId)}/complete`,
    { method: 'POST' },
  );
}

export function loadPortalStatus() {
  return request('/portal-status');
}

export function sendPortalLink(opportunityId, offeredDates) {
  const payload = {};
  if (Array.isArray(offeredDates) && offeredDates.length) payload.offered_dates = offeredDates;
  return request(`/opportunities/${encodeURIComponent(opportunityId)}/portal-link`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
