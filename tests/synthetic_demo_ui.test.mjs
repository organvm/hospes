import assert from 'node:assert/strict';
import test from 'node:test';

import {
  capabilitiesFor,
  plannerCapability,
} from '../dashboard/assets/capabilities.mjs';
import { clipboardText } from '../dashboard/assets/clipboard.mjs';
import {
  DECISION_COMMITTED_EVENT,
  publishDecisionCommitted,
  resolveSelectedOpportunity,
  subscribeDecisionCommitted,
} from '../dashboard/assets/decisions.mjs';
import {
  applyDemoDecision,
  isProtectedCandidate,
} from '../dashboard/assets/demo.js';
import {
  plannerDecisionKinds,
  rankedDecisionKind,
  shouldRequestPilotPlan,
} from '../dashboard/assets/planner.mjs';
import {
  plannerDecisionPayload,
  receiptPayload,
} from '../dashboard/assets/payloads.mjs';
import {
  badgeText,
  escapeHTML,
  formatDue,
  typeLabel,
} from '../dashboard/assets/notifications.js';
import { VIEWS, isView, showView, VIEW_SECTIONS } from '../dashboard/assets/views.mjs';

test('in-memory demo decisions fail closed for protected candidates', () => {
  const protectedCandidate = { id: 'protected', relationship_class: 'C4' };
  const ordinaryCandidate = { id: 'ordinary', relationship_class: 'C3' };
  assert.equal(isProtectedCandidate(protectedCandidate), true);
  assert.equal(isProtectedCandidate(ordinaryCandidate), false);
  assert.deepEqual(
    applyDemoDecision(protectedCandidate, 'approve', 'attempted approval'),
    {
      saved: { decision: 'protect', note: 'attempted approval' },
      refused: true,
      reason: 'protected',
    },
  );
  assert.deepEqual(
    applyDemoDecision({ relationship_class: 'c5' }, 'reject', ''),
    {
      saved: { decision: 'protect', note: '' },
      refused: true,
      reason: 'protected',
    },
  );
  assert.deepEqual(
    applyDemoDecision(ordinaryCandidate, 'approve', 'safe synthetic choice'),
    {
      saved: { decision: 'approve', note: 'safe synthetic choice' },
      refused: false,
      reason: null,
    },
  );
  assert.deepEqual(
    applyDemoDecision(ordinaryCandidate, 'approve', '', { decision: 'protect', note: '' }),
    {
      saved: { decision: 'protect', note: '' },
      refused: true,
      reason: 'protected',
    },
  );
  assert.deepEqual(
    applyDemoDecision(ordinaryCandidate, 'forged-action', ''),
    {
      saved: { decision: 'pending', note: '' },
      refused: true,
      reason: 'invalid-action',
    },
  );
});

const multiActivation = {
  revision: 3,
  action_kind: 'pause',
  required_role: 'producer',
  lifecycle_state: 'ACTIVE',
  ranked_action: {
    decision_kind: 'activate_set',
    required_role: 'relationship_owner',
    assignments: [
      { assignment_id: 'one', not_before: '2026-07-23T12:00:00Z' },
      { assignment_id: 'two', not_before: '2026-07-23T13:00:00Z' },
    ],
  },
};

test('completed specimens expose no write, copy, or export capability', () => {
  const capabilities = capabilitiesFor({
    role: 'relationship_owner',
    demo_scenario: 'complete',
    query_only: true,
  });
  assert.equal(capabilities.writable, false);
  assert.equal(capabilities.canDraft, false);
  assert.equal(capabilities.canExport, false);
  assert.throws(
    () => clipboardText('draft', {
      completed: true,
      canDraft: true,
      reviewed: true,
    }),
    /no copy surface/,
  );
});

test('relationship owner retains draft controls and owns elevated Pilot actions', () => {
  const owner = capabilitiesFor({
    role: 'relationship_owner',
    demo_scenario: 'review_ready',
  });
  const producer = capabilitiesFor({
    role: 'producer',
    demo_scenario: 'review_ready',
  });
  assert.equal(owner.canDraft, true);
  assert.equal(owner.canMultiActivate, true);
  assert.equal(producer.canDraft, true);
  assert.equal(producer.canMultiActivate, false);
  for (const kind of ['activate_set', 'pause', 'promote', 'fallback_rehearsal']) {
    assert.equal(plannerCapability(kind, multiActivation, owner), true);
    assert.equal(plannerCapability(kind, multiActivation, producer), false);
  }
});

