#!/usr/bin/env python3
"""Permission parity — does the declared contract match what the code enforces?

`spec/permission-matrix.yaml` declared six roles and their capabilities since
1.0 and no runtime code ever read it. Enforcement lived in nineteen module-local
frozensets behind seven divergent `_require_role` helpers, and nothing compared
the two. This is the comparison.

Five checks, all derived from live values:

  A. Role vocabulary — the matrix and `service.HumanRole` declare the same six
     roles, and no enforcement site names a role the matrix has never heard of.
     A site enforcing a NARROWER set is legitimate (the CLI's lifecycle choices
     exclude `editor` deliberately); an unknown role is drift and always reds.

  B. Mirror — `spec/permission-matrix.yaml` and its packaged copy are identical.

  C. Divergence — every site whose enforced role-set disagrees with the matrix's
     declaration for the capability it is about, held to an EXACT baseline in
     both directions. A new or widened divergence reds; so does a baseline entry
     for a divergence that has been resolved, so fixing one and recording it are
     necessarily the same change.

  D. Unnamed capabilities — sites the matrix has no vocabulary for. It cannot
     govern these at all, so they are counted and baselined too.

  E. Enforced-set membership is read by IMPORT, not by parsing source, so the
     census is of what actually executes.

This predicate does not enforce anything and deliberately does not derive
enforcement from the matrix. Doing that today would tighten eight sites at once
— revoking, among others, a producer's ability to decide a clearance. The
divergences are made visible and prevented from growing; each is then decided on
its own evidence.

Exit 0 ⟺ the contract and the code disagree in exactly the recorded ways.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hospes.permissions import (  # noqa: E402
    ENFORCEMENT_SITES,
    PermissionMatrixError,
    divergences,
    load_matrix,
    role_literals,
    uncovered_role_literals,
    unknown_role_literals,
    unknown_roles,
    unnamed_signature,
    unnamed_sites,
)

BASELINE_PATH = ROOT / "permission-divergence.json"
SPEC = ROOT / "spec" / "permission-matrix.yaml"
PACKAGED = ROOT / "hospes" / "resources" / "spec" / "permission-matrix.yaml"


def check_mirror() -> list[str]:
    if not SPEC.exists() or not PACKAGED.exists():
        return ["permission matrix is missing from spec/ or hospes/resources/spec/"]
    if SPEC.read_bytes() != PACKAGED.read_bytes():
        return ["spec/permission-matrix.yaml and its packaged mirror differ"]
    return []


def check_divergence(observed: dict[str, dict[str, list[str]]], recorded: dict[str, dict[str, list[str]]]) -> list[str]:
    """Both directions of every divergence, held to an exact baseline.

    `extra` (code grants beyond the matrix) was the only side ever recorded
    here. A site can also REFUSE a role the matrix grants — revoking a
    matrix-granted role from the code while its recorded `extra` stays
    unchanged previously passed silently, because nothing compared the
    `refused` side to anything. Both are baselined and ratcheted exactly now,
    the same way `extra` alone always was.

    `capability` is baselined too, not just the role diffs it produced. The
    matrix has 21 capability names sharing only 10 distinct role-sets, so
    re-pointing a site at a DIFFERENT capability with the same holders would
    reproduce the same `extra`/`refused` and pass unnoticed — the exact
    coincidence-density problem the module docstring already names as why the
    site map is hand-authored rather than inferred.
    """
    problems = []
    for site, obs in sorted(observed.items()):
        want = recorded.get(site)
        if want is None:
            problems.append(f"NEW DIVERGENCE: {site} grants {obs['extra']} beyond the matrix, not in the baseline")
            continue
        if want.get("capability") != obs["capability"]:
            problems.append(
                f"CHANGED CAPABILITY: {site} is now compared against {obs['capability']!r}, "
                f"baseline records {want.get('capability')!r}"
            )
        if sorted(want.get("extra") or []) != sorted(obs["extra"]):
            problems.append(
                f"CHANGED DIVERGENCE: {site} now grants {obs['extra']}, baseline records {sorted(want.get('extra') or [])}"
            )
        if sorted(want.get("refused") or []) != sorted(obs["refused"]):
            problems.append(
                f"CHANGED REFUSAL: {site} now refuses {obs['refused']}, "
                f"baseline records {sorted(want.get('refused') or [])}"
            )
    for site in sorted(set(recorded) - set(observed)):
        problems.append(f"STALE BASELINE: {site} no longer diverges — remove it in the change that reconciled it")
    return problems


def check_coverage(observed: list[str], recorded: list[str]) -> list[str]:
    """Every role set in the package is either mapped or a declared coverage gap.

    Without this the gate reports the size of its own map and calls it the
    surface. Measured at first run: 18 mapped, 39 real — a coverage claim wrong
    by more than half, stated with a green tick. A silent under-report in a
    completeness gate is worse than no gate, because it retires the question.
    """
    problems = []
    observed_counts: dict[str, int] = {}
    recorded_counts: dict[str, int] = {}
    for signature in observed:
        observed_counts[signature] = observed_counts.get(signature, 0) + 1
    for signature in recorded:
        recorded_counts[signature] = recorded_counts.get(signature, 0) + 1

    for signature in sorted(set(observed_counts) | set(recorded_counts)):
        seen = observed_counts.get(signature, 0)
        want = recorded_counts.get(signature, 0)
        if seen > want:
            problems.append(
                f"UNCOVERED ROLE SET: {signature} appears {seen}x, baseline records {want}x — "
                "map it in ENFORCEMENT_SITES or record the gap"
            )
        elif seen < want:
            problems.append(
                f"STALE COVERAGE GAP: {signature} appears {seen}x, baseline still records {want}x — "
                "remove it in the change that covered it"
            )
    return problems


def check_unnamed(observed: list[str], recorded: list[str]) -> list[str]:
    """Sites with no matrix vocabulary, baselined by KEY + currently-enforced ROLES.

    Comparing keys alone cannot see a site it already lists WIDEN — a key-only
    baseline for `distribution:PUBLISH_ROLES` still matches after `host` is
    added to it, because the key never changed. Both `observed` and `recorded`
    here are `unnamed_signature()` strings (key + sorted members), so a widened
    site produces a signature not in the baseline and reds like any other new
    finding.
    """
    problems = []
    for site in sorted(set(observed) - set(recorded)):
        problems.append(f"NEW OR WIDENED UNNAMED CAPABILITY: {site} enforces something the matrix cannot express")
    for site in sorted(set(recorded) - set(observed)):
        problems.append(f"STALE BASELINE: {site} no longer matches — matrix gained the capability, or it narrowed")
    return problems


def check_unknown_literals(observed: list[str]) -> list[str]:
    """A role-set literal anywhere in the package naming a role HumanRole lacks.

    `unknown_roles()` only ever looks at ENFORCEMENT_SITES. A brand-new inline
    or table-based site naming an invalid role (a typo, an invented role) was
    invisible to it AND, previously, to `role_literals()` itself — the AST
    scan silently dropped any literal that was not a clean subset of known
    roles, so the whole site vanished before either check could see it. This
    always reds; there is no baseline, because a role name the matrix has
    never heard of is never a resting state.
    """
    return [f"UNKNOWN ROLE IN LITERAL: {site} names {unknown} — not in HumanRole" for site, unknown in observed]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit non-zero on any violation")
    parser.add_argument("--baseline", default=str(BASELINE_PATH), help="baseline path override")
    args = parser.parse_args()

    print("=== HOSPES PERMISSION PARITY ===")
    try:
        matrix = load_matrix()
    except PermissionMatrixError as exc:
        print(f"  FAIL  matrix did not load: {exc}")
        return 1 if args.check else 0

    observed_div = {
        item.site: {
            "capability": item.capability,
            "extra": list(item.code_grants_extra),
            "refused": list(item.code_refuses_granted),
        }
        for item in divergences(matrix)
    }
    observed_unnamed = sorted(unnamed_signature(site) for site in unnamed_sites(matrix))
    observed_uncovered = sorted(item.signature for item in uncovered_role_literals())
    all_literals = role_literals()
    stray = unknown_roles()
    unknown_literals = sorted((item.key, list(item.unknown)) for item in unknown_role_literals())

    print(f"roles        {len(matrix.roles)} declared, matching service.HumanRole")
    print(f"capabilities {len(matrix.capabilities)} named across reads/decides/exports")
    print(
        f"role sets    {len(all_literals)} in hospes/*.py — "
        f"{len(ENFORCEMENT_SITES)} mapped to a capability, {len(observed_uncovered)} not yet"
    )
    print(f"divergent    {len(observed_div)} sites enforce a different set than the matrix declares")
    print(f"unnamed      {len(observed_unnamed)} sites the matrix has no vocabulary for")
    print("")

    problems: list[str] = []

    vocab_ok = not stray and not unknown_literals
    print(f"[A] vocabulary    {'OK' if vocab_ok else 'FAIL'} — no site enforces a role the matrix does not know")
    for site, roles in sorted(stray.items()):
        problems.append(f"{site} enforces unknown role(s) {roles}")
    problems.extend(check_unknown_literals(unknown_literals))

    mirror = check_mirror()
    print(f"[B] mirror        {'OK' if not mirror else 'FAIL'} — spec/ and packaged matrix are identical")
    problems.extend(mirror)

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        problems.append(f"baseline file missing: {baseline_path.name}")
        recorded_div, recorded_unnamed, recorded_uncovered = {}, [], []
    else:
        try:
            raw = json.loads(baseline_path.read_text(encoding="utf-8"))
            recorded_div = raw.get("divergences") or {}
            recorded_unnamed = raw.get("unnamed_capabilities") or []
            recorded_uncovered = raw.get("uncovered_role_sets") or []
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"cannot read baseline: {exc}")
            recorded_div, recorded_unnamed, recorded_uncovered = {}, [], []

    div_problems = check_divergence(observed_div, recorded_div)
    print(f"[C] divergence    {'OK' if not div_problems else 'FAIL'} — exactly the recorded disagreements, no more")
    problems.extend(div_problems)

    unnamed_problems = check_unnamed(observed_unnamed, recorded_unnamed)
    print(f"[D] unnamed       {'OK' if not unnamed_problems else 'FAIL'} — exactly the recorded gaps, no more")
    problems.extend(unnamed_problems)

    coverage_problems = check_coverage(observed_uncovered, recorded_uncovered)
    print(f"[E] coverage      {'OK' if not coverage_problems else 'FAIL'} — every role set is mapped or declared")
    problems.extend(coverage_problems)

    grants = {site: v["extra"] for site, v in observed_div.items() if v["extra"]}
    refuses = {site: v["refused"] for site, v in observed_div.items() if v["refused"]}
    if grants:
        print("")
        print("code is more permissive than the contract at:")
        for site, extra in sorted(grants.items()):
            print(f"  {site:<46} grants {', '.join(extra)}")
    if refuses:
        print("")
        print("code REFUSES what the contract grants at:")
        for site, missing in sorted(refuses.items()):
            print(f"  {site:<46} refuses {', '.join(missing)}")

    if problems:
        print("")
        for problem in problems:
            print(f"  FAIL  {problem}")
        print("")
        print(f"permission parity FAILED — {len(problems)} problem(s)")
        return 1 if args.check else 0

    print("")
    print("permission parity OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
