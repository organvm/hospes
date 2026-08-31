"""Phase 0 synthetic launch, bootstrap, CSV, and cleanup acceptance."""

from __future__ import annotations

import csv
import io
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hospes import candidate_import, operator, store, synthetic_demo
from hospes.paths import DATA_DIR

TOKEN = "synthetic-phase-zero-token"  # allow-secret: inert test fixture
SESSION_SECRET = "synthetic-phase-zero-session"  # allow-secret: inert test fixture


def build_app(
    tmp_path: Path,
    *,
    scenario: str = "review_ready",
    nonce: str | None = "one-time-synthetic-bootstrap",
    expires_at: float | None = None,
):
    bundle = tmp_path / scenario
    synthetic_demo.seed_synthetic_demo(bundle, allowed_demo_dir=bundle)
    config = synthetic_demo.SCENARIOS[scenario]
    return operator.create_operator_app(
        db_path=str(bundle / str(config["filename"])),
        auth_token=TOKEN,  # allow-secret: inert test fixture
        actor_id="synthetic_operator",
        role="producer",
        tenant_id=str(config["tenant_id"]),
        session_secret=SESSION_SECRET,  # allow-secret: inert test fixture
        synthetic_demo=True,
        bootstrap_nonce=nonce,
        bootstrap_expires_at=(time.time() + 30 if expires_at is None and nonce else expires_at),
        demo_pipeline_path=DATA_DIR / "demo-pipeline.csv",
    )


def test_one_time_bootstrap_scrubs_bearer_and_unlocks_demo_csv(tmp_path: Path) -> None:
    nonce = "one-time-synthetic-bootstrap"
    app = build_app(tmp_path, nonce=nonce)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login = client.get("/operator/login")
        assert TOKEN not in login.text
        assert login.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
        assert client.get("/operator/demo-pipeline.csv").status_code == 401
        bootstrap = client.post("/operator/bootstrap", json={"nonce": nonce})
        assert bootstrap.status_code == 200
        assert TOKEN not in bootstrap.text
        assert "httponly" in bootstrap.headers["set-cookie"].lower()
        assert "samesite=strict" in bootstrap.headers["set-cookie"].lower()
        csv_response = client.get("/operator/demo-pipeline.csv")
        assert csv_response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(csv_response.text)))
        assert len(rows) == 5
        assert [row["guest_name"].split()[-1] for row in rows] == [
            "Mortarboard",
            "Punchclock",
            "Quibble",
            "Sidecar",
            "Crumbweather",
        ]
        assert client.post("/operator/bootstrap", json={"nonce": nonce}).status_code == 401


def test_bootstrap_rejects_expiry_and_wrong_host(tmp_path: Path) -> None:
    wrong_host_app = build_app(tmp_path / "wrong-host")
    with TestClient(wrong_host_app, base_url="http://localhost") as client:
        response = client.post(
            "/operator/bootstrap", json={"nonce": "one-time-synthetic-bootstrap"}
        )
        assert response.status_code == 400

    expired_app = build_app(
        tmp_path / "expired",
        nonce="expired-synthetic-bootstrap",
        expires_at=time.time() - 1,
    )
    with TestClient(expired_app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/operator/bootstrap", json={"nonce": "expired-synthetic-bootstrap"}
        )
        assert response.status_code == 401


def test_demo_csv_is_importer_valid_and_has_three_eligible_rows(tmp_path: Path) -> None:
    connection = store.connect(tmp_path / "import.sqlite3")
    try:
        result = candidate_import.import_candidate_file(
            connection,
            DATA_DIR / "demo-pipeline.csv",
            tenant_id="synthetic_tenant",
            network_id="synthetic_network",
            show_id="synthetic_show",
            actor_id="synthetic_operator",
            actor_role="producer",
        )
        connection.commit()
        rows = store.fetch_all(connection, "SELECT * FROM appearance_opportunities")
    finally:
        connection.close()
    assert result.created == 5
    eligible = [
        row
        for row in rows
        if row["relationship_class"] in {"C2", "C3"}
        and row["preferred_city"] in {"Los Angeles", "New York City", "Austin"}
        and row["social_cost_1_5"] <= 2
    ]
    assert len(eligible) == 3
    assert sum(row["relationship_class"] in {"C4", "C5"} for row in rows) == 1
    assert all(row["relationship_owner"].startswith("demo_owner_") for row in rows)


def test_completed_specimen_has_no_demo_csv_route(tmp_path: Path) -> None:
    app = build_app(tmp_path, scenario="complete", nonce=None)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login = client.post(
            "/operator/session",
            data={"token": TOKEN},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert client.get("/operator/demo-pipeline.csv").status_code == 404


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_no_browser_launch_is_loopback_only_and_cleans_temp_bundle(
    termination_signal: signal.Signals,
) -> None:
    root = Path(__file__).resolve().parents[1]
    temp_root = Path(tempfile.gettempdir())
    before = set(temp_root.glob("hospes-synthetic-demo-*"))
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(root),
            "PYTHONUNBUFFERED": "1",
            "HOSPES_OPERATOR_TOKEN": TOKEN,
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hospes",
            "demo",
            "--open",
            "--no-browser",
            "--port",
            "0",
        ],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    deadline = time.monotonic() + 20
    assert process.stdout is not None
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.25)
        if ready:
            line = process.stdout.readline()
            output.append(line)
            if "press Ctrl+C" in line:
                break
        if process.poll() is not None:
            break
    joined = "".join(output)
    assert "http://127.0.0.1:" in joined, joined
    assert TOKEN not in joined
    process.send_signal(termination_signal)
    remainder, _ = process.communicate(timeout=10)
    assert process.returncode == 0, joined + remainder
    after = set(temp_root.glob("hospes-synthetic-demo-*"))
    assert after == before
