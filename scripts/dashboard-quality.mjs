import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

import { launch as launchChrome } from 'chrome-launcher';
import lighthouse from 'lighthouse';
import { chromium } from 'playwright';

const baseUrl = process.env.HOSPES_DASHBOARD_URL || 'http://127.0.0.1:8765';
const token = process.env.HOSPES_OPERATOR_TOKEN; // allow-secret: environment lookup only
const artifactDir = path.resolve(process.env.HOSPES_DASHBOARD_ARTIFACT_DIR || 'artifacts/dashboard-quality');
const responsiveViewports = [
  { width: 390, height: 844, label: '390x844' },
  { width: 1280, height: 800, label: '1280x800' },
  { width: 1440, height: 900, label: '1440x900' },
  { width: 1920, height: 1080, label: '1920x1080' },
];
const UNRESOLVED_TEMPLATE = /__HOSPES_[A-Z0-9_]+__/;
const SYNTHETIC_CHIP_TEXT = /synthetic|live sqlite/i;
if (!token) throw new Error('HOSPES_OPERATOR_TOKEN is required');
await fs.mkdir(artifactDir, { recursive: true });

async function collectLayoutAudit(page) {
  return page.evaluate(() => {
    const visible = element => {
      const style = getComputedStyle(element);
      return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
    };
    const tabs = document.querySelector('.view-tabs');
    const chip = document.querySelector('#mode-badge');
    const header = document.querySelector('.header-row');
    const textareas = [...document.querySelectorAll('textarea')]
      .filter(visible)
      .map(element => element.getBoundingClientRect().height);
    return {
      viewport: [window.innerWidth, window.innerHeight],
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      tabsOverflowX: getComputedStyle(tabs).overflowX,
      tabsScrollable: tabs.scrollWidth > tabs.clientWidth,
      chipShown: chip && visible(chip),
      chipText: chip?.textContent?.trim() || '',
      headerPosition: getComputedStyle(header).position,
      headerTop: getComputedStyle(header).top,
      textareaMinimum: textareas.length ? Math.min(...textareas) : Infinity,
    };
  });
}

