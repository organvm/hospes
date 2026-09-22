#!/usr/bin/env python3
"""
check_asks.py — Parse docs/ASKS-LEDGER.md, extract artifact path(s) from backtick
cells, assert each path exists under the repo root.

Additionally runs a set of CONTENT CHECKS that assert specific strings appear
inside specific files. These guard atomic-audit closure predicates that cannot
be expressed as file-existence checks alone.

Exit 0 if all artifacts present and all content checks pass; exit 1 otherwise.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

LEDGER = ROOT / "docs" / "ASKS-LEDGER.md"

# ---------------------------------------------------------------------------
# Content checks added for the 2026-07-14 atomic-audit closure.
# Each entry: (check_id, file_path_relative_to_ROOT, substring_that_must_exist)
# ---------------------------------------------------------------------------
CONTENT_CHECKS = [
    # AA1 — care profile schema file-existence is handled via ASKS table;
    #        verify the permission-matrix references care_profile_storage and
    #        the guest-ops-os doc has the delight section.
    (
        "AA1-care-profile-consent-type",
        "spec/consent.schema.json",
        "care_profile_storage",
    ),
    (
        "AA1-delight-section",
        "docs/guest-operations-os.md",
        "Guest delight and care profile",
    ),
    # AA2 — no-promotion gate: permission matrix must contain the blocked action
    #        and consent schema must contain the const: false field.
    (
        "AA2-permission-matrix-blocked",
        "spec/permission-matrix.yaml",
        "request_guest_promotion",
    ),
    (
        "AA2-consent-promotion-const",
        "spec/consent.schema.json",
        "promotion_required",
    ),
    # AA3 — claim-evidence: drafts module must define ClaimNotApprovedError
    #        and validate_claims.
    (
        "AA3-claim-not-approved-error",
        "hospes/drafts.py",
        "ClaimNotApprovedError",
    ),
    (
        "AA3-validate-claims",
        "hospes/drafts.py",
        "validate_claims",
    ),
    # AA4 — human-correction metrics: events.json must contain both new events;
    #        audit.py must contain both capture helpers.
    (
        "AA4-event-draft-edited",
        "spec/events.json",
        "draft.edited",
    ),
    (
        "AA4-event-classification-overridden",
        "spec/events.json",
        "classification.overridden",
    ),
    (
        "AA4-audit-record-draft-edited",
        "hospes/audit.py",
        "record_draft_edited",
    ),
    (
        "AA4-audit-record-classification-overridden",
        "hospes/audit.py",
        "record_classification_overridden",
    ),
    (
        "AA4-audit-human-correction-counts",
        "hospes/audit.py",
        "human_correction_counts",
    ),
    (
        "AA4-evaluation-doc-metrics-section",
        "docs/evaluation.md",
        "Human-correction metrics",
    ),
    # ROADMAP — phase-2 register must exist (file-existence via ASKS table)
    #           and must contain the 9 items.
    (
        "ROADMAP-guest-portal",
        "docs/ROADMAP.md",
        "Guest portal UI",
    ),
    (
        "ROADMAP-calendar-connector",
        "docs/ROADMAP.md",
        "Google Calendar free/busy connector",
    ),
    (
        "ROADMAP-asset-amplifier",
        "docs/ROADMAP.md",
        "Asset Amplifier",
    ),
    (
        "ROADMAP-voice-constitution",
        "docs/ROADMAP.md",
        "Voice Constitution",
    ),
    (
        "ROADMAP-outreach-templates",
        "docs/ROADMAP.md",
        "outreach templates",
    ),
    (
        "ROADMAP-city-intelligence",
        "docs/ROADMAP.md",
        "City Intelligence",
    ),
    (
        "ROADMAP-sponsor-governance",
        "docs/ROADMAP.md",
        "Sponsor creative governance",
    ),
    (
        "ROADMAP-nurture-cadence",
        "docs/ROADMAP.md",
        "Relationship nurture cadence",
    ),
    (
        "ROADMAP-distribution-tooling",
        "docs/ROADMAP.md",
        "Owned-distribution launch tooling",
    ),
    # Prior-art register — the pre-design lineage from the 2026-07-14 excavation
    # must stay registered, and its candidate tenant must stay owned in ROADMAP.
    (
        "PRIOR-ART-register",
        "docs/prior-art.md",
        "Podcast Livestream Narrative Framework",
    ),
    (
        "PRIOR-ART-field-show-precursor",
        "docs/prior-art.md",
        "road-trip travel podcast",
    ),
    (
        "PRIOR-ART-estate-boundary",
        "docs/prior-art.md",
        "podcast engine",
    ),
    (
        "ROADMAP-amp-lab-tenant",
        "docs/ROADMAP.md",
        "AMP LAB MEDIA",
    ),
]


def parse_artifacts(cell: str) -> list[str]:
    """Extract all `backtick` values from a table cell."""
    return re.findall(r"`([^`]+)`", cell)


def run_content_checks() -> list[tuple[str, str, str]]:
    """Run all content checks. Return list of (check_id, file, needle) failures."""
    failures = []
    for check_id, rel_path, needle in CONTENT_CHECKS:
        path = ROOT / rel_path
        if not path.exists():
            print(f"  MISSING FILE  [{check_id}] {rel_path}")
            failures.append((check_id, rel_path, needle))
            continue
        text = path.read_text(encoding="utf-8")
        if needle in text:
            print(f"  OK  [{check_id}] '{needle}' in {rel_path}")
        else:
            print(f"  MISSING CONTENT  [{check_id}] '{needle}' not found in {rel_path}")
            failures.append((check_id, rel_path, needle))
    return failures


def main() -> int:
    """Check an explicitly selected in-repository ledger and its declared artifacts.

    Preserve the historical default rather than falling back to public inputs.
    Return one for missing, empty, escaping, or content-incomplete evidence.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default="docs/ASKS-LEDGER.md")
    args = parser.parse_args()
    ledger = (ROOT / args.ledger).resolve()
    if not ledger.is_relative_to(ROOT.resolve()):
        print("ledger must remain inside the repository")
        return 1
    if not ledger.is_file():
        print(f"MISSING: {ledger.relative_to(ROOT.resolve())}")
        return 1

    lines = ledger.read_text().splitlines()

    # Find table rows: lines that start with |, skip header and separator rows
    rows = []
    in_table = False
    header_skipped = False
    separator_skipped = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            if not in_table:
                in_table = True
                # This is the header row
                header_skipped = True
                continue
            if header_skipped and not separator_skipped:
                # This is the separator row (---|---|...)
                separator_skipped = True
                continue
            rows.append(stripped)
        else:
            if in_table:
                in_table = False
                header_skipped = False
                separator_skipped = False

    failures = []
    total = 0

    for row in rows:
        cols = [c.strip() for c in row.strip("|").split("|")]
        if len(cols) < 3:
            continue
        ask_id = cols[0].strip()
        artifact_cell = cols[2].strip() if len(cols) > 2 else ""
        artifacts = parse_artifacts(artifact_cell)

        for artifact in artifacts:
            total += 1
            path = (ROOT / artifact).resolve()
            if not path.is_relative_to(ROOT.resolve()):
                print(f"  OUTSIDE REPOSITORY  [{ask_id}] {artifact}")
                failures.append((ask_id, artifact))
                continue
            if path.exists():
                print(f"  OK  [{ask_id}] {artifact}")
            else:
                print(f"  MISSING  [{ask_id}] {artifact}")
                failures.append((ask_id, artifact))

    print()
    print(f"Checked {total} artifact(s). {len(failures)} missing.")

    # --- Content checks ---
    print()
    print("=== Content checks ===")
    content_failures = run_content_checks()
    print()
    print(f"Ran {len(CONTENT_CHECKS)} content check(s). {len(content_failures)} failed.")

    if failures:
        print("\nFailed artifact checks:")
        for ask_id, artifact in failures:
            print(f"  {ask_id}: {artifact}")

    if content_failures:
        print("\nFailed content checks:")
        for check_id, rel_path, needle in content_failures:
            print(f"  {check_id}: '{needle}' not in {rel_path}")

    if total == 0:
        print("ledger must declare at least one artifact")
        return 1

    if failures or content_failures:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
