const OWNER_ROLES = new Set([
  'producer',
  'editorial_owner',
  'relationship_owner',
]);

export function capabilitiesFor(context = {}) {
  const role = String(context.role || '');
  const completed = context.demo_scenario === 'complete' || context.query_only === true;
  const writable = !completed;
  return Object.freeze({
    completed,
    writable,
    canDecideCandidates: writable && ['host', 'editorial_owner', 'relationship_owner'].includes(role),
    canProtect: writable && role === 'relationship_owner',
    canDraft: writable && OWNER_ROLES.has(role),
    canViewContacts: writable && ['host', ...OWNER_ROLES].includes(role),
    canViewTouchpoints: context.private_field_custody_configured === true && ['host', ...OWNER_ROLES].includes(role),
    canLogTouchpoint: writable && context.private_field_custody_configured === true && ['host', ...OWNER_ROLES].includes(role),
    canSendPortalLink: writable && context.guest_portal_configured === true && OWNER_ROLES.has(role),
    canViewClearances: ['host', 'network_operator', ...OWNER_ROLES].includes(role),
    canManageClearances: writable && OWNER_ROLES.has(role),
    canViewGuestHistory: ['host', ...OWNER_ROLES].includes(role),
    canSetDoNotContact: writable && role === 'relationship_owner',
    canViewAnalytics: ['host', ...OWNER_ROLES].includes(role),
    canExportAnalytics: writable && OWNER_ROLES.has(role),
    canRecordEvidence: writable && OWNER_ROLES.has(role),
    // The portfolio spans shows, so it belongs to the one role scoped to the
    // network rather than to a show. Operating a show inside the network does
    // not widen a read to the whole of it.
    canViewNetwork: role === 'network_operator',
    canExportNetworkReport: writable && role === 'network_operator',
    canViewRevenue: ['host', 'network_operator', ...OWNER_ROLES].includes(role),
    canManageSponsors: writable && OWNER_ROLES.has(role),
    canApproveSponsorClaims: writable && ['editorial_owner', 'relationship_owner'].includes(role),
    canResearch: writable && ['producer', 'editorial_owner'].includes(role),
    canViewPublishing: ['host', 'network_operator', ...OWNER_ROLES].includes(role),
    canManagePublishing: writable && OWNER_ROLES.has(role),
    canAuthorizePublication: writable && ['editorial_owner', 'relationship_owner'].includes(role),
    canManageRegister: writable && OWNER_ROLES.has(role),
    canReviewPartnership: writable && OWNER_ROLES.has(role),
    canRequestPilotPlan: role !== 'host',
    canStartPilot: writable && role === 'relationship_owner',
    canMultiActivate: writable && role === 'relationship_owner',
    canPause: writable && role === 'relationship_owner',
    canPromote: writable && role === 'relationship_owner',
    canFallback: writable && role === 'relationship_owner',
    canExport: !completed,
  });
}

export function plannerCapability(kind, plan, capabilities) {
  if (!capabilities?.writable) return false;
  const assignments = plan?.ranked_action?.assignments || [];
  if (kind === 'activate_set' && assignments.length > 1) {
    return capabilities.canMultiActivate;
  }
  if (kind === 'pause') return capabilities.canPause;
  if (kind === 'promote') return capabilities.canPromote;
  if (kind === 'fallback_rehearsal') return capabilities.canFallback;
  const requiredRole = plan?.ranked_action?.required_role || plan?.required_role;
  if (requiredRole === 'relationship_owner') {
    return capabilities.canMultiActivate;
  }
  return requiredRole === 'producer'
    ? capabilities.canRecordEvidence
    : capabilities.canManageRegister;
}