const browser = await chromium.launch({ headless: true });
let cookieHeader = '';
const qualitySignals = {
  consoleErrors: [],
  pageErrors: [],
  requestFailures: [],
  responseFailures: [],
};
try {
  const context = await browser.newContext({ viewport: { width: 375, height: 667 } });
  const page = await context.newPage();
  page.on('console', message => {
    if (message.type() !== 'error') return;
    qualitySignals.consoleErrors.push({
      text: message.text(),
      source: message.location(),
    });
  });
  page.on('pageerror', error => {
    qualitySignals.pageErrors.push({
      text: String(error),
    });
  });
  page.on('requestfailed', request => {
    if (request.resourceType() === 'script') {
      qualitySignals.requestFailures.push({
        url: request.url(),
        failure: request.failure()?.errorText || 'unknown',
        type: request.resourceType(),
      });
    }
  });
  page.on('response', response => {
    const request = response.request();
    if (request.resourceType() === 'script' && !response.ok()) {
      qualitySignals.responseFailures.push({
        url: response.url(),
        status: response.status(),
        type: request.resourceType(),
      });
    }
  });
  const rosterRef = 'private-field://00000000-0000-4000-8000-000000000020';
  const publicRoster = {
    roster_id: '00000000-0000-4000-8000-000000000021',
    role: 'publicist',
    name_ref: rosterRef,
    route_ref: rosterRef,
    notes_ref: rosterRef,
    provenance_ref: 'roster://synthetic/browser-20',
    verified_at: '2026-08-10T12:00:00+00:00',
    usable: true,
    preferred: true,
    permission_status: 'permitted',
  };
  const privateRoster = {
    ...publicRoster,
    name: '<Synthetic Publicist>',
    email: 'publicist@example.test',
    phone: null,
    notes: 'Synthetic browser fixture.',
  };
  const touchpoints = [];
  let staleNetworkAudit = {};
  let logoutBoundaryAudit = {};
  await page.route('**/operator/api/operator-context', async route => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.private_field_custody_configured = true;
    await route.fulfill({ response, json: payload });
  });
  await page.route('**/operator/api/partnerships/*/command-center', async route => {
    const response = await route.fetch();
    const payload = await response.json();
    if (touchpoints[0]) {
      payload.recent_events.unshift({
        id: 'synthetic-touchpoint-audit-21',
        event_type: 'touchpoint.recorded',
        actor_id: touchpoints[0].initiator,
        actor_role: touchpoints[0].initiator_role,
        details: { touchpoint_id: touchpoints[0].touchpoint_id },
        created_at: touchpoints[0].created_at,
      });
    }
    await route.fulfill({ response, json: payload });
  });
  await page.route('**/operator/api/shows/*/touchpoints*', async route => {
    if (route.request().method() === 'POST') {
      const payload = route.request().postDataJSON();
      const item = {
        touchpoint_id: '00000000-0000-4000-8000-000000000121',
        tenant_id: 'synthetic',
        show_id: 'field_show',
        guest_id: payload.guest_id,
        guest_name: '<Synthetic Touchpoint Guest>',
        opportunity_id: payload.opportunity_id,
        partnership_id: payload.partnership_id,
        kind: 'informal_touchpoint',
        channel: payload.channel,
        notes_ref: 'private-field://00000000-0000-4000-8000-000000000122',
        notes: payload.notes,
        notes_available: true,
        occurred_at: payload.occurred_at,
        initiator: 'synthetic-relationship-owner',
        initiator_role: 'relationship_owner',
        created_at: payload.occurred_at,
      };
      touchpoints.splice(0, touchpoints.length, item);
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(item) });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      headers: { 'Cache-Control': 'no-store, private' },
      body: JSON.stringify(touchpoints),
    });
  });
  await page.route('**/operator/api/shows/*/network-map*', async route => {
    const requestUrl = new URL(route.request().url());
    const requestedGuest = requestUrl.searchParams.get('guest');
    if (
      !['Theo Von', 'Slow Guest'].includes(requestedGuest)
      || requestUrl.searchParams.get('depth') !== '2'
    ) {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'unexpected scoped relationship-map request' }),
      });
      return;
    }
    if (requestedGuest === 'Slow Guest') {
      await new Promise(resolve => setTimeout(resolve, 150));
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      headers: { 'Cache-Control': 'no-store, private' },
      body: JSON.stringify({
        schema_version: 1,
        tenant_id: 'synthetic',
        show_id: 'field_show',
        root: 'theo-von',
        depth: 2,
        nodes: [
          {
            id: 'theo-von',
            label: 'Theo Von',
            visibility: 'public',
            distance: 0,
            relationship_class: null,
            target_candidate: false,
            on_target_path: true,
          },
          {
            id: 'synthetic-c2',
            label: '<Synthetic C2>',
            visibility: 'public',
            distance: 1,
            relationship_class: 'C2',
            target_candidate: true,
            on_target_path: true,
          },
        ],
        edges: [
          {
            id: 'edge-synthetic-issue-22',
            tenant_id: 'synthetic',
            show_id: 'field_show',
            source_guest_id: 'theo-von',
            target_guest_id: 'synthetic-c2',
            source_label: 'Theo Von',
            target_label: '<Synthetic C2>',
            edge_type: 'introduced_by',
            relationship_class: 'C2',
            relationship_strength: 'medium',
            provenance_ref: 'owner://synthetic/issue-22',
            source_kind: 'manual',
            distance: 1,
            on_target_path: true,
          },
        ],
        target_paths: [
          {
            target_id: 'synthetic-c2',
            target_label: '<Synthetic C2>',
            node_ids: ['theo-von', 'synthetic-c2'],
            edge_ids: ['edge-synthetic-issue-22'],
          },
        ],
        summary: {
          node_count: 2,
          edge_count: 1,
          reachable_target_count: 1,
          source_counts: { manual: 1 },
        },
      }),
    });
  });
  await page.route('**/operator/api/approval-queue*', async route => {
    const response = await route.fetch();
    const payload = await response.json();
    if (Array.isArray(payload) && payload[0]) {
      payload[0].contact_roster_refs = [publicRoster];
      payload[0].contact_roster = [privateRoster];
    }
    await route.fulfill({ response, json: payload });
  });
  await page.route('**/operator/api/opportunities', async route => {
    const response = await route.fetch();
    const payload = await response.json();
    if (Array.isArray(payload) && payload[0]) {
      payload[0].status = 'APPROVED';
      payload[0].disposition = 'APPROVED';
      payload[0].relationship_class = 'C2';
    }
    await route.fulfill({ response, json: payload });
  });
  await page.route(/\/operator\/api\/opportunities\/[^/?]+\/contact-roster$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    headers: { 'Cache-Control': 'no-store, private' },
    body: JSON.stringify({
      items: [privateRoster],
      invitation_prefill: {
        role: 'publicist',
        name: privateRoster.name,
        route_kind: 'email',
        route_value: privateRoster.email,
        route_ref: rosterRef,
        provenance_ref: privateRoster.provenance_ref,
        verified_at: privateRoster.verified_at,
      },
    }),
  }));
  await page.route(/\/operator\/api\/opportunities\/[^/?]+$/, async route => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.contact_roster_refs = [publicRoster];
    await route.fulfill({ response, json: payload });
  });
  await page.goto(`${baseUrl}/operator/login`, { waitUntil: 'networkidle' });
  const loginPageAudit = await page.evaluate(() => ({
    hasRuntimeWarning: document.body.hasAttribute('data-runtime-warning'),
    hasLoginWarningBanner: !!document.querySelector('[data-runtime-warning]'),
    modeChipText: document.querySelector('#mode-badge')?.textContent?.trim() || '',
    bootstrapText: document.querySelector('#bootstrap-status')?.textContent?.trim() || '',
  }));
  if (loginPageAudit.hasRuntimeWarning) throw new Error('login page body has data-runtime-warning');
  if (loginPageAudit.hasLoginWarningBanner) throw new Error('login page contains hidden runtime warning banners');
  if (!/synthetic/i.test(loginPageAudit.modeChipText)) throw new Error(`login mode chip not synthetic: ${loginPageAudit.modeChipText}`);
  if (/Nothing is sent to a guest/.test(loginPageAudit.bootstrapText)) throw new Error('login page still says "Nothing is sent to a guest"');
  await page.fill('#token', token);
  await Promise.all([
    page.waitForURL(/\/operator\/\?show=[A-Za-z0-9_-]+$/),
    page.click('button[type="submit"]'),
  ]);
  await page.waitForFunction(() => Boolean(document.querySelector('#logout-csrf')?.value));
  const initialRender = await page.evaluate((pattern) => ({
    unresolvedTemplateTokens: new RegExp(pattern).test(document.documentElement.innerHTML),
    hasOpenPageFailureText: /Failed to open page/i.test(document.body?.textContent || ''),
    chip: document.querySelector('#mode-badge')?.textContent?.trim() || '',
    refreshButtonText: document.querySelector('#btn-partnership-refresh')?.textContent?.trim() || '',
  }), UNRESOLVED_TEMPLATE.source);
  if (initialRender.unresolvedTemplateTokens) throw new Error('rendered template tokens remain in dashboard HTML');
  if (initialRender.hasOpenPageFailureText) throw new Error('dashboard indicated an open-page failure');
  if (!SYNTHETIC_CHIP_TEXT.test(initialRender.chip)) {
    throw new Error(`mode chip is not reporting synthetic/runtime context: ${JSON.stringify(initialRender)}`);
  }
  if (initialRender.refreshButtonText !== 'Refresh') throw new Error(`refresh button label is "${initialRender.refreshButtonText}", expected "Refresh"`);
  logoutBoundaryAudit = await page.evaluate(() => {
    const csrfCookie = document.cookie.split(';').map(item => item.trim())
      .find(item => item.startsWith('hospes_csrf='));
    return {
      activeView: document.querySelector('.view-tab.active')?.dataset.view || '',
      inputMatchesCookie: Boolean(
        csrfCookie
        && document.querySelector('#logout-csrf')?.value
          === decodeURIComponent(csrfCookie.split('=', 2)[1]),
      ),
      workbenchLoaded: performance.getEntriesByType('resource')
        .some(entry => entry.name.endsWith('/assets/app.js')),
      partnershipModuleLoaded: performance.getEntriesByType('resource')
        .some(entry => entry.name.endsWith('/assets/partnership.js')),
      workspaceMarkupLoaded: performance.getEntriesByType('resource')
        .some(entry => entry.name.endsWith('/assets/partnership-workspace.html')),
    };
  });
  if (
    logoutBoundaryAudit.activeView !== 'overview'
    || !logoutBoundaryAudit.inputMatchesCookie
    || logoutBoundaryAudit.workbenchLoaded
    || logoutBoundaryAudit.partnershipModuleLoaded
    || logoutBoundaryAudit.workspaceMarkupLoaded
  ) throw new Error(`logout CSRF is not initialized eagerly and independently: ${JSON.stringify(logoutBoundaryAudit)}`);
  await page.waitForSelector('#agenda details');
  await page.waitForSelector('#capabilities .check');
  await page.waitForSelector('#audit-timeline li');
  await page.click('.view-tab[data-view="workbench"]');
  await page.waitForSelector('#candidate-slate .slate-card');
  const approvedOpportunity = await page.$eval('#workbench-opportunity', select => (
    [...select.options].find(option => option.textContent.includes('APPROVED'))?.value || ''
  ));
  if (!approvedOpportunity) throw new Error('no approved opportunity is available for contact prefill');
  await page.selectOption('#workbench-opportunity', approvedOpportunity);
  await page.waitForSelector('#contact-roster-panel:not(.hidden) .contact-pill');
  await page.waitForSelector('#cards .contact-route-pill');
  await page.click('#btn-log-touchpoint');
  await page.waitForSelector('#touchpoint-dialog[open]');
  await page.selectOption('#touchpoint-opportunity', approvedOpportunity);
  await page.selectOption('#touchpoint-form select[name="channel"]', 'text');
  await page.fill('#touchpoint-form textarea[name="notes"]', '<Synthetic maybe; will check schedule>');
  await page.click('#touchpoint-form button[type="submit"]');
  await page.waitForFunction(() => !document.querySelector('#touchpoint-dialog')?.open);
  await page.waitForFunction(() => document.querySelector('#guest-touchpoint-timeline')?.textContent.includes('Synthetic maybe'));
  const touchpointAudit = await page.evaluate(() => ({
    guestText: document.querySelector('#guest-touchpoint-timeline li')?.textContent || '',
    guestHTML: document.querySelector('#guest-touchpoint-timeline li')?.innerHTML || '',
  }));
  await page.click('.view-tab[data-view="overview"]');
  await page.waitForFunction(() => document.querySelector('#audit-timeline')?.textContent.includes('Synthetic maybe'));
  touchpointAudit.executiveText = await page.textContent('#audit-timeline');
  const duplicateAudit = await page.evaluate(() => {
    const items = [...document.querySelectorAll('#audit-timeline li')];
    const entries = items.map(item => item.textContent?.trim()).filter(Boolean);
    const uniqueEntries = [...new Set(entries)];
    return {
      total: entries.length,
      unique: uniqueEntries.length,
      hasLocaleTimestamps: items.some(item => /T\d{2}:\d{2}:\d{2}/.test(item.querySelector('time')?.textContent || '') === false),
    };
  });
  if (duplicateAudit.total > 0 && duplicateAudit.unique !== duplicateAudit.total) {
    throw new Error(`audit timeline has ungrouped duplicate entries: ${JSON.stringify(duplicateAudit)}`);
  }
  if (!duplicateAudit.hasLocaleTimestamps) throw new Error('audit timeline still showing raw ISO timestamps');
  await page.click('.view-tab[data-view="workbench"]');
  if (
    !touchpointAudit.guestText.includes('<Synthetic maybe; will check schedule>')
    || !touchpointAudit.guestHTML.includes('&lt;Synthetic maybe; will check schedule&gt;')
    || !touchpointAudit.executiveText.includes('<Synthetic maybe; will check schedule>')
  ) throw new Error(`touchpoint timelines are incomplete or unsafe: ${JSON.stringify(touchpointAudit)}`);
  const verticalSliceAudit = await page.evaluate(() => {
    const contextActor = document.querySelector('#context-actor')?.textContent || '';
    const candidateSlateCount = document.querySelectorAll('#candidate-slate .slate-card').length;
    const decisionQueueVisible = Boolean(document.querySelector('#workbench-opportunity'));
    const draftControls = document.querySelector('#draft-console')?.classList.contains('hidden') === false;
    const draftButton = Boolean(document.querySelector('#btn-preview-draft:not(.hidden)'));
    const receiptActions = document.querySelector('#receipt-form')?.classList.contains('hidden') === false;
    const auditItems = document.querySelectorAll('#audit-timeline li').length;
    return {
      contextActor,
      candidateSlateCount,
      decisionQueueVisible,
      draftControls,
      draftButton,
      receiptActions,
      auditItems,
    };
  });
  if (
    !verticalSliceAudit.contextActor
    || verticalSliceAudit.candidateSlateCount < 2
    || !verticalSliceAudit.decisionQueueVisible
    || !verticalSliceAudit.draftControls
    || !verticalSliceAudit.draftButton
    || !verticalSliceAudit.receiptActions
    || verticalSliceAudit.auditItems === 0
  ) throw new Error(`vertical slice failed: ${JSON.stringify(verticalSliceAudit)}`);
  await page.fill('#network-map-form input[name="guest"]', 'Theo Von');
  await page.selectOption('#network-map-form select[name="depth"]', '2');
  await page.click('#network-map-form button[type="submit"]');
  await page.waitForFunction(() => document.querySelector('#network-map-summary')?.textContent.includes('1 reachable C2/C3 target paths'));
  const networkAudit = await page.evaluate(() => ({
    summary: document.querySelector('#network-map-summary')?.textContent || '',
    pathText: document.querySelector('#network-target-paths li')?.textContent || '',
    pathHTML: document.querySelector('#network-target-paths li')?.innerHTML || '',
    injectedElements: document.querySelectorAll('#network-target-paths script, #network-target-paths img').length,
  }));
  if (
    !networkAudit.pathText.includes('<Synthetic C2>')
    || !networkAudit.pathText.includes('Theo Von → <Synthetic C2>')
    || !networkAudit.pathHTML.includes('&lt;Synthetic C2&gt;')
    || networkAudit.injectedElements !== 0
  ) throw new Error(`relationship map UI is incomplete or unsafe: ${JSON.stringify(networkAudit)}`);
  const contactAudit = await page.evaluate(() => ({
    panelHidden: document.querySelector('#contact-roster-panel').classList.contains('hidden'),
    pillText: document.querySelector('#contact-roster-panel .contact-pill')?.textContent || '',
    pillHTML: document.querySelector('#contact-roster-panel .contact-pill')?.innerHTML || '',
    prefill: document.querySelector('#invitation-route-prefill')?.value || '',
    queuePill: document.querySelector('#cards .contact-route-pill')?.textContent || '',
  }));
  if (
    contactAudit.panelHidden
    || !contactAudit.pillText.includes('<Synthetic Publicist>')
    || !contactAudit.pillHTML.includes('&lt;Synthetic Publicist&gt;')
    || !contactAudit.prefill.includes('publicist@example.test')
    || !contactAudit.queuePill.includes('<Synthetic Publicist>')
  ) throw new Error(`contact roster UI is incomplete or unsafe: ${JSON.stringify(contactAudit)}`);
  const differentOpportunity = await page.$eval('#workbench-opportunity', select => (
    [...select.options].find(option => option.value !== select.value)?.value || ''
  ));
  if (!differentOpportunity) throw new Error('stale relationship-map audit requires two opportunities');
  await page.fill('#network-map-form input[name="guest"]', 'Slow Guest');
  await page.click('#network-map-form button[type="submit"]');
  await page.selectOption('#workbench-opportunity', differentOpportunity);
  await page.waitForTimeout(250);
  staleNetworkAudit = await page.evaluate(() => ({
    summary: document.querySelector('#network-map-summary')?.textContent || '',
    pathCount: document.querySelectorAll('#network-target-paths li:not(.empty)').length,
  }));
  if (
    !staleNetworkAudit.summary.includes('Choose a public guest name')
    || staleNetworkAudit.pathCount !== 0
  ) throw new Error(`stale relationship map was rendered after scope change: ${JSON.stringify(staleNetworkAudit)}`);
  await page.click('#btn-demo');
  await page.click('#btn-load-demo');
  await page.waitForFunction(() => document.querySelectorAll('#cards .card').length === 5);
  await page.evaluate(() => window.scrollTo(0, 240));
  await page.waitForTimeout(50);

  const audit = await page.evaluate(() => {
    const visible = element => {
      const style = getComputedStyle(element);
      return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
    };
    const undersized = [...document.querySelectorAll('button, input, select, textarea')]
      .filter(visible)
      .map(element => ({
        tag: element.tagName,
        id: element.id,
        width: element.getBoundingClientRect().width,
        height: element.getBoundingClientRect().height,
      }))
      .filter(item => item.width < 44 || item.height < 44);
    const tabs = document.querySelector('.view-tabs');
    const chip = document.querySelector('#mode-badge');
    const chipShown = chip && visible(chip);
    const header = document.querySelector('.header-row');
    const textareas = [...document.querySelectorAll('textarea')]
      .filter(visible)
      .map(element => element.getBoundingClientRect().height);
    const cards = [...document.querySelectorAll('#cards .card')];
    const protectedCard = cards.find(card => (
      card.querySelector('.pill.protected')?.textContent.trim().toUpperCase() === 'C4'
    ));
    return {
      viewport: [window.innerWidth, window.innerHeight],
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      undersized,
      tabsOverflowX: getComputedStyle(tabs).overflowX,
      tabsScrollable: tabs.scrollWidth > tabs.clientWidth,
      chipShown,
      chipText: chip?.textContent?.trim() || '',
      headerPosition: getComputedStyle(header).position,
      headerTop: getComputedStyle(header).top,
      textareaMinimum: Math.min(...textareas),
      cards: cards.length,
      eligibleCards: cards.filter(card => card.querySelector('.card-eyebrow')?.textContent.trim().toUpperCase() === 'APPROVED').length,
      protectedHasApprove: Boolean(protectedCard?.querySelector('.act-approve')),
      protectedHasReject: Boolean(protectedCard?.querySelector('.act-reject')),
      protectedHasProtect: Boolean(protectedCard?.querySelector('.act-protect')),
    };
  });
  audit.contactRoster = contactAudit;
  audit.touchpoints = touchpointAudit;
  audit.relationshipMap = networkAudit;
  audit.staleRelationshipMap = staleNetworkAudit;
  audit.logoutBoundary = logoutBoundaryAudit;
  if (audit.viewport[0] !== 375 || audit.viewport[1] !== 667) throw new Error(`wrong viewport: ${audit.viewport}`);
  if (audit.overflow > 1) throw new Error(`document overflows by ${audit.overflow}px`);
  if (audit.undersized.length) throw new Error(`undersized controls: ${JSON.stringify(audit.undersized)}`);
  if (!['auto', 'scroll'].includes(audit.tabsOverflowX) || !audit.tabsScrollable) throw new Error('tabs are not horizontally scrollable');
  if (
    !audit.chipShown
    || !SYNTHETIC_CHIP_TEXT.test(audit.chipText)
    || audit.headerPosition !== 'sticky'
    || audit.headerTop === '0px'
  ) throw new Error(`mode chip or sticky navigation missing: ${JSON.stringify(audit)}`);
  if (audit.textareaMinimum < 120) throw new Error(`textarea is ${audit.textareaMinimum}px high`);
  if (audit.cards !== 5 || audit.eligibleCards !== 3) throw new Error(`wrong synthetic candidate counts: ${JSON.stringify(audit)}`);
  if (audit.protectedHasApprove || audit.protectedHasReject || !audit.protectedHasProtect) throw new Error(`protected candidate actions are unsafe: ${JSON.stringify(audit)}`);
  const layoutAudits = {};
  for (const target of responsiveViewports) {
    await page.setViewportSize({ width: target.width, height: target.height });
    const layoutAudit = await collectLayoutAudit(page);
    layoutAudits[target.label] = layoutAudit;
    if (layoutAudit.overflow > 1) {
      throw new Error(`viewport ${target.label} has ${layoutAudit.overflow}px of horizontal overflow`);
    }
    if (target.width <= 420 && (!['auto', 'scroll'].includes(layoutAudit.tabsOverflowX) || !layoutAudit.tabsScrollable)) {
      throw new Error(`viewport ${target.label} tabs are not horizontally scrollable`);
    }
    if (target.width >= 1280 && layoutAudit.tabsScrollable) {
      throw new Error(`viewport ${target.label} tabs should not scroll`);
    }
    if (!SYNTHETIC_CHIP_TEXT.test(layoutAudit.chipText)) {
      throw new Error(`viewport ${target.label} chip text is missing synthetic marker`);
    }
    if (target.width <= 420 && layoutAudit.textareaMinimum < 120) {
      throw new Error(`viewport ${target.label} has small textarea controls`);
    }
  }
  await page.setViewportSize({ width: 375, height: 667 });
  audit.layoutAudits = layoutAudits;
  await page.screenshot({ path: path.join(artifactDir, 'mobile-375x667.png'), fullPage: true });
  await fs.writeFile(path.join(artifactDir, 'playwright-audit.json'), `${JSON.stringify(audit, null, 2)}\n`);
  cookieHeader = (await context.cookies(baseUrl)).map(cookie => `${cookie.name}=${cookie.value}`).join('; ');
} finally {
  await browser.close();
}
if (qualitySignals.requestFailures.length) {
  throw new Error(`script request failures: ${JSON.stringify(qualitySignals.requestFailures)}`);
}
if (qualitySignals.responseFailures.length) {
  throw new Error(`module/script response failures: ${JSON.stringify(qualitySignals.responseFailures)}`);
}
if (qualitySignals.pageErrors.length) {
  throw new Error(`page errors: ${JSON.stringify(qualitySignals.pageErrors)}`);
}
if (qualitySignals.consoleErrors.length) {
  throw new Error(`console errors: ${JSON.stringify(qualitySignals.consoleErrors)}`);
}

