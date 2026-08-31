// record-demo.mjs — record a HOSPES walkthrough against the marked synthetic bundle.
//
// Three cuts:
//   proof    — fast, uncaptioned; evidence the operator surface works end to end.
//   pitch    — paced, captioned; a walkthrough for a human audience.
//   tutorial — drives the product's own guided-tour rail through every beat, so
//              the recording IS the feature rather than a re-narration of it.
//
// This recorder never stubs an endpoint. Whatever the synthetic bundle actually
// renders is what gets filmed; a beat whose data is genuinely absent is skipped
// and named in the manifest rather than faked.
//
// Driven by scripts/record-demo.sh, which owns the server lifecycle.

import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

import { chromium } from 'playwright';

const baseUrl = process.env.HOSPES_DEMO_URL || 'http://127.0.0.1:8765';
const token = process.env.HOSPES_OPERATOR_TOKEN; // allow-secret: environment lookup only
const videoDir = path.resolve(process.env.HOSPES_DEMO_VIDEO_DIR || 'artifacts/demo-video');
const cut = process.env.HOSPES_DEMO_CUT || 'proof';

if (!token) throw new Error('HOSPES_OPERATOR_TOKEN is required');
if (!['proof', 'pitch', 'tutorial'].includes(cut)) {
  throw new Error(`unknown cut: ${cut} (expected proof, pitch, or tutorial)`);
}
/**
 * Narration depth for the tutorial cut. Empty means "keep whatever depth the
 * persona already selected" — forcing 'tutorial' here made an owner, ari, or
 * investor recording narrate at the wrong depth for its own audience.
 */
const tutorialDepth = process.env.HOSPES_DEMO_DEPTH || '';

const VIEWPORT = { width: 1440, height: 900 };
const PACE = cut === 'proof'
  ? { caption: 0, settle: 150, hold: 250 }
  : { caption: 1500, settle: 700, hold: 1100 };

await fs.mkdir(videoDir, { recursive: true });

/** Show a caption for the pitch cut; a no-op for the proof cut. */
async function caption(page, title, note) {
  if (cut !== 'pitch') return;
  await page.evaluate(([titleText, noteText]) => {
    let node = document.querySelector('#hospes-demo-caption');
    if (!node) {
      node = document.createElement('div');
      node.id = 'hospes-demo-caption';
      node.style.cssText = [
        'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:2147483647',
        'padding:20px 32px', 'background:rgba(12,14,18,0.92)', 'color:#f6f7f9',
        'font:500 20px/1.4 ui-sans-serif,-apple-system,Segoe UI,sans-serif',
        'pointer-events:none', 'transition:opacity 220ms ease',
      ].join(';');
      document.body.appendChild(node);
    }
    node.style.opacity = '1';
    node.innerHTML = '';
    const heading = document.createElement('div');
    heading.textContent = titleText;
    heading.style.cssText = 'font-weight:700;letter-spacing:0.01em';
    const body = document.createElement('div');
    body.textContent = noteText;
    body.style.cssText = 'opacity:0.78;font-size:17px;margin-top:4px';
    node.append(heading, body);
  }, [title, note]);
  await page.waitForTimeout(PACE.caption);
}

async function clearCaption(page) {
  if (cut !== 'pitch') return;
  await page.evaluate(() => {
    const node = document.querySelector('#hospes-demo-caption');
    if (node) node.style.opacity = '0';
  });
}

/**
 * True when a selector becomes *visible* within a short budget.
 *
 * `state: 'attached'` is the wrong test here: gated controls such as
 * #btn-log-touchpoint are rendered with a `hidden` class until their capability
 * is configured, so an attached-check passes and the subsequent click then burns
 * the full 30s action timeout before failing.
 */
async function present(page, selector, timeout = 3000) {
  try {
    await page.waitForSelector(selector, { timeout, state: 'visible' });
    return true;
  } catch {
    return false;
  }
}

