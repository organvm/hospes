import { rankedDecisionKind } from './planner.mjs';

export function plannerDecisionPayload(plan, requestedKind) {
  const ranked = plan?.ranked_action || {};
  const kind = requestedKind || rankedDecisionKind(plan);
  if (!kind) throw new Error('The ranked plan has no decision kind.');
  const payload = {
    decision_kind: kind,
    expected_revision: Number(plan.revision),
  };
  if (kind === 'activate_set') payload.assignments = ranked.assignments;
  if (kind === 'follow_up') payload.assignment_id = ranked.assignment_id;
  if (kind === 'promote') {
    payload.exhausted_assignment_id = ranked.exhausted_assignment_id;
    payload.promoted_assignment_id = ranked.promoted_assignment_id;
    payload.not_before = ranked.not_before;
  }
  return payload;
}

export function receiptPayload(raw, fields, toIso) {
  const details = {};
  for (const field of fields[raw.receipt_type] || []) {
    const value = raw[field.name];
    details[field.name] = field.type === 'checkbox'
      ? Boolean(value)
      : (field.type === 'datetime-local' ? toIso(value) : value);
  }
  return {
    receipt_type: raw.receipt_type,
    external_reference: raw.external_reference,
    occurred_at: toIso(raw.occurred_at),
    details,
  };
}
