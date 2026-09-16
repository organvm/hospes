# Pilot review validation

Owner: Codex; source main 3ea044cf521296fe70a608dc64d4d03c338479ea.

The existing readiness projection separates guest_pilot recording from technical_rehearsal. Existing synthetic tests do not establish real participant evidence. The external pilot remains owned by issue #9.

Review count validation accepted booleans as integers. Six added input cases reject true and false for decisions_count, coverage_met, and coverage_total before storage. The unfixed test reached store.insert instead of rejecting; the repair passes 127 focused receipt, partnership, and pilot-service tests. No live receipt or participant state changed.

Full done.sh verification is the integration predicate. Retain actual external participation, custody, and correspondence in their private owner.

## Exact-code verification and owned failures

At c65b9f081329405d9e4f9da5d08f10e0ab74fbd4, host-admitted done.sh passed 1,070 tests at 93.63 percent coverage, 10 Node tests, shell checks, demo twice without checkout changes, configuration, and installed-wheel acceptance. It exited 1 at missing docs/ASKS-LEDGER.md. The ledger has no history in this public checkout. Do not fabricate historical asks or import private source.

Independent remaining gates passed: hardening ratchet, declared predicates, permission parity, privacy and whitespace. Completion projection check also fails because docs/plans/2026-08-10-hospes-1.0-completion.md is absent. Owner: this PR / Codex continuation. Next: reconcile the public ledger and generated projection homes with their canonical source, then run python3 scripts/check_asks.py and python3 scripts/check_completion_registry.py --check. Preserve unchanged green test receipts. These failures prevent a full done claim; the ratchets still retain 109 open hardening findings and nine permission divergences.

## Source-scope correction

The public repository is a separate immutable GitHub repository created after the original operations repository. Its issue #9 is absent. Read-only metadata confirms the historical pilot issue and ask ledger remain in the private operations owner. The earlier issue #9 reference is historical, not an issue in this public repository. Do not copy the private ledger into public Git. The public done predicate still incorrectly requires private-era document homes; its scope must be repaired while retaining explicit private acceptance ownership.

## Public software predicate routing

The public command now explicitly chooses docs/PUBLIC-ACCEPTANCE.md and the
public generated completion projection. The 25 existing content requirements
remain intact, with matching artifact rows derived only from public code.
Default checker invocations still require their historical operations inputs;
there is no silent fallback. Five tests cover explicit selection, missing and
empty input, and attempts to select files outside the checkout. No private
source body was read or copied. The final done statement names public software
and leaves private pilot outcomes external. Full verification follows on this
changed command tree; older passing shards remain historical evidence.

## Public verification receipt

The exact code tree 1e6d1f8 passed host-admitted bash done.sh, exit 0,
including full Python coverage, Node and shell checks, clean demo twice,
installed-wheel acceptance, explicit public artifact/content checks, both
completion projections, debt and permission ratchets, privacy and whitespace.
The public scope is software verification only. No private ledger or participant
source was copied, and no publication consent or real pilot was inferred.