const BEATS = [
  {
    id: 'overview',
    title: 'The desk, at a glance',
    note: 'Agenda, configured capabilities, and a running audit timeline — every action leaves a receipt.',
    async run(page) {
      await page.click('.view-tab[data-view="overview"]');
      await page.waitForSelector('#agenda details');
      await page.waitForSelector('#capabilities .check');
      await page.waitForSelector('#audit-timeline li');
      await page.evaluate(() => window.scrollTo({ top: 0 }));
    },
  },
  {
    id: 'capabilities',
    title: 'Nothing is silently substituted',
    note: 'Unconfigured providers are shown as unconfigured. A missing credential is visible state, never a fake success.',
    async run(page) {
      await page.locator('#capabilities').scrollIntoViewIfNeeded();
      await page.waitForTimeout(PACE.hold);
    },
  },
  {
    id: 'queue',
    title: 'The candidate queue',
    note: 'Five synthetic candidates. Relationship class drives what the operator is even allowed to do.',
    async run(page) {
      // Deliberately does NOT load the CSV fallback. Doing so swapped live
      // truth for an in-memory fixture and hid the live draft console, so
      // every later beat filmed fixture rows while the narration claimed the
      // real queue — and the roster beat reported "no contact roster in this
      // bundle" for a bundle that has five. The CSV fallback is a real
      // feature; it is explained by its own tooltip, not filmed here.
      await page.click('.view-tab[data-view="workbench"]');
      await page.waitForSelector('#candidate-slate .slate-card');
      await page.waitForFunction(
        () => document.querySelectorAll('#cards .card').length > 0,
        null,
        { timeout: 10_000 },
      );
      await page.evaluate(() => window.scrollTo({ top: 240 }));
    },
  },
  {
    id: 'human-gate',
    title: 'The gate that cannot be automated',
    note: 'A protected C4 contact offers no approve and no reject — only "protect". Six judgment classes are permanently human.',
    optional: true,
    async run(page) {
      // Protection is a decision a human makes, not a property the class
      // confers: a C4 candidate offers all three actions until someone
      // protects them. So this exercises the gate on live data instead of
      // filming a fixture row that was already protected before the camera
      // started — the claim is only worth filming if the transition is real.
      const locate = () => page.evaluate(() => {
        const card = [...document.querySelectorAll('#cards .card')].find(item => (
          item.querySelector('.pill')?.textContent.trim().toUpperCase() === 'C4'
        ));
        if (!card) return null;
        card.scrollIntoView({ block: 'center' });
        return {
          hasApprove: Boolean(card.querySelector('.act-approve')),
          hasReject: Boolean(card.querySelector('.act-reject')),
          hasProtect: Boolean(card.querySelector('.act-protect')),
        };
      });

      const before = await locate();
      if (!before) return { skipped: 'no C4 candidate in this bundle' };
      if (!before.hasProtect) {
        throw new Error(`C4 candidate offers no protect action: ${JSON.stringify(before)}`);
      }
      await page.waitForTimeout(PACE.hold);

      const button = page.locator('#cards .card', { has: page.locator('.pill') })
        .filter({ hasText: 'C4' }).first().locator('.act-protect');
      await button.click({ timeout: 8000 });
      await page.waitForTimeout(PACE.hold);

      const after = await locate();
      await page.waitForTimeout(PACE.hold);
      // Leaving the decision queue altogether is the strongest form of the
      // guarantee, not a missing result: there is no longer a card on which
      // approve or reject could be pressed.
      if (!after) return { before, after: 'removed from the decision queue' };
      if (after.hasApprove || after.hasReject) {
        throw new Error(`protected candidate still exposes decisions: ${JSON.stringify(after)}`);
      }
      return { before, after };
    },
  },
  {
    id: 'roster',
    title: 'Relationship-safe routing',
    note: 'Contact routes carry provenance and permission status. The least socially expensive route is the one it prefills.',
    optional: true,
    async run(page) {
      if (!await present(page, '#workbench-opportunity', 3000)) {
        return { skipped: 'no workbench opportunity selector' };
      }
      // The draft console — and the contact roster inside it — is gated on an
      // APPROVED, non-C4/C5 candidate. That is the product being careful: a
      // private route is not visible until someone has decided to invite the
      // person. So the beat performs that approval rather than reporting an
      // empty panel as if the data were missing.
      if (!await present(page, '#cards .card', 5000)) {
        return { skipped: 'no candidate cards rendered' };
      }
      const approvedCard = page.locator('#cards .card')
        .filter({ hasText: 'C2' }).first();
      if (await approvedCard.count()) {
        const approve = approvedCard.locator('.act-approve');
        if (await approve.count()) {
          await approve.click({ timeout: 8000 });
          await page.waitForTimeout(PACE.hold);
        }
      }
      // The workbench candidate selector repopulates itself when a decision
      // commits (organvm/hospes#59): the queue announces the commit and the
      // workspace re-reads live truth and lands on the decided candidate. The
      // beat no longer presses "Refresh live truth" on the operator's behalf.
      // That reconcile is asynchronous, so wait for the approved option rather
      // than reading the list mid-reload.
      await page.waitForFunction(
        () => [...(document.querySelector('#workbench-opportunity')?.options || [])]
          .some(option => option.textContent.includes('APPROVED')),
        null,
        { timeout: 10_000 },
      ).catch(() => {});
      const chosen = await page.$eval('#workbench-opportunity', select => {
        const approved = [...select.options].find(option => option.textContent.includes('APPROVED'));
        return (approved || select.options[0])?.value || '';
      });
      if (!chosen) return { skipped: 'no opportunity available' };
      await page.selectOption('#workbench-opportunity', chosen);
      // The roster is fetched and decrypted after the selection lands, so this
      // waits on the rendered pill rather than on the selection event. Too
      // short a budget here reports "no contact roster in this bundle" for a
      // bundle that has one, which reads as an unimplemented feature.
      await page.waitForTimeout(PACE.settle);
      if (!await present(page, '#contact-roster-panel:not(.hidden) .contact-pill', 8000)) {
        // Report WHY, not just that it skipped: "no roster in this bundle" was
        // reported for a bundle that has one, which reads as a missing feature.
        const why = await page.evaluate(() => {
          const panel = document.querySelector('#contact-roster-panel');
          const console_ = document.querySelector('#draft-console');
          return {
            panelPresent: Boolean(panel),
            panelHidden: panel?.classList.contains('hidden') ?? null,
            panelDisplay: panel ? getComputedStyle(panel).display : null,
            consoleDisplay: console_ ? getComputedStyle(console_).display : null,
            detailsOpen: document.querySelector('details.capture-panel')?.open ?? null,
            pillsAnywhere: document.querySelectorAll('.contact-pill').length,
          };
        });
        return { skipped: `contact roster not visible: ${JSON.stringify(why)}`, opportunity: chosen };
      }
      await page.locator('#contact-roster-panel').scrollIntoViewIfNeeded();
      await page.waitForTimeout(PACE.hold);
      return { opportunity: chosen };
    },
  },
  {
    id: 'touchpoint',
    title: 'Log the hallway conversation',
    note: 'A text, a DM, a chat at a party — logged as a lightweight encrypted receipt so the relationship record stays true.',
    optional: true,
    async run(page) {
      if (!await present(page, '#btn-log-touchpoint', 2500)) {
        return { skipped: 'touchpoint control not available' };
      }
      await page.click('#btn-log-touchpoint', { timeout: 5000 });
      if (!await present(page, '#touchpoint-dialog[open]', 2500)) {
        return { skipped: 'touchpoint dialog did not open' };
      }
      const opportunity = await page.$eval(
        '#touchpoint-opportunity',
        select => select.options[0]?.value || '',
      ).catch(() => '');
      if (opportunity) await page.selectOption('#touchpoint-opportunity', opportunity);
      await page.selectOption('#touchpoint-form select[name="channel"]', 'text').catch(() => {});
      await page.fill(
        '#touchpoint-form textarea[name="notes"]',
        'Synthetic: said yes in principle, asked us to follow up after the tour.',
      );
      await page.waitForTimeout(PACE.settle);
      await page.click('#touchpoint-form button[type="submit"]');
      await page.waitForFunction(
        () => !document.querySelector('#touchpoint-dialog')?.open,
        null,
        { timeout: 8000 },
      );
      await page.waitForFunction(
        () => document.querySelector('#guest-touchpoint-timeline')?.textContent.includes('said yes in principle'),
        null,
        { timeout: 8000 },
      );
      await page.locator('#guest-touchpoint-timeline').scrollIntoViewIfNeeded();
      await page.waitForTimeout(PACE.hold);
      return { opportunity };
    },
  },
  {
    id: 'network',
    title: 'Who can actually introduce us',
    note: 'Second-degree paths through the network, ranked by social cost — provenance-backed, never invented.',
    optional: true,
    async run(page) {
      if (!await present(page, '#network-map-form input[name="guest"]', 2500)) {
        return { skipped: 'network map form not available' };
      }
      await page.fill('#network-map-form input[name="guest"]', 'Theo Von');
      await page.selectOption('#network-map-form select[name="depth"]', '2').catch(() => {});
      await page.click('#network-map-form button[type="submit"]');
      await page.waitForTimeout(PACE.settle + 400);
      const summary = await page.textContent('#network-map-summary').catch(() => '');
      await page.locator('#network-map-summary').scrollIntoViewIfNeeded().catch(() => {});
      await page.waitForTimeout(PACE.hold);
      return { summary: (summary || '').trim().slice(0, 200) };
    },
  },
  {
    id: 'audit',
    title: 'Every decision leaves a receipt',
    note: 'The executive timeline reflects what just happened. HOSPES drafts and records; it has no send endpoint at all.',
    async run(page) {
      await page.click('.view-tab[data-view="overview"]');
      await page.waitForSelector('#audit-timeline li');
      await page.locator('#audit-timeline').scrollIntoViewIfNeeded();
      await page.waitForTimeout(PACE.hold);
    },
  },
];

