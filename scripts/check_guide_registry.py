#!/usr/bin/env python3
"""Hold the demo guide registry in parity with the surfaces it explains.

The registry at ``dashboard/assets/guide.json`` claims to explain *every*
capability and *every* instrumented element.  That claim decays the moment a
capability is added without a glossary entry, so it is checked rather than
trusted.

Checks:

  A. Every capability computed by ``capabilities.mjs`` has a glossary entry,
     and the glossary invents none that the engine does not compute.
  B. Every ``data-guide`` anchor in the dashboard markup resolves to an element
     entry, and every element entry is anchored somewhere.
  C. Every tour beat names a declared view, a non-empty target, and carries
     narration for every declared depth.
  D. The two tracked dashboard trees are byte-identical.  In a source checkout
     the operator serves ``dashboard/``; an installed wheel serves
     ``hospes/resources/dashboard/``.  Nothing else enforces this, and a change
     applied to one alone ships a demo that works in development and not from
     the wheel.
  E. Every persona resolves, with a known role and a declared narration depth.

Exit 0 ⟺ all five hold.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DASHBOARD = ROOT / "dashboard"
PACKAGED_DASHBOARD = ROOT / "hospes" / "resources" / "dashboard"
GUIDE = RUNTIME_DASHBOARD / "assets" / "guide.json"
CAPABILITIES = RUNTIME_DASHBOARD / "assets" / "capabilities.mjs"

#: Keys capabilitiesFor() returns that describe session state rather than a
#: permission.  They are still documented, so they are not exempt from check A.
#: Matches both ``key: expression,`` and the ES6 shorthand ``key,`` — the
#: latter is how ``completed`` and ``writable`` are returned.
_CAPABILITY_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*[:,]", re.MULTILINE)
_DATA_GUIDE = re.compile(r'data-guide="([^"]+)"')
#: The cockpit's view tabs.  Derived from the markup rather than restated here:
#: a hand-maintained view list turns "add a view" into "edit this gate too",
#: and the gate then fails for a reason that has nothing to do with the guide.
_DATA_VIEW = re.compile(r'data-view="([^"]+)"')

failures: list[str] = []


def fail(check: str, message: str) -> None:
    failures.append(f"[{check}] {message}")


def computed_capabilities() -> set[str]:
    """Return the keys of the object capabilitiesFor() freezes and returns."""
    source = CAPABILITIES.read_text(encoding="utf-8")
    start = source.find("return Object.freeze({")
    if start < 0:
        fail("A", "capabilities.mjs no longer returns Object.freeze({...})")
        return set()
    depth = 0
    end = -1
    for index in range(source.find("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        fail("A", "could not delimit the capabilities object literal")
        return set()
    return set(_CAPABILITY_LINE.findall(source[start:end]))


def anchored_keys() -> set[str]:
    keys: set[str] = set()
    for path in sorted(RUNTIME_DASHBOARD.rglob("*.html")):
        keys |= set(_DATA_GUIDE.findall(path.read_text(encoding="utf-8")))
    return keys


def declared_views() -> set[str]:
    """Return the view ids the cockpit markup actually declares as tabs."""
    views: set[str] = set()
    for path in sorted(RUNTIME_DASHBOARD.rglob("*.html")):
        views |= set(_DATA_VIEW.findall(path.read_text(encoding="utf-8")))
    return views


def main() -> int:
    if not GUIDE.is_file():
        print(f"guide registry is missing: {GUIDE}", file=sys.stderr)
        return 1
    guide = json.loads(GUIDE.read_text(encoding="utf-8"))
    declared_capabilities = set(guide.get("capabilities", {}))
    declared_elements = set(guide.get("elements", {}))
    depths = set(guide.get("depths", {}))

    # --- A: capability glossary parity ---
    computed = computed_capabilities()
    for key in sorted(computed - declared_capabilities):
        fail("A", f"capability {key!r} is computed but has no glossary entry")
    for key in sorted(declared_capabilities - computed):
        fail("A", f"glossary documents {key!r}, which capabilities.mjs never computes")
    for key, entry in sorted(guide.get("capabilities", {}).items()):
        for field in ("label", "what", "why", "requires"):
            if not str(entry.get(field, "")).strip():
                fail("A", f"capability {key!r} is missing {field!r}")

    # --- B: element tooltip parity ---
    anchors = anchored_keys()
    for key in sorted(anchors - declared_elements):
        fail("B", f"markup anchors data-guide={key!r} with no element entry")
    for key in sorted(declared_elements - anchors):
        fail("B", f"element entry {key!r} is never anchored in the markup")
    for key, entry in sorted(guide.get("elements", {}).items()):
        for field in ("title", "body"):
            if not str(entry.get(field, "")).strip():
                fail("B", f"element {key!r} is missing {field!r}")
        referenced = entry.get("capability")
        if referenced and referenced not in declared_capabilities:
            fail("B", f"element {key!r} references unknown capability {referenced!r}")

    # --- C: tour beats ---
    beats = guide.get("beats", [])
    if not beats:
        fail("C", "the guide declares no tour beats")
    views = declared_views()
    if not views:
        fail("C", "the cockpit markup declares no data-view tabs")
    seen: set[str] = set()
    for beat in beats:
        beat_id = str(beat.get("id", ""))
        if not beat_id:
            fail("C", "a beat is missing its id")
            continue
        if beat_id in seen:
            fail("C", f"duplicate beat id {beat_id!r}")
        seen.add(beat_id)
        if beat.get("view") not in views:
            fail("C", f"beat {beat_id!r} names unknown view {beat.get('view')!r}")
        if not str(beat.get("target", "")).strip():
            fail("C", f"beat {beat_id!r} has no target selector")
        if not str(beat.get("title", "")).strip():
            fail("C", f"beat {beat_id!r} has no title")
        narration = beat.get("narration") or {}
        for depth in sorted(depths):
            if not str(narration.get(depth, "")).strip():
                fail("C", f"beat {beat_id!r} has no {depth!r} narration")

    # --- D: dashboard tree parity ---
    runtime = {
        path.relative_to(RUNTIME_DASHBOARD): path.read_bytes()
        for path in sorted(RUNTIME_DASHBOARD.rglob("*"))
        if path.is_file()
    }
    packaged = {
        path.relative_to(PACKAGED_DASHBOARD): path.read_bytes()
        for path in sorted(PACKAGED_DASHBOARD.rglob("*"))
        if path.is_file()
    }
    for relative in sorted(set(runtime) - set(packaged)):
        fail("D", f"dashboard/{relative} has no packaged counterpart")
    for relative in sorted(set(packaged) - set(runtime)):
        fail("D", f"hospes/resources/dashboard/{relative} has no runtime counterpart")
    for relative in sorted(set(runtime) & set(packaged)):
        if runtime[relative] != packaged[relative]:
            fail(
                "D",
                f"{relative} differs between dashboard/ and hospes/resources/dashboard/",
            )

    # --- F: synthetic-demo marker substitution ---
    # operator.py unhides demo markers by replacing the exact literal
    # "data-runtime-warning hidden". An attribute inserted between those two
    # leaves the marker hidden in synthetic mode with no error anywhere: the
    # bundle still says it is a demo, the page just stops showing it.
    for path in sorted(RUNTIME_DASHBOARD.rglob("*.html")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "data-runtime-warning" not in line:
                continue
            if "data-runtime-warning hidden" not in line:
                fail(
                    "F",
                    f"{path.relative_to(ROOT)}:{number} breaks the "
                    "'data-runtime-warning hidden' literal the operator "
                    "substitutes; keep those two attributes adjacent",
                )

    # --- E: personas ---
    sys.path.insert(0, str(ROOT))
    try:
        from hospes import demo_guide

        for name in demo_guide.persona_names():
            demo_guide.resolve_persona(name)
        if not demo_guide.persona_names():
            fail("E", "no personas are declared")
    except Exception as exc:  # noqa: BLE001 - reported as a gate failure
        fail("E", f"persona registry does not load: {exc}")

    if failures:
        print("guide registry check FAILED", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(
        "guide registry OK — "
        f"{len(declared_capabilities)} capabilities, "
        f"{len(declared_elements)} tooltips, "
        f"{len(beats)} beats, dashboard trees identical"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
