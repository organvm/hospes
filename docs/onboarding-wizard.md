# Non-Technical Onboarding Wizard (`hospes init`)

A new show should not require an operator to hand-write YAML, know the pipeline
CSV's column order, or discover the credential-reference rule by hitting an
error. `hospes init` asks eight questions and leaves behind a workspace that is
already validated, already demonstrated, and already honest about the one thing
it will not do on its own. Issue #31 is closed by
`tests/issue_predicates/test_issue_31.py`, which proves every contract on this
page.

## The exact eight questions

The interactive wizard and the `--answers` JSON form ask for the same eight
keys, in the same order. `hospes.onboarding.ANSWER_KEYS` is the single
declaration both read.

| # | Answer key | Question | Required |
|---|------------|----------|----------|
| 1 | `show_name` | Show name | yes |
| 2 | `host_names` | Host names (comma-separated) | yes |
| 3 | `recording_cities` | Recording cities (LA/NYC/Austin/custom, comma-separated) | yes |
| 4 | `show_format` | Show format (`interview`/`narrative`/`conversation`) | yes |
| 5 | `primary_format` | Primary format (`audio`/`video`/`both`) | yes |
| 6 | `partnership_type` | Partnership type (`solo`/`co-host`/`network`) | yes |
| 7 | `notification_email_ref` | Email for operator notifications, as an opaque credential reference | yes |
| 8 | `github_repo_name` | GitHub repo name | optional |

Three rules make "exactly eight" mean something:

- **A ninth key is an error, not a shrug.** An unknown answer key is rejected by
  name. Ignoring it would let a typo (`show_fromat`) fall through to a guessed
  default the operator never chose and cannot see.
- **The three choice answers are checked against their vocabularies.** A show
  format of `podcast` fails at the wizard rather than at the first demo run.
- **Question 7 takes a reference, never an address.** Operator notification
  email is private contact data, and generated configuration is committed, so
  the answer must be an opaque `credential://` or `op://` reference. A literal
  address is refused with the reason (`email-like content`) named. The
  credential itself lives in the credential wall, never in the repository.

Question 3 is free text so "custom" is a real answer; LA/NYC/Austin are
suggestions (`hospes.onboarding.SUGGESTED_CITIES`), not an enumeration.

## What one run produces

```bash
mkdir new-podcast
python3 -m hospes init --root new-podcast
# → eight questions → a validated workspace
python3 -m hospes init --root new-podcast --answers answers.json   # same wizard, no prompts
```

The wizard writes the engine's shipped top-level configuration into the new
workspace, then the show's own documents on top of it:

| Path | What it holds |
|------|---------------|
| `dna/<show>.show.yaml` | Show DNA: identity, hosts, recording cities, primary format, partnership type, and the format engine |
| `config/shows/<show>.yaml` | The show contract: tenant, label, safe defaults (`operator_packet` + `draft_only`), roles, and `dna_ref` |
| `config/partnerships/<show>.yaml` | An empty partnership record ready for the first commitment |
| `config/brand.yaml` | White-label name and colors |
| `config/analytics.yaml` | Analytics provider template with credential references, never keys |
| `config/notifications.yaml` | The operator notification credential reference and `draft_only` delivery |
| `data/example-pipeline.csv` | The candidate pipeline template |

The pipeline template carries `hospes.pipeline.PIPELINE_HEADERS` — the engine's
own canonical header order. A shorter hand-written header omitted columns the
candidate validator requires (`contact_route`, `preferred_city`,
`social_cost_1_5`, `ari_effort`, `source_provenance`, `next_action`), so the
first row a new operator typed could not pass `hospes demo`. The template is
now generated from the validator's own list, so it cannot drift again.

The derived `show_id` is route-safe and filename-safe by construction: it is
slugged from the show name, clamped to the identifier the show-configuration
resolver accepts, and rejected outright if no usable identifier survives.

## Validate and demo, in the same run