const browser = await chromium.launch({ headless: true });
const manifest = { cut, baseUrl, viewport: VIEWPORT, beats: [], skipped: [] };
let videoPath = '';

try {
  const context = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir: videoDir, size: VIEWPORT },
    reducedMotion: 'reduce',
  });
  const page = await context.newPage();

  await page.goto(`${baseUrl}/operator/login`, { waitUntil: 'networkidle' });
  await page.fill('#token', token);
  await Promise.all([
    page.waitForURL(/\/operator\/\?show=[A-Za-z0-9_-]+$/),
    page.click('button[type="submit"]'),
  ]);
  await page.waitForFunction(() => Boolean(document.querySelector('#logout-csrf')?.value));

  // --- Safety gate: refuse to film anything that is not the marked synthetic bundle. ---
  const guard = await page.evaluate(() => {
    const visible = element => {
      if (!element) return false;
      const style = getComputedStyle(element);
      return !element.hidden && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const chip = document.querySelector('#mode-badge');
    return {
      tenant: document.querySelector('#context-tenant')?.textContent?.trim() || '',
      chip: chip?.textContent?.trim() || '',
      chipShown: visible(chip),
    };
  });
  if (!guard.chipShown || !/synthetic/i.test(guard.chip)) {
    throw new Error(`refusing to record: synthetic-mode chip is not displayed (${JSON.stringify(guard)})`);
  }
  if (!/demo/i.test(guard.tenant)) {
    throw new Error(`refusing to record: tenant "${guard.tenant}" is not a demo tenant`);
  }
  manifest.tenant = guard.tenant;
  manifest.marker = guard.chip;
  process.stdout.write(`[record-demo] safety gate passed — tenant=${guard.tenant}\n`);

  if (cut === 'pitch') {
    await caption(page, 'HOSPES', 'The guest, production, and distribution desk installed around a host.');
    await page.waitForTimeout(PACE.hold);
  }

  if (cut === 'tutorial') {
    // The tutorial cut films the shipped guided tour rather than re-narrating
    // it. Anything wrong with the rail — a missing beat, a blank narration —
    // then shows up in the recording instead of being papered over by a
    // parallel script that cannot go out of sync because it never syncs.
    await page.waitForSelector('#guide-rail', { state: 'visible', timeout: 10_000 });
    if (tutorialDepth) {
      await page.selectOption('#guide-depth-select', tutorialDepth);
      await page.waitForTimeout(PACE.settle);
    }
    const depth = await page.$eval('#guide-depth-select', select => select.value);

    const total = Number((await page.textContent('#guide-step'))?.split('/')?.[1]?.trim() || 0);
    if (!total) throw new Error('the guided tour reported no beats');
    manifest.depth = depth;

    for (let step = 0; step < total; step += 1) {
      const shown = await page.evaluate(() => ({
        step: document.querySelector('#guide-step')?.textContent?.trim(),
        title: document.querySelector('#guide-title')?.textContent?.trim(),
        narration: document.querySelector('#guide-narration')?.textContent?.trim() || '',
        spotlit: document.querySelectorAll('.guide-spotlight').length,
      }));
      if (!shown.narration) {
        manifest.skipped.push({ id: `beat-${step + 1}`, reason: 'narration was empty' });
        process.stdout.write(`[record-demo] SKIPPED tour beat ${step + 1}: empty narration\n`);
      } else {
        manifest.beats.push({ id: `beat-${step + 1}`, title: shown.title, detail: shown });
        process.stdout.write(`[record-demo] tour ${shown.step} — ${shown.title}\n`);
      }
      // Hold long enough for a viewer to actually read the narration.
      await page.waitForTimeout(PACE.hold + Math.min(4500, shown.narration.length * 24));
      const next = await page.$('#guide-next');
      if (!next || (await next.isDisabled())) break;
      await next.click();
      await page.waitForTimeout(PACE.settle);
    }
  } else {
    for (const beat of BEATS) {
      await caption(page, beat.title, beat.note);
      let result;
      try {
        result = await beat.run(page);
      } catch (error) {
        if (!beat.optional) throw error;
        result = { skipped: `beat failed: ${error.message}` };
      }
      await page.waitForTimeout(PACE.settle);
      if (result && result.skipped) {
          const { skipped: reason, ...rest } = result;
        manifest.skipped.push({ id: beat.id, reason, ...rest });
        process.stdout.write(`[record-demo] SKIPPED ${beat.id}: ${result.skipped}\n`);
      } else {
        manifest.beats.push({ id: beat.id, title: beat.title, detail: result || null });
        process.stdout.write(`[record-demo] beat ${beat.id}\n`);
      }
    }
  }

  await clearCaption(page);
  await page.waitForTimeout(PACE.settle);

  const video = page.video();
  if (!video) throw new Error('playwright did not attach a video to the page');
  const rawPath = await video.path();
  await context.close(); // flushes the webm
  videoPath = path.join(videoDir, `hospes-demo-${cut}.webm`);
  await fs.rename(rawPath, videoPath);
} finally {
  await browser.close();
}

manifest.video = videoPath;
await fs.writeFile(
  path.join(videoDir, `hospes-demo-${cut}.manifest.json`),
  `${JSON.stringify(manifest, null, 2)}\n`,
);

process.stdout.write(
  `[record-demo] ${cut} cut: ${manifest.beats.length} beats recorded, `
  + `${manifest.skipped.length} skipped -> ${videoPath}\n`,
);
