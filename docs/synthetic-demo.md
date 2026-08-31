# Ari synthetic demonstration

This demonstration is a resettable, non-authoritative specimen. It never opens,
copies, migrates, or replaces the canonical Pilot database. Every displayed
figure is fictional, every route and receipt uses `fixture://`, and every
surface says `SYNTHETIC DEMO — NOT AUTHORITY`.

## Build or reset

```bash
python3 -m hospes seed-synthetic-demo \
  --output-dir "$HOME/Library/Application Support/HOSPES/private_pilot/demo" \
  --replace-demo
```

The factory installs one recoverable generation:

- `ari-review.synthetic-demo.sqlite3` for the interactive review and planner;
- `ari-complete.synthetic-demo.sqlite3` for the read-only 14/14 specimen;
- marker-v2 metadata in both databases plus a build-receipt-v2 with a generation
  id bound to `generated_at` and `receipt_version`, lowercase SHA-256 checksums,
  generation cohesion, and aggregate counts only.

The directory is mode `0700`; databases and receipts are mode `0600`. Replacement
holds the private bundle lock, stages on the destination filesystem, fsyncs an
install journal, validates both databases, and restores the prior three artifacts
after any interrupted phase. It fails unless an existing bundle carries the
correct tenant-scoped `engine.synthetic_demo` v2 markers and one generation.
Validation rejects symlinks, SQLite sidecars, migration drift, integrity or
foreign-key errors, a mixed generation, and completed checksum drift. The review
checksum is intentionally allowed to change through legitimate operator writes;
the completed specimen remains checksum-bound. The canonical
`private_pilot/hospes.sqlite3` path is never a valid target.

The review tenant is `ari_demo_review`. It contains five fictional candidates,
exactly three policy-eligible C2/C3 figures, verified fixture routes, a fixture
calendar window, a fresh fourteen-day policy, and no human decisions. The
completed tenant is `ari_demo_complete`; the factory uses the real domain
services to record all candidate decisions, slate selection, review, planner
decision, reviewed draft metadata, fixture receipts, brief, assets, rehearsal,
guest recording, media custody, and scorecard.

## Local-only operation

> [!IMPORTANT]
> `dashboard/index.html` is a server-rendered template, not a standalone dashboard.
> Do not open it directly: unresolved `__HOSPES_*__` placeholders mean the
> operator server is not running. For the guided synthetic review, use
> `python3 -m hospes demo --open`; it starts the loopback operator, verifies the
> rendered dashboard, and opens the correct `/operator/` URL automatically.

Use one session-only token for both processes:

```bash
IFS= read -r -s -p "Operator token: " HOSPES_OPERATOR_TOKEN; printf '\n'
export HOSPES_OPERATOR_TOKEN
python3 -m hospes operator \
  --db "$HOME/Library/Application Support/HOSPES/private_pilot/demo/ari-review.synthetic-demo.sqlite3" \
  --port 8765 --actor ari_demo_owner --role relationship_owner \
  --tenant ari_demo_review --synthetic-demo
```

The completed database uses port `8766` and tenant `ari_demo_complete`.
`--synthetic-demo` refuses an unmarked database or mismatched tenant and cannot
be combined with `--enable-raw-v1`. The completed scenario is opened with
`PRAGMA query_only=ON` and also rejects every authenticated HTTP write. Add
`--secure-session-cookie` only when the browser reaches the operator over HTTPS.
All browser-supplied Authorization and `X-Hospes-*` headers are stripped; actor,
role, and tenant identity are server-owned.

## Cloudflare Access launch

Cloudflare Access must exist before the tunnel starts. Access is the
identity-aware first gate; the HOSPES token remains the independent second
gate. The setup uses two exact-hostname self-hosted applications and one allow
policy per application containing only the owner and Ari's exact external email
identities. Users who do not match a policy are denied by default. See
[self-hosted Access applications](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/),
[Access policies](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/),
and [published Tunnel applications](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/).

The current host requires interactive Cloudflare authentication. Keep the
origin certificate in cloudflared's private user directory; setup rejects a
symlink or group/world-readable certificate and never copies it:

```bash
cloudflared tunnel login
chmod 600 "$HOME/.cloudflared/cert.pem"
```

Then supply runtime-only values. Use an API token with the narrowly required
Access Apps and Policies Write and DNS Write permissions; do not use a global
key.

