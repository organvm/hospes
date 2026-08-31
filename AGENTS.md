# HOSPES Agent Protocol

- Work in isolated branches or worktrees; keep `main` releasable.
- Treat `config/domain_kernel.yaml` as the domain contract and update it with schema changes.
- Never commit raw prompts, transcripts, mailbox contents, private contact data, credentials, or source exports.
- External correspondence is draft-only unless a task explicitly carries a human-approved send receipt.
- Completion requires executable tests, a clean rerun, and remote custody through a commit or pull request.
- Compose external ORGANVM repositories through adapters and events. Do not copy or merge their implementation into this repository.
- Stage named paths only; never sweep unrelated work with `git add -A`.

## HOSPES 1.0 scope correction

Compatible requirements are additive and configurable. Local, tunneled, hosted, and hybrid
runtimes; operator-mediated and optional isolated guest intake; draft-only, manually receipted,
and provider-connected distribution; and manual, fixture, local, and live provider adapters are
profiles or precedence choices, not mutually exclusive product alternatives. Safe defaults remain
local/private, operator-mediated, draft-only, and least-privilege. Live outbound work always
requires an explicit human authorization receipt, and a missing credential or external account is
recorded as visible `unconfigured` or `blocked` state rather than hidden or silently substituted.
