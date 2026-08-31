# Multi-Show / Multi-Tenant Dashboard

HOSPES operates several shows for one operator estate from a single dashboard:
a flagship show, a field show, and client shows, each with its own DNA, voice,
templates, branding, and pilot policy. Issue #23 is closed by
`tests/issue_predicates/test_issue_23.py`, which proves every contract on this
page against the live API surface.

## Show state: URL, signed selection, and session

One server-owned authority selects the active show. A login remains valid while
the operator moves between shows; the selection itself is stored in the signed,
HTTP-only `hospes_operator_show` cookie. `GET /operator/` redirects to the
selected show, or to the configured Flagship default. A top-level
`/operator/?show=<id>` navigation changes selection only to an enabled show with
an active `show_registry` row for the process-bound tenant.

The rendered page contains the canonical show. Dashboard JavaScript sends that
value as `X-Session-Show`; it never derives authority from a query string. The
operator proxy requires URL, signed cookie, header, path records, and payload
records to agree. Missing or conflicting scopes, retired shows, foreign-tenant
shows, and unregistered shows return `403`.

The API enforces the same scope:

- **URL show state** — routes under `/v1/shows/{show_id}/…` (suggestions,
  touchpoints) name their show in the path. Authentication resolves Access
  identities against the requested show (`identity_mappings` rows are selected
  by `requested_show`), and `_require_identity_show` rejects a scoped session
  reaching for another show with `403`.
- **Session show state** — the session layer sets
  `request.scope["hospes.operator_show"]` to the operator's active show. Every
  list and mutation route threads that scope through `_identity_show`, so
  switching the session show performs an **isolated reload**: only the active
  show's opportunities, partnerships, and pilot state are visible or mutable.
  An identity bound to one show that presents a session scope for a different
  show is rejected with `403` before any resource is touched.

An identity with no show binding (`show_id IS NULL`) spans its tenant; an
identity bound to a show is confined to it. The operator header is accepted
only after it matches the server-verified signed selection.

## Show switcher

`GET /v1/shows` powers the dashboard switcher. It returns every active
registered show for the operator's tenant, each row carrying:

- `active` — whether the row is the session's current show, and
- `profile` — the bounded per-show resource profile (label, default flag,
  display order, DNA title, primary format, recording cities, and the
  `dna_ref` / `voice_ref` / `template_ref` / `brand_ref` / `pilot_policy_ref`
  references), resolved from the show's configuration file.

A show-bound identity sees only its own show in the switcher.

## Per-show resources

Each show configuration under `config/shows/<show_id>.yaml` declares its own
resources by repository-relative reference:

| Field | Root | Required |
|-------|------|----------|
| `dna_ref` | `dna/` | for every enabled show |
| `voice_ref` | `config/voices/` | optional |
| `template_ref` | `config/templates/` | optional |
| `brand_ref` | `config/brands/` | optional |
| `pilot_policy_ref` | `config/pilot_policies/` | optional |

`show_resource_path` resolves references without allowing path traversal:
references must start with the declared root prefix, name a YAML file directly
inside that root, and resolve to a registered file — `..` segments, absolute
paths, symbolic links, and unregistered kinds are all rejected. Every DNA,
voice, template, brand, and pilot policy declares its owning `show_id` and
expected version/schema. `validate_configuration` additionally rejects two
shows in one tenant sharing the same resource reference. Rendering loads the
selected show's brand.

Disabled shows may omit `dna_ref`; enabling a show requires its DNA.

## Data scoping

Migration `014_multi_show_partnership_and_pilot_scope` adds `show_id` to
partnerships and pilot tables (existing rows backfill from their opportunity
spine or a sole active registered show; genuinely ambiguous rows remain
`legacy`). Partnership identity is
show-scoped: the same partnership template imported under two shows yields two
independent partnerships, and pilot candidate selection only sees opportunities
belonging to the partnership's own show. Imports name their show explicitly when
multiple active shows exist; the CLI infers a missing `--show` only for a tenant
with exactly one active show:

```bash
python3 -m hospes import-partnership config/partnerships/example-private-pilot.yaml \
  --tenant private_pilot --show flagship_private_pilot \
  --actor example_operator --role producer
```

An ambiguous migrated partnership is assigned by one audited transaction that
updates the partnership, its records, and every dependent Pilot row:

```bash
python3 -m hospes assign-partnership-show --db out/hospes.sqlite3 \
  --tenant private_pilot --partnership <opaque-id> --show flagship \
  --actor example_operator --role producer
```

## Negative cross-show authorization

The predicate proves the deny paths, not just the happy paths:

- a session scoped to show A receives `403` on show B's URL-scoped routes;
- a show-bound identity presenting a session scope for another show receives
  `403`;
- a known show B partnership returns `403` under show A, while a tenant-absent
  record returns `404`;
- opportunity listings under a session scope contain only that show's rows;
- traversal-shaped resource references (`dna/../…`, absolute paths, non-YAML,
  unknown kinds) are rejected at configuration load.