```bash
export CLOUDFLARE_API_TOKEN=...        # environment only; never argv or disk
export CLOUDFLARE_ACCOUNT_ID=...
export CLOUDFLARE_ZONE_ID=...
export HOSPES_DEMO_ZONE=existing-managed-zone.example
export HOSPES_DEMO_OWNER_EMAIL=...
export HOSPES_DEMO_ARI_EMAIL=...
scripts/setup-synthetic-demo-cloudflare.sh
unset CLOUDFLARE_API_TOKEN HOSPES_DEMO_OWNER_EMAIL HOSPES_DEMO_ARI_EMAIL
```

Setup creates one named tunnel and stores its credentials and local configuration
only under the canonical private demo directory. It uses bounded API pagination
and exact readback of token, account, tunnel, and One-time PIN provider for the
first gate, then applications, policies, DNS, and ingress for the second gate,
before writing the redacted
`hospes.cloudflare-access-receipt.v1`. That receipt contains neither identities,
hostnames, URLs, credentials, nor Cloudflare resource identifiers. Setup creates:

- `ari-review.<zone>` to `http://127.0.0.1:8765`;
- `ari-complete.<zone>` to `http://127.0.0.1:8766`;
- a final `http_status:404` ingress rule.

It does not start the tunnel. Once `access-ready.json` exists:

```bash
scripts/setup-synthetic-demo-cloudflare.sh --verify-only
scripts/start-synthetic-demo-cloudflare.sh
scripts/stop-synthetic-demo-cloudflare.sh
```

There is no Quick Tunnel, LaunchAgent, or permanent service fallback. The Mac
must remain awake and online for the duration of the review. Start uses
whitelisted child environments, scrubs setup secrets, proves both operator ports
and live connector readiness, binds `caffeinate` to the tunnel PID, and writes
atomic process ledger v1 entries with PID/start identity. Stop is idempotent, bounds TERM/KILL,
rejects reused PIDs, and retains remote configuration.

## Walkthrough

1. Open the review hostname at `/operator/` and pass both authentication gates.
2. Approve Bixby Mortarboard, Mildred Punchclock, and Rufus Quibble.
3. Protect Tallulah Sidecar and reject Professor Crumbweather.
4. Assign the approved figures to primary and backup slots.
5. Record the bounded synthetic Ari review.
6. Start the run after the first four gates are green.
7. Inspect assignments, alternatives, timing, constraints, rationale, and risk;
   explicitly approve the ranked activation. Nothing is sent.
8. Open the completed hostname to inspect the read-only 14/14 lifecycle,
   completed plan, event history, and partnership register.

None of this advances the real Pilot 1 predicate.

## Guided demonstration (four audiences, one surface)

`python3 -m hospes demo --open` builds an ephemeral marked bundle, mints a one-time bootstrap
nonce, and opens a browser against the loopback operator on port 8765 (the operator default;
pass `--port` to override). The launcher waits for `http://127.0.0.1:8765/operator/` to answer
over HTTP, confirms the rendered dashboard leaks no `__HOSPES_*__` placeholder, and destroys
everything on exit. `--persona` selects who the guided tour addresses:

```bash
python3 -m hospes demo --open --persona owner      # design intent and invariants
python3 -m hospes demo --open --persona ari        # show-week operating view
python3 -m hospes demo --open --persona investor   # why the boundary is the product
python3 -m hospes demo --open --persona newcomer   # plain language, no product knowledge
```

**Every persona carries full `relationship_owner` authority.** Personas differ in narration depth
and in the identity attributed to their decisions, never in what they may do. That is deliberate:
the demonstration exists to show the human gates working, and a viewer who cannot reach a gate
cannot evaluate it. A viewer who wants to see role-gating instead opens the rail's
**What can I do here?** legend, which lists every capability with its live on/off state and the
role or key material it requires.

Before this flag existed the one-command demo ran as `producer` — a role that cannot decide
candidates at all — so the flagship surface rendered five candidate cards whose only action was
*Save note*, and the product's central claim was unreachable from its own demo.

### What the tour is

A rail docked beside the live cockpit, declared entirely in
[`dashboard/assets/guide.json`](../dashboard/assets/guide.json):

