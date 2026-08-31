// A committed candidate decision changes state that two independent operator
// surfaces project. The approval queue (app.js) owns the cards and refreshes
// itself the moment a decision lands. The pilot workbench
// (partnership-workspace.js) owns the candidate selector, and the draft
// console, slot form, and encrypted contact roster gated behind an APPROVED
// disposition. Before this channel existed the second surface kept its stale
// list: an approved candidate still read EDITORIAL_REVIEW, the console stayed
// closed, and the roster read as a missing feature until a human refreshed the
// latest state (organvm/hospes#59).
//
// The event deliberately carries no candidate payload beyond identity. Every
// listener re-reads the authenticated API rather than patching a browser-side
// guess at the server's lifecycle state machine, so the browser never becomes
// a second writer of HOSPES state.

export const DECISION_COMMITTED_EVENT = 'hospes:decision-committed';

export function decisionCommittedDetail(opportunityId, action) {
  return {
    opportunityId: String(opportunityId ?? ''),
    action: String(action ?? ''),
  };
}

export function publishDecisionCommitted(target, opportunityId, action) {
  const detail = decisionCommittedDetail(opportunityId, action);
  if (!target || !detail.opportunityId) return false;
  target.dispatchEvent(new CustomEvent(DECISION_COMMITTED_EVENT, { detail }));
  return true;
}

export function subscribeDecisionCommitted(target, handler) {
  const listener = event => {
    const detail = decisionCommittedDetail(
      event?.detail?.opportunityId,
      event?.detail?.action,
    );
    if (!detail.opportunityId) return;
    handler(detail);
  };
  target.addEventListener(DECISION_COMMITTED_EVENT, listener);
  return () => target.removeEventListener(DECISION_COMMITTED_EVENT, listener);
}

// Which candidate the workbench selector should land on after the list is
// re-read. A committed decision requests its own candidate so the operator
// lands on the console that the decision just opened; otherwise the operator's
// current selection is preserved. Either request is honoured only while the
// candidate is still in the refreshed list, and the fallback is the first
// option — the value the browser itself selects when options are replaced.
export function resolveSelectedOpportunity(opportunities, previousId, requestedId = '') {
  const items = Array.isArray(opportunities) ? opportunities : [];
  const known = new Set(items.map(item => item?.id).filter(Boolean));
  if (requestedId && known.has(requestedId)) return requestedId;
  if (previousId && known.has(previousId)) return previousId;
  return items[0]?.id || '';
}
