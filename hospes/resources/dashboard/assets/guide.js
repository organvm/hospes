/**
 * Guided tour, capability legend, and tooltip layer.
 *
 * Everything rendered here is declared in ./guide.json.  Nothing in this file
 * asserts a capability or a figure of its own: the legend reads the live
 * operator context through the same capabilitiesFor() used by the workbench,
 * so a control the tour explains is a control the surface actually offers.
 *
 * The dashboard must survive this module failing.  Every entry point degrades
 * to a no-op rather than throwing into the shell.
 */
import { capabilitiesFor } from './capabilities.mjs';
import { loadOperatorContext } from './api.js';

const $ = selector => document.querySelector(selector);

const state = {
  guide: null,
  depth: 'tutorial',
  persona: null,
  beats: [],
  index: 0,
  playing: false,
  timer: null,
};

/** Milliseconds a beat holds when the tour is auto-playing. */
const BEAT_MS = 9000;

/** Below this viewport width the rail never opens by itself. */
const AUTO_OPEN_MIN_WIDTH = 900;

/** Anchors that are already controls; these never receive a nested badge. */
const INTERACTIVE = new Set(['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA']);

function metaContent(name) {
  return document.querySelector(`meta[name="${name}"]`)?.getAttribute('content')?.trim() || '';
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Narration for the active depth, falling back through the declared order. */
function narrate(entry, depth) {
  if (!entry) return '';
  if (typeof entry === 'string') return entry;
  return entry[depth] || entry.tutorial || entry.body || entry.pitch || entry.practitioner || entry.expert || '';
}

// --- tooltip layer ---------------------------------------------------------

let tip = null;

function ensureTip() {
  if (tip) return tip;
  tip = element('div', 'guide-tip');
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  document.body.appendChild(tip);
  return tip;
}

function showTip(anchor, title, body, meta) {
  const node = ensureTip();
  node.replaceChildren();
  node.appendChild(element('b', null, title));
  node.appendChild(element('p', null, body));
  if (meta) node.appendChild(element('span', 'guide-tip-meta', meta));
  node.hidden = false;
  const box = anchor.getBoundingClientRect();
  const width = Math.min(360, window.innerWidth - 24);
  node.style.width = `${width}px`;
  let left = box.left + window.scrollX;
  left = Math.min(left, window.scrollX + window.innerWidth - width - 12);
  node.style.left = `${Math.max(window.scrollX + 12, left)}px`;
  const above = box.top < 220;
  node.style.top = above
    ? `${box.bottom + window.scrollY + 10}px`
    : `${box.top + window.scrollY - node.offsetHeight - 10}px`;
}

function hideTip() {
  if (tip) tip.hidden = true;
}

/**
 * Attach an info affordance to every [data-guide] anchor.
 *
 * A button is injected rather than binding hover on the section itself: the
 * anchors are large layout containers, so a whole-panel hover target would
 * fire constantly, and a button is reachable by keyboard where a <section>
 * is not.
 */
function mountTooltips(root = document) {
  const entries = state.guide?.elements || {};
  root.querySelectorAll('[data-guide]').forEach(anchor => {
    // Presence of the badge is the guard, not a flag on the anchor. Anchors
    // such as #cards and #candidate-slate are repopulated with innerHTML,
    // which deletes the badge; a dataset flag would survive that and suppress
    // the remount, so the tooltips would vanish the moment live data arrived.
    if (anchor.querySelector(':scope > .guide-badge')) return;
    const entry = entries[anchor.dataset.guide];
    if (!entry) return;
    if (anchor.dataset.guideBound === '1') return;
    // Only establish a containing block where there is not one already.
    // Forcing position:relative here would silently demote an anchor that
    // relies on its own positioning — the mode chip lives in the sticky
    // header, and overriding it drops the chip out of the sticky header.
    if (getComputedStyle(anchor).position === 'static') {
      anchor.classList.add('guide-anchored');
    }
    const body = narrate(entry, state.depth) || entry.body;
    const capability = entry.capability ? state.guide.capabilities?.[entry.capability] : null;
    const meta = capability ? `Requires ${capability.requires}` : '';

    // An anchor that is itself interactive gets no badge. Nesting a <button>
    // inside a <button> (#btn-presentation, #btn-log-touchpoint) is invalid
    // markup and gives keyboard and assistive-technology users ambiguous focus
    // and activation. Such anchors are already focusable, so the tooltip binds
    // to them directly and the anchor's own click is left untouched.
    if (INTERACTIVE.has(anchor.tagName)) {
      anchor.dataset.guideBound = '1';
      anchor.setAttribute('aria-description', `${entry.title}. ${body}`);
      const openOn = () => showTip(anchor, entry.title, body, meta);
      anchor.addEventListener('mouseenter', openOn);
      anchor.addEventListener('focus', openOn);
      anchor.addEventListener('mouseleave', hideTip);
      anchor.addEventListener('blur', hideTip);
      return;
    }

    const badge = element('button', 'guide-badge', '?');
    badge.type = 'button';
    badge.setAttribute('aria-label', `Explain: ${entry.title}`);
    const open = () => showTip(badge, entry.title, body, meta);
    badge.addEventListener('mouseenter', open);
    badge.addEventListener('focus', open);
    badge.addEventListener('mouseleave', hideTip);
    badge.addEventListener('blur', hideTip);
    badge.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      if (tip && !tip.hidden) hideTip();
      else open();
    });
    anchor.prepend(badge);
  });
}