- **14 beats** that switch views, spotlight a real element, and narrate it at the selected depth.
  Back / Play / Next, arrow keys, and Escape all drive it; Play auto-advances.
- **A depth selector** — `pitch`, `tutorial`, `practitioner`, `expert` — changeable mid-tour, so an
  owner can see exactly what an investor is being told.
- **Tooltips on every instrumented element**, reached through a `?` badge that is keyboard
  focusable. A badge appears only where the registry declares an entry.
- **A capability legend** computed from the live operator context through the same
  `capabilitiesFor()` the workbench uses — so the tour can never claim a control the surface does
  not actually offer.

### Private context is seeded

The review specimen seeds encrypted contact rosters and informal touchpoints for **all five**
figures, so no candidate selection lands on an empty panel. Because those two surfaces are gated on
private-field custody, `demo --open` mints an ephemeral `HOSPES_MASTER_KEY_B64` when none is set;
a caller-supplied key always wins, the key lives only in that process, and the bundle is destroyed
on exit. Without custody the panels are absent rather than empty, and a bundle built that way is
still valid — it simply carries no private layer.

### The registry is checked, not trusted

`scripts/check_guide_registry.py` (gate 5 of `done.sh`) exits 0 only when every capability
`capabilities.mjs` computes has a glossary entry and vice versa, every `data-guide` anchor resolves
and every entry is anchored, every beat names a real view and carries narration at every declared
depth, both tracked dashboard trees are byte-identical, and every persona resolves. A capability
added without an explanation is a red gate, not a decayed doc.

That fourth check matters more than it looks: the dashboard is tracked **twice** — `dashboard/` and
`hospes/resources/dashboard/`. A source checkout serves the first, an installed wheel the second,
and nothing else holds them in parity.

## Recorded walkthrough

`scripts/record-demo.sh` films the walkthrough above without a human driving it. It owns the
whole lifecycle — it boots `hospes demo --open --no-browser` on a loopback port, waits for
`/operator/login` to answer, drives the surface with Playwright, tears the server down, and
transcodes the capture to mp4:

```bash
HOSPES_DEMO_CUT=proof    scripts/record-demo.sh   # fast, uncaptioned evidence cut
HOSPES_DEMO_CUT=pitch    scripts/record-demo.sh   # paced, captioned walkthrough
HOSPES_DEMO_CUT=tutorial scripts/record-demo.sh   # films the shipped guided tour
```

`HOSPES_DEMO_PERSONA` selects the audience (default `newcomer`) and `HOSPES_DEMO_DEPTH` overrides
its narration depth. The `tutorial` cut does not re-narrate the product from a parallel script: it
drives the real `#guide-rail` through its own beats and records the result, so a broken or empty
beat appears in the film instead of being hidden by a script that cannot drift because it never
syncs.

Both cuts share one beat list in `scripts/record-demo.mjs`; only pacing and caption overlays
differ. Output goes to `artifacts/demo-video/` (gitignored) as `hospes-demo-<cut>.mp4` beside a
`hospes-demo-<cut>.manifest.json` recording the tenant, the marker text, every beat that ran,
and every beat that was skipped with its reason.

Three properties are deliberate:

- **No stubbed endpoints.** Unlike `scripts/dashboard-quality.mjs`, which intercepts
  `operator-context`, roster, and network responses to audit a fixed layout, the recorder films
  only what the bundle really renders. A beat with no data is skipped and named, never faked —
  a demo that shows data the system did not produce is not evidence.
- **A refusal gate before the first frame.** Recording aborts unless a visible
  `SYNTHETIC DEMO — NOT AUTHORITY` marker is on the page *and* `#context-tenant` matches a demo
  tenant. The recorder cannot be aimed at `private_pilot/hospes.sqlite3`.
- **Ephemeral credentials.** `HOSPES_OPERATOR_TOKEN` and `HOSPES_MASTER_KEY_B64` are minted per
  run when unset and exist only in the run's environment. The master key matters: private-field
  custody is a real capability gate, so without it the contact-roster and touchpoint surfaces
  stay hidden and their beats skip.

`ari_demo_review` seeds no contact roster, so the `roster` beat skips by design; the manifest
records it. Everything else — overview, capabilities, candidate queue, the protected-C4 gate
that offers no approve and no reject, a live encrypted touchpoint receipt, the relationship map,
and the audit timeline — records from real state.
