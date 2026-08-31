// The cockpit's view registry. The shell (partnership.js) and the workspace
// (partnership-workspace.js) both toggle and title views; keeping the ids and
// titles here means a new view is added once instead of drifting between two
// hand-maintained copies.
const VIEW_ALIASES = Object.freeze({
  'revenue': 'operations',
  'publish': 'operations',
  'register': 'operations',
  'queue': 'operations',
  'analytics': 'intelligence',
  'network': 'intelligence',
});
export const VIEWS = Object.freeze(['overview', 'workbench', 'operations', 'intelligence']);
const VIEW_SECTION_MAP = Object.freeze({
  overview: Object.freeze(['overview-view']),
  workbench: Object.freeze(['workbench-view']),
  operations: Object.freeze(['revenue-view', 'publish-view', 'register-view', 'queue-view']),
  intelligence: Object.freeze(['analytics-view', 'network-view']),
});
export const VIEW_SECTIONS = [...new Set(Object.values(VIEW_SECTION_MAP).flat())];

const VIEW_TITLES = Object.freeze({
  overview: Object.freeze([
    'Overview',
    'A live, SQLite-backed view of decisions, obligations, pilot gates, and evidence.',
  ]),
  workbench: Object.freeze([
    'Workbench',
    'Candidate decisions and evidence-only lifecycle actions, constrained by role and state.',
  ]),
  operations: Object.freeze([
    'Operations',
    'Revenue, sponsor inventory, claims governance, publishing readiness, and assignment queue.',
  ]),
  intelligence: Object.freeze([
    'Analytics',
    'Provider metrics and network intelligence for the same operator with attributable receipts.',
  ]),
});

function normalizeView(view) {
  return VIEW_ALIASES[view] || view;
}

export function isView(view) {
  return VIEWS.includes(normalizeView(view));
}

export function isCanonicalView(view) {
  return VIEWS.includes(view);
}

export function canonicalView(view) {
  return normalizeView(view);
}

export function sectionsForView(view) {
  return VIEW_SECTION_MAP[normalizeView(view)] || Object.freeze([]);
}

export function viewTitle(view) {
  return VIEW_TITLES[normalizeView(view)] || VIEW_TITLES.overview;
}

export function showView(view, doc = document) {
  const canonical = normalizeView(view);
  if (!VIEWS.includes(canonical)) return false;
  const activeSections = new Set(VIEW_SECTION_MAP[canonical]);
  for (const name of VIEW_SECTIONS) {
    const section = doc.querySelector(`#${name}`);
    if (!section) continue;
    section.classList.toggle('hidden', !activeSections.has(name));
  }
  doc.querySelectorAll('.view-tab')
    .forEach(button => button.classList.toggle('active', normalizeView(button.dataset.view) === canonical));
  const [title, subtitle] = viewTitle(canonical);
  const heading = doc.querySelector('#page-title');
  const description = doc.querySelector('#page-subtitle');
  if (heading) heading.textContent = title;
  if (description) description.textContent = subtitle;
  return true;
}