// --- capability legend -----------------------------------------------------

/**
 * The shared api.js client is used rather than a bare fetch: it attaches the
 * X-Session-Show scope header the operator boundary requires, without which
 * every request is rejected with 403 and the legend silently reports that no
 * capability is granted.
 */
async function operatorContext() {
  try {
    return (await loadOperatorContext()) || {};
  } catch {
    return {};
  }
}

/**
 * Render every declared capability with its live on/off state.
 *
 * An unavailable capability is shown, not hidden: "why can I not do this"
 * is the question the legend exists to answer.
 */
function renderLegend(container, capabilities) {
  const declared = state.guide?.capabilities || {};
  container.replaceChildren();
  Object.entries(declared).forEach(([key, entry]) => {
    const granted = capabilities[key] === true;
    const row = element('div', `guide-cap ${granted ? 'on' : 'off'}`);
    const head = element('div', 'guide-cap-head');
    head.appendChild(element('span', 'guide-cap-mark', granted ? '✓' : '—'));
    head.appendChild(element('b', null, entry.label));
    row.appendChild(head);
    row.appendChild(element('p', null, entry.what));
    row.appendChild(element('p', 'guide-cap-why', entry.why));
    row.appendChild(element('span', 'guide-cap-req', `Requires ${entry.requires}`));
    container.appendChild(row);
  });
}

// --- tour ------------------------------------------------------------------

function activeView() {
  return document.querySelector('.view-tab.active')?.dataset.view || 'overview';
}

/** Switch tabs and wait for the lazily-loaded view to attach its target. */
async function ensureView(view, selector) {
  if (activeView() !== view) {
    document.querySelector(`.view-tab[data-view="${view}"]`)?.click();
  }
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const node = selector ? document.querySelector(selector) : null;
    if (!selector || node) return node;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return selector ? document.querySelector(selector) : null;
}

function clearSpotlight() {
  document.querySelectorAll('.guide-spotlight').forEach(node => node.classList.remove('guide-spotlight'));
}