test('host sessions do not request plans and the UI uses ranked decision_kind', () => {
  assert.equal(shouldRequestPilotPlan({ role: 'host' }, { id: 'run' }), false);
  assert.equal(
    shouldRequestPilotPlan({ role: 'relationship_owner' }, { id: 'run' }),
    true,
  );
  assert.equal(rankedDecisionKind(multiActivation), 'activate_set');
  const owner = capabilitiesFor({ role: 'relationship_owner' });
  assert.deepEqual(
    plannerDecisionKinds(
      multiActivation,
      owner,
      (kind, plan, capabilities) => plannerCapability(kind, plan, capabilities),
    ),
    ['activate_set', 'pause'],
  );
});

test('planner, receipt, and clipboard payload helpers fail closed', () => {
  assert.deepEqual(plannerDecisionPayload(multiActivation), {
    decision_kind: 'activate_set',
    expected_revision: 3,
    assignments: multiActivation.ranked_action.assignments,
  });
  assert.deepEqual(
    receiptPayload(
      {
        receipt_type: 'consent.signed',
        external_reference: 'fixture://consent',
        occurred_at: '2026-07-23T12:00',
        private_pilot: true,
        clip_scope: 'none',
      },
      {
        'consent.signed': [
          { name: 'private_pilot', type: 'checkbox' },
          { name: 'clip_scope', type: 'select' },
        ],
      },
      value => `${value}:00Z`,
    ),
    {
      receipt_type: 'consent.signed',
      external_reference: 'fixture://consent',
      occurred_at: '2026-07-23T12:00:00Z',
      details: { private_pilot: true, clip_scope: 'none' },
    },
  );
  assert.equal(
    clipboardText('reviewed draft', {
      completed: false,
      canDraft: true,
      reviewed: true,
    }),
    'reviewed draft',
  );
  assert.throws(
    () => clipboardText('draft', {
      completed: false,
      canDraft: true,
      reviewed: false,
    }),
    /reviewed/,
  );
});

test('the view router owns every cockpit view and refuses unknown ones', () => {
  assert.deepEqual(VIEWS, ['overview', 'workbench', 'operations', 'intelligence']);
  assert.equal(isView('analytics'), true);
  assert.equal(isView('network'), true);
  assert.equal(isView('inbox'), false);

  const sections = new Map(VIEW_SECTIONS.map(name => [`#${name}`, {
    hidden: false,
    classList: {
      toggle(_name, force) { this.owner.hidden = force; },
    },
  }]));
  for (const [, section] of sections) section.classList.owner = section;
  const title = { textContent: '' };
  const subtitle = { textContent: '' };
  const doc = {
    querySelector(selector) {
      if (selector === '#page-title') return title;
      if (selector === '#page-subtitle') return subtitle;
      return sections.get(selector) || null;
    },
    querySelectorAll() { return []; },
  };

  assert.equal(showView('queue', doc), true);
  assert.equal(sections.get('#queue-view').hidden, false);
  assert.equal(sections.get('#overview-view').hidden, true);
  assert.equal(title.textContent, 'Operations');
  assert.match(subtitle.textContent, /Revenue, sponsor inventory/);

  assert.equal(showView('nowhere', doc), false);
  assert.equal(title.textContent, 'Operations');
});

test('bell helpers bound the badge and never emit raw markup', () => {
  assert.equal(badgeText({ unread: 0 }), '0');
  assert.equal(badgeText(null), '0');
  assert.equal(badgeText({ unread: 7 }), '7');
  assert.equal(badgeText({ unread: 240 }), '99+');
  assert.equal(typeLabel('clips_needed'), 'Clips needed');
  assert.equal(typeLabel('some_future_type'), 'some future type');
  assert.equal(formatDue(null), 'no due date');
  assert.equal(formatDue('not-a-date'), 'no due date');
  assert.equal(
    escapeHTML('<img src=x onerror="alert(1)">'),
    '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;',
  );
});