const chrome = await launchChrome({
  chromeFlags: ['--headless'],
});
try {
  let result;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    result = await lighthouse(`${baseUrl}/operator/`, {
      port: chrome.port,
      logLevel: 'error',
      output: 'json',
      onlyCategories: ['performance'],
      formFactor: 'mobile',
      screenEmulation: { mobile: true, width: 375, height: 667, deviceScaleFactor: 1, disabled: false },
      extraHeaders: { Cookie: cookieHeader },
    });
    if (!result) throw new Error('Lighthouse returned no result');
    if (!result.lhr.runtimeError) break;
    await fs.writeFile(
      path.join(artifactDir, `lighthouse-runtime-error-${attempt}.json`),
      result.report,
    );
    const code = result.lhr.runtimeError.code || 'UNKNOWN_RUNTIME_ERROR';
    if (!['NO_NAVSTART', 'NO_FCP'].includes(code) || attempt === 2) {
      throw new Error(`Lighthouse runtime error: ${code}`);
    }
  }
  if (!result) throw new Error('Lighthouse returned no result');
  await fs.writeFile(path.join(artifactDir, 'lighthouse.json'), result.report);
  const score = result.lhr.categories.performance.score ?? 0;
  if (score < 0.9) throw new Error(`Lighthouse mobile performance score ${Math.round(score * 100)} is below 90`);
  process.stdout.write(`dashboard quality: 375x667 Playwright PASS; Lighthouse ${Math.round(score * 100)}\n`);
} finally {
  await chrome.kill();
}