async function showBeat(index) {
  const beats = state.beats;
  if (!beats.length) return;
  state.index = Math.max(0, Math.min(index, beats.length - 1));
  const beat = beats[state.index];
  const rail = $('#guide-rail');
  if (!rail) return;

  $('#guide-step').textContent = `${state.index + 1} / ${beats.length}`;
  $('#guide-title').textContent = beat.title;
  $('#guide-narration').textContent = narrate(beat.narration, state.depth);
  $('#guide-prev').disabled = state.index === 0;
  $('#guide-next').disabled = state.index === beats.length - 1;
  $('#guide-progress').style.width = `${((state.index + 1) / beats.length) * 100}%`;

  clearSpotlight();
  const target = await ensureView(beat.view, beat.target);
  mountTooltips();
  // A target can exist and still be hidden: the draft console and its contact
  // roster stay closed until a candidate is approved. Spotlighting an
  // invisible node points the viewer at blank space, so say so instead.
  const visible = target && (() => {
    const style = getComputedStyle(target);
    return !target.classList.contains('hidden')
      && style.display !== 'none'
      && style.visibility !== 'hidden'
      && target.getClientRects().length > 0;
  })();
  const note = $('#guide-note');
  if (visible) {
    target.classList.add('guide-spotlight');
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (note) note.hidden = true;
  } else if (note) {
    note.textContent = target
      ? 'This surface is gated and not visible yet — the narration explains what unlocks it.'
      : 'This surface is not present in the current view.';
    note.hidden = false;
  }
}

function stop() {
  state.playing = false;
  if (state.timer) clearTimeout(state.timer);
  state.timer = null;
  const play = $('#guide-play');
  if (play) {
    play.textContent = 'Play';
    play.setAttribute('aria-pressed', 'false');
  }
}

function tick() {
  if (!state.playing) return;
  state.timer = setTimeout(async () => {
    if (!state.playing) return;
    if (state.index >= state.beats.length - 1) {
      stop();
      return;
    }
    await showBeat(state.index + 1);
    tick();
  }, BEAT_MS);
}

function play() {
  if (state.playing) {
    stop();
    return;
  }
  state.playing = true;
  const button = $('#guide-play');
  button.textContent = 'Pause';
  button.setAttribute('aria-pressed', 'true');
  tick();
}

// --- rail ------------------------------------------------------------------

function buildRail() {
  const rail = element('aside', 'guide-rail');
  rail.id = 'guide-rail';
  rail.hidden = true;
  rail.setAttribute('aria-label', 'Guided tour');

  const head = element('div', 'guide-rail-head');
  const heading = element('div', 'guide-rail-heading');
  heading.appendChild(element('div', 'eyebrow', 'Guided tour'));
  heading.appendChild(element('b', null, state.persona?.label || 'Walkthrough'));
  head.appendChild(heading);
  const close = element('button', 'quiet guide-close', 'Close');
  close.type = 'button';
  close.addEventListener('click', () => toggleRail(false));
  head.appendChild(close);
  rail.appendChild(head);

  if (state.persona?.headline) {
    rail.appendChild(element('p', 'guide-headline', state.persona.headline));
  }
  if (state.persona?.opening) {
    // Rendered as its own element rather than seeded into #guide-narration:
    // the first beat overwrites that node before the browser paints, which
    // made every persona opening dead content.
    rail.appendChild(element('p', 'guide-opening', state.persona.opening));
  }

  const depthRow = element('label', 'guide-depth', 'Explain for ');
  const select = element('select');
  select.id = 'guide-depth-select';
  Object.entries(state.guide.depths).forEach(([key, description]) => {
    const option = element('option', null, key);
    option.value = key;
    option.title = description;
    if (key === state.depth) option.selected = true;
    select.appendChild(option);
  });
  select.addEventListener('change', async event => {
    state.depth = event.currentTarget.value;
    document.querySelectorAll('.guide-badge').forEach(node => node.remove());
    mountTooltips();
    await showBeat(state.index);
  });
  depthRow.appendChild(select);
  rail.appendChild(depthRow);

  const bar = element('div', 'guide-progress-bar');
  const progress = element('div', 'guide-progress');
  progress.id = 'guide-progress';
  bar.appendChild(progress);
  rail.appendChild(bar);

  const body = element('div', 'guide-body');
  body.appendChild(Object.assign(element('div', 'guide-step'), { id: 'guide-step' }));
  body.appendChild(Object.assign(element('h3', 'guide-title'), { id: 'guide-title' }));
  body.appendChild(Object.assign(element('p', 'guide-narration'), { id: 'guide-narration' }));
  const note = Object.assign(element('p', 'guide-note'), { id: 'guide-note' });
  note.hidden = true;
  body.appendChild(note);
  rail.appendChild(body);

  const controls = element('div', 'guide-controls');
  const prev = element('button', 'quiet', 'Back');
  prev.type = 'button';
  prev.id = 'guide-prev';
  prev.addEventListener('click', () => { stop(); void showBeat(state.index - 1); });
  const playButton = element('button', 'btn-primary', 'Play');
  playButton.type = 'button';
  playButton.id = 'guide-play';
  playButton.setAttribute('aria-pressed', 'false');
  playButton.addEventListener('click', play);
  const next = element('button', 'quiet', 'Next');
  next.type = 'button';
  next.id = 'guide-next';
  next.addEventListener('click', () => { stop(); void showBeat(state.index + 1); });
  controls.append(prev, playButton, next);
  rail.appendChild(controls);

  const legendToggle = element('details', 'guide-legend');
  legendToggle.appendChild(element('summary', null, 'What can I do here?'));
  const legendBody = element('div', 'guide-legend-body');
  legendBody.id = 'guide-legend-body';
  legendToggle.appendChild(legendBody);
  rail.appendChild(legendToggle);

  document.body.appendChild(rail);
  return rail;
}