`hospes init` does not hand back a directory and hope. Before it returns:

1. **`validate_workspace(root)`** runs the engine's validators against the new
   root — `dna.validate_all` over the workspace's `dna/` tree, the show
   contract's id / mode / `dna_ref`-resolution rules, the shared
   `configuration.assert_no_secrets` guard over every generated document, and
   `pipeline.validate_candidates` over the template. The engine's module-level
   `CONFIG_DIR` is bound at import time, so a workspace created *elsewhere*
   needs these checks driven from explicit paths; that is what this function
   is.
2. **The demo runs**, scoped to the new root: the same `DemoRunner` that backs
   `hospes demo`, with `out_dir` pointed at `<root>/out`. Its summary is
   returned in the result. `--skip-demo` opts out; nothing else does.

The result's `ok` field is the wizard's own predicate: validation green, the
demo ran (unless skipped), and no authorized GitHub action failed. The CLI
exits `0` and prints ``Run `hospes demo --open` to start`` only when `ok` holds;
otherwise it prints the failing checks to stderr and exits `1`.

## Rerunning is safe

The default rerun is **refused**, listing every managed file that already
exists. Overwriting an operator's edited configuration is the one
unrecoverable thing this command could do, and a wizard that a non-technical
operator runs twice by accident must not be the thing that does it.

`--merge` is the explicit, non-destructive rerun: it creates only files that
are missing and never rewrites a byte that is already on disk. Existing files
come back in `preserved`; a second `--merge` run over a complete workspace
generates nothing at all. Validation and the demo run on every form, so a
merge rerun re-proves the workspace rather than assuming it.

## GitHub creation is gated on authorization

Answer 8 is a *request*, never a trigger. Creating a real repository is
outbound work, and `AGENTS.md` requires that live outbound work carry an
explicit human authorization receipt and that a missing external account
surface as visible `unconfigured`/`blocked` state rather than being hidden or
silently substituted. The gate therefore has five honest outcomes:

| `github.status` | When | Did anything run? |
|-----------------|------|-------------------|
| `not_requested` | Answer 8 was left blank | no |
| `blocked` | A repo was named, but no authorization receipt was supplied | no |
| `unconfigured` | Authorized, but the GitHub CLI is not installed on this host | no |
| `created` | Authorized, `gh` available, creation succeeded | yes |
| `failed` | Authorized, `gh` available, creation failed (reason reported) | yes |

Authorization is two halves that must arrive together — a named human and an
opaque receipt reference:

```bash
python3 -m hospes init --root new-podcast --answers answers.json \
  --github-authorized-by example_operator \
  --github-authorization-ref receipt://hospes/github-repo-create/2026-08-14
```

One half without the other is refused: an authorizer with no receipt, or a
receipt with no authorizer, is an unauthorized request wearing an
authorization's clothes. The reference must be a `receipt://` or
`authorization://` URI, matching the opaque-reference discipline the rest of
the estate uses.

The `gh` invocation is built as an argv list, never a shell string, and the
repository name is constrained to GitHub's own character set, so no answer can
be read as a flag or a path. Creation is also withheld — reported as `blocked`
— whenever workspace validation failed: a broken workspace never reaches the
network. Repository creation runs through `GithubCliAdapter`, which probes
`gh` before it runs anything; any object with `available()` and `create()` can
be substituted, which is how the predicate test proves each outcome without
touching GitHub.

## Receipts

Every run appends its receipts to the workspace's own append-only audit log at
`<root>/out/audit.log` through `hospes.audit`, in this order:

- `onboarding.initialized` — show id, merge mode, generated and preserved counts
- `onboarding.validated` — the validation verdict and every error
- `onboarding.demo` — whether the demo ran, and why not if it did not
- `onboarding.github` — the gate status, reason, authorizing human, and
  authorization reference

The GitHub receipt records the decision and its authorization, never the
adapter's captured output: failure detail is returned to the caller for the
operator to read, and is kept out of the durable log.
