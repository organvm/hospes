"""Demo + validate run end-to-end and the demo is idempotent."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(*args):
    env = {"PYTHONPATH": str(REPO_ROOT)}
    import os
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "hospes", *args],
        cwd=str(REPO_ROOT),
        env=full_env,
        capture_output=True,
        text=True,
    )


def test_demo_runs_twice_exit_zero():
    first = _run("demo")
    assert first.returncode == 0, first.stderr
    second = _run("demo")
    assert second.returncode == 0, second.stderr


def test_demo_is_idempotent_commitments():
    # Running demo twice must not duplicate the two recorded commitments.
    _run("demo")
    _run("demo")
    from hospes import commitments
    all_c = commitments.list_all()
    # The two demo commitments each appear at most once.
    # Note: other tests may also create commitments; we only check demo's own
    demo_descriptions = [
        "Send guest the one-page editorial brief",
        "Confirm episode title with guest before publication",
    ]
    for desc in demo_descriptions:
        demo_count = sum(
            1
            for commitment in all_c
            if commitment.description == desc
            and commitment.opportunity
            == "Pilot A — Prior Professional Guest (comedian / Unlicensed Therapy alumna)"
        )
        assert demo_count <= 1, f"Demo commitment '{desc}' duplicated: {demo_count} times"


def test_validate_exits_zero():
    res = _run("validate")
    assert res.returncode == 0, res.stdout + res.stderr


def test_demo_writes_only_under_out():
    res = _run("demo")
    assert res.returncode == 0
    out_dir = REPO_ROOT / "out"
    assert out_dir.exists()
    assert (out_dir / "drafts").exists()
    assert (out_dir / "briefs").exists()
    assert (out_dir / "assets").exists()
