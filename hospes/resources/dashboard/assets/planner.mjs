export function rankedDecisionKind(plan) {
  const kind = plan?.ranked_action?.decision_kind;
  return typeof kind === 'string' && kind ? kind : null;
}

export function plannerDecisionKinds(plan, capabilities, allows) {
  if (!plan || !capabilities?.writable) return [];
  const ranked = rankedDecisionKind(plan);
  const kinds = [];
  if (ranked && ranked !== 'wait' && allows(ranked, plan, capabilities)) {
    kinds.push(ranked);
  }
  if (
    plan.lifecycle_state === 'ACTIVE'
    && allows('pause', plan, capabilities)
  ) {
    kinds.push('pause');
  }
  return [...new Set(kinds)];
}

export function shouldRequestPilotPlan(context, run) {
  return Boolean(run && context?.role !== 'host');
}