test('a committed decision is announced to every surface that projects it', () => {
  const channel = new EventTarget();
  const seen = [];
  const raw = [];
  channel.addEventListener(DECISION_COMMITTED_EVENT, event => raw.push(event.detail));
  const unsubscribe = subscribeDecisionCommitted(channel, detail => seen.push(detail));

  assert.equal(publishDecisionCommitted(channel, 'opp-1', 'approve'), true);
  assert.deepEqual(seen, [{ opportunityId: 'opp-1', action: 'approve' }]);
  assert.deepEqual(raw, [{ opportunityId: 'opp-1', action: 'approve' }]);

  // An unidentified commit is refused rather than triggering a blind reload.
  assert.equal(publishDecisionCommitted(channel, '', 'approve'), false);
  assert.equal(publishDecisionCommitted(null, 'opp-1', 'approve'), false);
  channel.dispatchEvent(new CustomEvent(DECISION_COMMITTED_EVENT, { detail: {} }));
  assert.equal(seen.length, 1);

  unsubscribe();
  publishDecisionCommitted(channel, 'opp-2', 'reject');
  assert.equal(seen.length, 1);
  // The unsubscribed surface stopped listening; the wire itself still carries
  // the commit under the name both surfaces agree on.
  assert.deepEqual(raw.at(-1), { opportunityId: 'opp-2', action: 'reject' });
});

test('the workbench selector honours a decided candidate, then the operator selection', () => {
  const opportunities = [{ id: 'opp-1' }, { id: 'opp-2' }];
  assert.equal(resolveSelectedOpportunity(opportunities, 'opp-1', 'opp-2'), 'opp-2');
  assert.equal(resolveSelectedOpportunity(opportunities, 'opp-2', ''), 'opp-2');
  // A decided or previously selected candidate that left the list falls back to
  // the first option — the value the browser itself selects on replacement.
  assert.equal(resolveSelectedOpportunity(opportunities, 'opp-2', 'gone'), 'opp-2');
  assert.equal(resolveSelectedOpportunity(opportunities, 'gone', ''), 'opp-1');
  assert.equal(resolveSelectedOpportunity([], 'opp-1', 'opp-2'), '');
  assert.equal(resolveSelectedOpportunity(null, 'opp-1', 'opp-2'), '');
});

test('a committed decision repopulates the workbench candidate selector', () => {
  // organvm/hospes#59: the approval queue committed and refreshed itself while
  // the workbench kept its stale list, so the candidate just approved still
  // read EDITORIAL_REVIEW and the APPROVED-gated draft console and contact
  // roster stayed shut until a human pressed "Refresh live truth".
  const channel = new EventTarget();
  const server = new Map([
    ['opp-1', { id: 'opp-1', guest_name: 'First', status: 'EDITORIAL_REVIEW' }],
    ['opp-2', { id: 'opp-2', guest_name: 'Second', status: 'EDITORIAL_REVIEW' }],
  ]);
  let reads = 0;
  const loadOpportunities = () => {
    reads += 1;
    return [...server.values()].map(item => ({ ...item }));
  };

  let opportunities = loadOpportunities();
  let selected = 'opp-1';
  const optionLabels = () => opportunities.map(item => `${item.guest_name} · ${item.status}`);
  subscribeDecisionCommitted(channel, detail => {
    const previous = selected;
    opportunities = loadOpportunities();
    selected = resolveSelectedOpportunity(opportunities, previous, detail.opportunityId);
  });

  // The API applies the lifecycle transition; the browser never guesses it.
  server.set('opp-2', { id: 'opp-2', guest_name: 'Second', status: 'APPROVED' });
  assert.deepEqual(optionLabels(), ['First · EDITORIAL_REVIEW', 'Second · EDITORIAL_REVIEW']);

  publishDecisionCommitted(channel, 'opp-2', 'approve');

  assert.equal(reads, 2);
  assert.deepEqual(optionLabels(), ['First · EDITORIAL_REVIEW', 'Second · APPROVED']);
  assert.equal(selected, 'opp-2');
});
