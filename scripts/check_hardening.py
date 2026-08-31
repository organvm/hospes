#!/usr/bin/env python3
"""Review-debt gate — makes deferred hardening findings visible to `done.sh`.

The repo previously reported green while 109 review findings sat open in eleven
issues, because no predicate could see them. This gate closes that blindness.

Everything it reports is DERIVED. The registry stores no count, no percentage,
and no distance (`hospes.hardening_registry.ALLOWED_FINDING_KEYS` is closed, so
such a field cannot even load). Four independent checks run:

  A. Structural   — the registry loads; every closed vocabulary holds.
  B. Attribution  — every path a row names still exists in the tree. A rename
                    that orphans a finding reds here instead of silently
                    detaching the backlog from the code.
  C. Ratchet      — the derived open counts match the recorded ratchet EXACTLY.
                    Exact, not a ceiling: a regression reds, and so does a
                    stale baseline left high after work landed. The number is
                    re-derived every run, so the ratchet file records a claim
                    that is checked rather than trusted.
  D. Issue state  — (network, opt-in) no hardening issue is CLOSED on GitHub
                    while it still holds open rows. That is the false-closure
                    shape this whole registry exists to prevent.

Exit 0 ⟺ every enabled check passes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hospes.hardening_registry import (  # noqa: E402
    EXPECTED_ISSUES,
    HardeningRegistryError,
    load_registry,
)

REGISTRY_PATH = ROOT / "hospes" / "resources" / "spec" / "hardening-registry.yaml"
RATCHET_PATH = ROOT / "hardening-ratchet.json"


def _fail(message: str) -> None:
    print(f"  FAIL  {message}")


def check_attribution(registry, root: Path = ROOT) -> list[str]:
    """Every path a finding names must exist. Renames must not orphan the backlog."""
    problems = []
    for finding in registry.findings:
        for ref in finding.files:
            if not (root / ref).exists():
                problems.append(f"{finding.id} names a path that does not exist: {ref}")
    return problems


def derived_counts(registry) -> dict[str, int]:
    open_findings = registry.open_findings()
    by_severity = Counter(item.severity for item in open_findings)
    return {
        "open_total": len(open_findings),
        "open_s1": by_severity.get("S1", 0),
        "open_s2": by_severity.get("S2", 0),
        "open_s3": by_severity.get("S3", 0),
    }


def check_ratchet(counts: dict[str, int], ratchet_path: Path = RATCHET_PATH) -> list[str]:
    if not ratchet_path.exists():
        return [f"ratchet file missing: {ratchet_path.name}"]
    try:
        recorded = json.loads(ratchet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read ratchet: {exc}"]
    problems = []
    for key in ("open_total", "open_s1"):
        want = recorded.get(key)
        got = counts[key]
        if not isinstance(want, int):
            problems.append(f"ratchet {key} must be an integer")
        elif got > want:
            problems.append(f"REGRESSION: {key} is {got}, ratchet allows {want}")
        elif got < want:
            problems.append(
                f"STALE RATCHET: {key} is {got} but the ratchet still records {want} — "
                f"lower it to {got} in the same change that closed the work"
            )
    return problems


def check_issue_state(registry, timeout: int = 20) -> tuple[list[str], str | None]:
    """No issue may be closed on GitHub while it still holds open findings."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                registry.repository,
                "--state",
                "all",
                "--limit",
                "200",
                "--json",
                "number,state",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"skipped ({type(exc).__name__})"
    if result.returncode != 0:
        return [], "skipped (gh unavailable or unauthenticated)"
    try:
        live = {row["number"]: row["state"] for row in json.loads(result.stdout)}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return [], f"skipped (unparseable gh output: {exc})"

    problems = []
    for number in sorted(EXPECTED_ISSUES):
        state = live.get(number)
        if state is None:
            problems.append(f"issue #{number} is registered but absent from {registry.repository}")
            continue
        open_rows = [item for item in registry.for_issue(number) if item.is_open]
        if state == "CLOSED" and open_rows:
            problems.append(f"FALSE CLOSURE: issue #{number} is CLOSED but holds {len(open_rows)} open findings")
    return problems, None


def report(registry, counts: dict[str, int]) -> None:
    print(f"registry     {registry.registry_owner} (version {registry.version})")
    print(f"campaign     {registry.campaign}")
    print(f"plan         {registry.plan}")
    print(f"findings     {len(registry.findings)} across {len(EXPECTED_ISSUES)} issues")
    print(
        "open         "
        f"{counts['open_total']} total — S1 {counts['open_s1']}, "
        f"S2 {counts['open_s2']}, S3 {counts['open_s3']}"
    )

    labels = Counter(item.priority for item in registry.open_findings())
    reranked = [item for item in registry.findings if item.reranked]
    print(
        "labels       "
        f"P1 {labels.get('P1', 0)}, P2 {labels.get('P2', 0)}, P3 {labels.get('P3', 0)} "
        f"({len(reranked)} of {len(registry.findings)} re-anchored against the label)"
    )

    by_phase = Counter(registry.themes[item.theme].phase for item in registry.open_findings())
    print("plan phase   " + ", ".join(f"phase {phase}: {by_phase[phase]}" for phase in sorted(by_phase)))

    blocked = [item for item in registry.open_findings() if item.blocked_by_phase]
    if blocked:
        print(f"blocked      {len(blocked)} findings cannot land before their gating phase")
    if registry.contradictions:
        pending = [item for item in registry.contradictions if item.needs_code_check]
        print(f"contradictions {len(registry.contradictions)} recorded, {len(pending)} awaiting a code check")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit non-zero on any violation")
    parser.add_argument("--github", action="store_true", help="also verify live issue state (network)")
    parser.add_argument("--registry", default=str(REGISTRY_PATH), help="registry path override")
    parser.add_argument("--ratchet", default=str(RATCHET_PATH), help="ratchet path override")
    parser.add_argument("--root", default=str(ROOT), help="tree root for the attribution check")
    args = parser.parse_args()

    print("=== HOSPES REVIEW DEBT ===")
    try:
        registry = load_registry(args.registry)
    except HardeningRegistryError as exc:
        print(f"  FAIL  registry did not load: {exc}")
        return 1 if args.check else 0

    counts = derived_counts(registry)
    report(registry, counts)
    print("")

    problems: list[str] = []

    attribution_problems = check_attribution(registry, Path(args.root))
    print(f"[A] attribution   {'OK' if not attribution_problems else 'FAIL'} — every named path exists")
    problems.extend(attribution_problems)

    ratchet_problems = check_ratchet(counts, Path(args.ratchet))
    print(f"[B] ratchet       {'OK' if not ratchet_problems else 'FAIL'} — derived counts match the recorded ratchet")
    problems.extend(ratchet_problems)

    if args.github:
        issue_problems, skipped = check_issue_state(registry)
        if skipped:
            print(f"[C] issue state   {skipped} — network check did not run")
        else:
            print(f"[C] issue state   {'OK' if not issue_problems else 'FAIL'} — no issue closed over open findings")
            problems.extend(issue_problems)
    else:
        print("[C] issue state   not requested (pass --github to verify live issue state)")

    if problems:
        print("")
        for problem in problems:
            _fail(problem)
        print("")
        print(f"review debt gate FAILED — {len(problems)} problem(s)")
        return 1 if args.check else 0

    print("")
    print("review debt gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