function toggleRail(open) {
  const rail = $('#guide-rail');
  if (!rail) return;
  const next = open === undefined ? rail.hidden : open;
  rail.hidden = !next;
  document.body.classList.toggle('guide-open', next);
  const button = $('#btn-guide');
  if (button) {
    button.setAttribute('aria-pressed', String(next));
    button.textContent = next ? 'Hide tour' : 'Guided tour';
  }
  if (!next) {
    stop();
    clearSpotlight();
    hideTip();
  }
}

// --- boot ------------------------------------------------------------------

async function boot() {
  let guide;
  try {
    const response = await fetch('./assets/guide.json', { credentials: 'same-origin' });
    if (!response.ok) return;
    guide = await response.json();
  } catch {
    return;
  }
  state.guide = guide;

  const personaKey = metaContent('hospes-demo-persona');
  state.persona = guide.personas?.[personaKey] || null;
  state.depth = state.persona?.depth || 'tutorial';
  state.beats = Array.isArray(guide.beats) ? guide.beats : [];

  mountTooltips();
  buildRail();

  const button = $('#btn-guide');
  if (button) button.addEventListener('click', () => toggleRail());

  const EDITABLE = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'OPTION']);
  document.addEventListener('keydown', event => {
    if ($('#guide-rail')?.hidden) return;
    if (event.key === 'Escape') { hideTip(); toggleRail(false); }
    // Arrow keys belong to whatever the operator is editing. The tour opens
    // automatically, so claiming them globally would move a caret and switch
    // the whole cockpit view mid-edit.
    const target = event.target;
    if (EDITABLE.has(target?.tagName) || target?.isContentEditable) return;
    if (event.key === 'ArrowRight') { stop(); void showBeat(state.index + 1); }
    if (event.key === 'ArrowLeft') { stop(); void showBeat(state.index - 1); }
  });

  // Late-attaching views (workbench, register) need their anchors mounted the
  // moment they land in the DOM.
  const observer = new MutationObserver(() => mountTooltips());
  observer.observe(document.body, { childList: true, subtree: true });

  const context = await operatorContext();
  const capabilities = capabilitiesFor(context);
  const legendBody = $('#guide-legend-body');
  if (legendBody) renderLegend(legendBody, capabilities);

  // A persona is only stamped by the synthetic demo, so opening the rail
  // automatically never surprises a real operator session.
  //
  // It stays closed on a narrow viewport regardless: there the rail is a
  // bottom sheet covering a large share of the screen, and auto-opening it
  // would put an overlay between the visitor and the surface they came to
  // see. The "Guided tour" button opens it on demand at every width.
  if (state.persona && window.innerWidth >= AUTO_OPEN_MIN_WIDTH) {
    toggleRail(true);
    await showBeat(0);
  }
}

void boot();
