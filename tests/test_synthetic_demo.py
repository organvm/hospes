"""Safety, lifecycle, operator, and launch tests for the synthetic Host demo."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hospes import generation, operator, partnerships, pilot_service, store
from hospes.synthetic_demo import (
    BUILD_RECEIPT_NAME,
    BUILD_RECEIPT_VERSION,
    CANONICAL_DB,
    INSTALL_PHASES,
    JOURNAL_NAME,
    MARKER_KEY,
    SCENARIOS,
    SyntheticDemoError,
    acquire_demo_lease,
    probe_synthetic_marker,
    read_synthetic_marker,
    seed_synthetic_demo,
    validate_synthetic_bundle,
)

TOKEN = "synthetic-demo-operator-token"
SESSION_SECRET = "synthetic-demo-session-secret"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(tmp_path: Path, *, now: datetime | None = None) -> tuple[Path, dict]:
    demo_dir = tmp_path / "private_pilot" / "demo"
    receipt = seed_synthetic_demo(
        demo_dir,
        now=now or datetime.now(UTC),
        allowed_demo_dir=demo_dir,
    )
    return demo_dir, receipt


def database_for(demo_dir: Path, scenario: str) -> Path:
    return demo_dir / str(SCENARIOS[scenario]["filename"])


def test_factory_builds_review_ready_and_complete_through_domain_services(
    tmp_path: Path,
) -> None:
    generated_at = datetime.now(UTC)
    demo_dir, receipt = build(tmp_path, now=generated_at)
    review_path = database_for(demo_dir, "review_ready")
    complete_path = database_for(demo_dir, "complete")

    assert stat.S_IMODE(demo_dir.stat().st_mode) == 0o700
    for path in (review_path, complete_path, demo_dir / BUILD_RECEIPT_NAME):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    review_marker = read_synthetic_marker(review_path)
    assert review_marker["tenant_id"] == "ari_demo_review"
    assert review_marker["marker_version"] == 2
    assert review_marker["receipt_version"] == BUILD_RECEIPT_VERSION
    assert review_marker["runtime_kind"] == "synthetic_demo"
    assert review_marker["scenario"] == "review_ready"
    assert review_marker["generation_id"] == receipt["generation_id"]
    assert review_marker["generated_at"] == receipt["generated_at"]
    assert read_synthetic_marker(complete_path)["scenario"] == "complete"

    review = store.connect(review_path)
    review_partnership = partnerships.list_partnerships(
        review, "ari_demo_review"
    )[0]
    review_center = partnerships.command_center(
        review, review_partnership["id"], "ari_demo_review"
    )
    names = [
        row[0]
        for row in review.execute(
            "SELECT guest_name FROM appearance_opportunities ORDER BY guest_name"
        ).fetchall()
    ]
    assert len(names) == 5
    assert all(name.startswith("[SYNTHETIC]") for name in names)
    assert review.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert review.execute(
        "SELECT COUNT(*) FROM appearance_opportunities "
        "WHERE relationship_class IN ('C2', 'C3') AND social_cost_1_5 <= 2"
    ).fetchone()[0] == 3
    routes = review.execute(
        "SELECT route_label, source_provenance, verified_at FROM contact_routes"
    ).fetchall()
    assert all(row[0].startswith("fixture://") for row in routes)
    assert all(row[1].startswith("fixture://") for row in routes)
    assert all(datetime.fromisoformat(row[2]) <= generated_at for row in routes)
    assert review_center["pilot_readiness"]["met"] == 1
    assert review_center["pilot_readiness"]["total"] == 14
    assert review_center["pilot_execution"]["latest_run"] is None
    policy = review_center["pilot_execution"]["current_policy"]
    assert datetime.fromisoformat(policy["deadline_at"]) == generated_at + timedelta(
        days=14
    )
    review.close()

    complete = store.connect(complete_path)
    complete_partnership = partnerships.list_partnerships(
        complete, "ari_demo_complete"
    )[0]
    complete_center = partnerships.command_center(
        complete, complete_partnership["id"], "ari_demo_complete"
    )
    assert complete_center["pilot_readiness"]["met"] == 14
    assert complete_center["pilot_readiness"]["total"] == 14
    assert complete_center["pilot_readiness"]["pilot_complete"] is True
    latest = complete_center["pilot_execution"]["latest_run"]
    assert latest["lifecycle_state"] == "COMPLETED"
    completed_plan = pilot_service.get_plan(
        complete,
        complete_partnership["id"],
        latest["id"],
        tenant_id="ari_demo_complete",
        actor_id="ari_demo_owner",
        actor_role="relationship_owner",
        now=generated_at,
    )
    assert completed_plan["lifecycle_state"] == "COMPLETED"
    assert completed_plan["risk_level"] == "complete"
    assert complete.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 5
    assert complete.execute("SELECT COUNT(*) FROM pilot_decisions").fetchone()[0] == 1
    assert complete.execute("SELECT COUNT(*) FROM correspondence_drafts").fetchone()[0] == 1
    persisted_body = complete.execute(
        "SELECT body FROM correspondence_drafts"
    ).fetchone()[0]
    assert persisted_body == (
        "[CORRESPONDENCE BODY NOT PERSISTED — EXTERNAL OWNER REQUIRED]"
    )
    receipt_rows = complete.execute(
        "SELECT external_reference, occurred_at FROM operational_receipts"
    ).fetchall()
    assert receipt_rows
    assert all(row[0].startswith("fixture://") for row in receipt_rows)
    assert all(datetime.fromisoformat(row[1]) <= generated_at for row in receipt_rows)
    complete.close()

    receipt_text = (demo_dir / BUILD_RECEIPT_NAME).read_text(encoding="utf-8")
    assert json.loads(receipt_text) == receipt
    assert receipt["receipt_version"] == BUILD_RECEIPT_VERSION
    assert all("path" not in item for item in receipt["builds"])
    assert validate_synthetic_bundle(demo_dir)["receipt"] == receipt
    assert "Bixby" not in receipt_text
    assert "Mortarboard" not in receipt_text
    assert "@" not in receipt_text


def test_rebuild_is_clean_and_replacement_is_marker_guarded(tmp_path: Path) -> None:
    generated_at = datetime.now(UTC)
    demo_dir, first = build(tmp_path, now=generated_at)
    second = seed_synthetic_demo(
        demo_dir,
        replace_demo=True,
        now=generated_at,
        allowed_demo_dir=demo_dir,
    )
    assert [
        (item["scenario"], item["candidate_counts"], item["readiness"], item["run_state"])
        for item in first["builds"]
    ] == [
        (item["scenario"], item["candidate_counts"], item["readiness"], item["run_state"])
        for item in second["builds"]
    ]
    for scenario in SCENARIOS:
        connection = sqlite3.connect(database_for(demo_dir, scenario))
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        connection.close()

    with pytest.raises(SyntheticDemoError, match="--replace-demo"):
        seed_synthetic_demo(demo_dir, allowed_demo_dir=demo_dir)

    review_path = database_for(demo_dir, "review_ready")
    review_path.unlink()
    unmarked = sqlite3.connect(review_path)
    unmarked.execute("CREATE TABLE authority_fixture (value TEXT)")
    unmarked.execute("INSERT INTO authority_fixture VALUES ('preserve-me')")
    unmarked.commit()
    unmarked.close()
    before = sha256(review_path)
    with pytest.raises(SyntheticDemoError, match="not a marked|not marked"):
        seed_synthetic_demo(
            demo_dir,
            replace_demo=True,
            allowed_demo_dir=demo_dir,
        )
    assert sha256(review_path) == before


def test_generation_scope_preserves_live_uuid4_and_reproducible_bundle_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Byte reproducibility is a property of a bundle with no encrypted private
    # layer. Private-field custody seeds contact rosters and touchpoints whose
    # nonces are random by construction, so the environment is pinned here
    # rather than left to inherit an ambient key and fail intermittently.
    monkeypatch.delenv("HOSPES_MASTER_KEY_B64", raising=False)
    live_ids = [generation.new_id("live-test") for _ in range(2)]
    assert live_ids[0] != live_ids[1]
    assert all(UUID(value).version == 4 for value in live_ids)
    first_live_time = generation.now()
    second_live_time = generation.now()
    assert second_live_time >= first_live_time

    generated_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = seed_synthetic_demo(
        first_dir, now=generated_at, allowed_demo_dir=first_dir
    )
    second = seed_synthetic_demo(
        second_dir, now=generated_at, allowed_demo_dir=second_dir
    )
    assert first == second
    for config in SCENARIOS.values():
        name = str(config["filename"])
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()
    assert (first_dir / BUILD_RECEIPT_NAME).read_bytes() == (
        second_dir / BUILD_RECEIPT_NAME
    ).read_bytes()

    generation_id = generation.deterministic_generation_id(generated_at, 2)
    with generation.deterministic_scope(generated_at, generation_id):
        scoped = [generation.new_id("fixture-row") for _ in range(2)]
        assert generation.now() == generated_at
    with generation.deterministic_scope(generated_at, generation_id):
        assert scoped == [generation.new_id("fixture-row") for _ in range(2)]
    assert all(UUID(value).version == 5 for value in scoped)


def test_bundle_lock_supports_shared_operators_and_rejects_build_contention(
    tmp_path: Path,
) -> None:
    demo_dir, _receipt = build(tmp_path)
    first = acquire_demo_lease(demo_dir, shared=True)
    second = acquire_demo_lease(demo_dir, shared=True)
    try:
        with pytest.raises(SyntheticDemoError, match="leased"):
            seed_synthetic_demo(
                demo_dir,
                replace_demo=True,
                allowed_demo_dir=demo_dir,
            )
    finally:
        second.close()
        first.close()
    seed_synthetic_demo(
        demo_dir,
        replace_demo=True,
        allowed_demo_dir=demo_dir,
    )


@pytest.mark.parametrize("phase", INSTALL_PHASES)
def test_every_install_journal_phase_recovers_or_commits(
    tmp_path: Path, phase: str
) -> None:
    demo_dir = tmp_path / phase
    frozen = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    seed_synthetic_demo(demo_dir, now=frozen, allowed_demo_dir=demo_dir)
    paths = [
        database_for(demo_dir, "review_ready"),
        database_for(demo_dir, "complete"),
        demo_dir / BUILD_RECEIPT_NAME,
    ]
    before = [sha256(path) for path in paths]
    with pytest.raises(SyntheticDemoError, match="injected"):
        seed_synthetic_demo(
            demo_dir,
            replace_demo=True,
            now=frozen + timedelta(minutes=1),
            allowed_demo_dir=demo_dir,
            _failure_phase=phase,
        )
    assert not (demo_dir / JOURNAL_NAME).exists()
    if phase == "committed":
        assert [sha256(path) for path in paths] != before
    else:
        assert [sha256(path) for path in paths] == before
    validate_synthetic_bundle(demo_dir)


def test_bundle_rejects_forgery_mixed_generations_and_file_drift(
    tmp_path: Path,
) -> None:
    def fresh(name: str) -> Path:
        directory = tmp_path / name
        seed_synthetic_demo(directory, allowed_demo_dir=directory)
        return directory

    forged = fresh("forged")
    forged_complete = database_for(forged, "complete")
    connection = sqlite3.connect(forged_complete)
    marker = json.loads(
        connection.execute(
            "SELECT metadata_value FROM runtime_metadata WHERE metadata_key = ?",
            (MARKER_KEY,),
        ).fetchone()[0]
    )
    marker["generated_at"] = (
        datetime.fromisoformat(marker["generated_at"]) + timedelta(seconds=1)
    ).isoformat()
    connection.execute(
        "UPDATE runtime_metadata SET metadata_value = ? WHERE metadata_key = ?",
        (json.dumps(marker, sort_keys=True), MARKER_KEY),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SyntheticDemoError, match="generation binding"):
        validate_synthetic_bundle(forged)

    mixed = fresh("mixed")
    other = tmp_path / "other-generation"
    seed_synthetic_demo(
        other,
        now=datetime.now(UTC) + timedelta(minutes=1),
        allowed_demo_dir=other,
    )
    database_for(mixed, "complete").write_bytes(
        database_for(other, "complete").read_bytes()
    )
    with pytest.raises(SyntheticDemoError, match="mixes generations"):
        validate_synthetic_bundle(mixed)

    migrated = fresh("migration")
    review = database_for(migrated, "review_ready")
    connection = sqlite3.connect(review)
    connection.execute(
        "UPDATE schema_migrations SET name = 'forged' WHERE version = 7"
    )
    connection.commit()
    connection.close()
    with pytest.raises(SyntheticDemoError, match="migration ledger"):
        validate_synthetic_bundle(migrated)

    checksummed = fresh("checksum")
    with database_for(checksummed, "complete").open("ab") as handle:
        handle.write(b"checksum-drift")
    with pytest.raises(SyntheticDemoError, match="checksum drifted"):
        validate_synthetic_bundle(checksummed)

    sidecar = fresh("sidecar")
    Path(f"{database_for(sidecar, 'review_ready')}-wal").touch()
    with pytest.raises(SyntheticDemoError, match="sidecar"):
        validate_synthetic_bundle(sidecar)

    linked = fresh("symlink")
    linked_review = database_for(linked, "review_ready")
    preserved = linked / "preserved-review.sqlite3"
    linked_review.rename(preserved)
    linked_review.symlink_to(preserved.name)
    with pytest.raises(SyntheticDemoError, match="regular file"):
        validate_synthetic_bundle(linked)


def test_bundle_rejects_receipt_generation_and_checksum_encoding_forgery(
    tmp_path: Path,
) -> None:
    generated_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    demo_dir, _receipt = build(tmp_path, now=generated_at)
    receipt_path = demo_dir / BUILD_RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["generation_id"] = str(uuid4())
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SyntheticDemoError, match="generation binding"):
        validate_synthetic_bundle(demo_dir)

    encoded = tmp_path / "checksum-encoding"
    seed_synthetic_demo(encoded, now=generated_at, allowed_demo_dir=encoded)
    encoded_receipt_path = encoded / BUILD_RECEIPT_NAME
    encoded_receipt = json.loads(
        encoded_receipt_path.read_text(encoding="utf-8")
    )
    encoded_receipt["builds"][0]["sha256"] = "A" * 64
    encoded_receipt_path.write_text(
        json.dumps(encoded_receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SyntheticDemoError, match="malformed"):
        validate_synthetic_bundle(encoded)


def test_bundle_rejects_foreign_key_and_sqlite_integrity_failures(
    tmp_path: Path,
) -> None:
    foreign = tmp_path / "foreign-key"
    seed_synthetic_demo(foreign, allowed_demo_dir=foreign)
    foreign_review = database_for(foreign, "review_ready")
    connection = sqlite3.connect(foreign_review)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO decisions "
        "(id, tenant_id, opportunity_id, action, actor_id, actor_role, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "forged-decision",
            "ari_demo_review",
            "missing-opportunity",
            "approve",
            "forged-actor",
            "relationship_owner",
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SyntheticDemoError, match="foreign-key"):
        validate_synthetic_bundle(foreign)

    corrupt = tmp_path / "integrity"
    seed_synthetic_demo(corrupt, allowed_demo_dir=corrupt)
    corrupt_review = database_for(corrupt, "review_ready")
    connection = sqlite3.connect(corrupt_review)
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    root_page = int(
        connection.execute(
            "SELECT rootpage FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'decisions'"
        ).fetchone()[0]
    )
    connection.close()
    with corrupt_review.open("r+b") as handle:
        handle.seek((root_page - 1) * page_size)
        handle.write(b"\x00")
    with pytest.raises(SyntheticDemoError, match="integrity check"):
        validate_synthetic_bundle(corrupt)


def test_factory_refuses_other_directories_and_preserves_sibling_authority(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private_pilot"
    private.mkdir()
    canonical = private / "hospes.sqlite3"
    protected_csv = private / "ari-pilot-1-candidates.csv"
    canonical.write_bytes(b"canonical-authority-fixture")
    protected_csv.write_bytes(b"protected-candidate-fixture")
    before = (sha256(canonical), sha256(protected_csv))
    demo_dir = private / "demo"
    seed_synthetic_demo(
        demo_dir,
        allowed_demo_dir=demo_dir,
        now=datetime.now(UTC),
    )
    assert (sha256(canonical), sha256(protected_csv)) == before

    outside = tmp_path / "outside"
    with pytest.raises(SyntheticDemoError, match="private demo directory"):
        seed_synthetic_demo(outside, allowed_demo_dir=demo_dir)
    with pytest.raises(SyntheticDemoError, match="real directory"):
        seed_synthetic_demo(canonical, allowed_demo_dir=canonical)
    with pytest.raises(SyntheticDemoError, match="canonical Pilot database"):
        seed_synthetic_demo(CANONICAL_DB, allowed_demo_dir=CANONICAL_DB)


def test_synthetic_operator_marker_banner_context_and_completed_immutability(
    tmp_path: Path,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    demo_dir, _receipt = build(tmp_path)
    review_path = database_for(demo_dir, "review_ready")
    app = operator.create_operator_app(
        db_path=str(review_path),
        auth_token=TOKEN,
        actor_id="ari_demo_owner",
        role="relationship_owner",
        tenant_id="ari_demo_review",
        session_secret=SESSION_SECRET,
        synthetic_demo=True,
    )
    with TestClient(app) as client:
        login = client.get("/operator/login")
        assert login.text.count("SYNTHETIC DEMO — NON-AUTHORITATIVE DATA") == 1
        client.post(
            "/operator/session", data={"token": TOKEN}, follow_redirects=False
        )
        dashboard = client.get("/operator/")
        # One persistent restraint: the mode chip is the dashboard's single
        # synthetic surface; the server-rendered page carries it as the
        # data-guide marker and no competing warning banners.
        assert 'class="mode-badge live" id="mode-badge" data-guide="synthetic-marker"' in dashboard.text
        assert dashboard.text.count("SYNTHETIC DEMO — NON-AUTHORITATIVE DATA") == 0
        show_headers = {"X-Session-Show": "synthetic_private_pilot"}
        context = client.get(
            "/operator/api/operator-context", headers=show_headers
        ).json()
        assert context["runtime_kind"] == "synthetic_demo"
        assert context["demo_scenario"] == "review_ready"
        assert context["role"] == "relationship_owner"
        assert context["scenario_label"] == "Synthetic review ready specimen"
        assert str(review_path) not in json.dumps(context)
        assert client.get("/v1/operator-context").status_code == 404
        opportunities = client.get(
            "/operator/api/opportunities", headers=show_headers
        ).json()
        written = client.post(
            f"/operator/api/opportunities/{opportunities[0]['id']}/decisions",
            json={"action": "approve"},
            headers={
                "Authorization": "Bearer browser-forgery",
                "X-Hospes-Actor": "browser-forgery",
                "X-Hospes-Role": "host",
                "X-Hospes-Tenant": "browser-forgery",
                "X-Hospes-Anything": "strip-me",
                "X-Hospes-CSRF": client.cookies.get("hospes_csrf"),
                "X-Session-Show": "synthetic_private_pilot",
            },
        )
        assert written.status_code == 200
        assert written.json()["decisions"][-1]["actor_id"] == "ari_demo_owner"
        for name in (
            "cache-control",
            "content-security-policy",
            "permissions-policy",
            "referrer-policy",
            "strict-transport-security",
            "x-content-type-options",
            "x-frame-options",
        ):
            assert name in client.get("/health").headers
    validate_synthetic_bundle(demo_dir)

    complete_path = database_for(demo_dir, "complete")
    complete_app = operator.create_operator_app(
        db_path=str(complete_path),
        auth_token=TOKEN,
        actor_id="ari_demo_owner",
        role="relationship_owner",
        tenant_id="ari_demo_complete",
        session_secret=SESSION_SECRET,
        synthetic_demo=True,
    )
    with TestClient(complete_app) as client:
        client.post(
            "/operator/session", data={"token": TOKEN}, follow_redirects=False
        )
        assert client.get("/operator/").status_code == 200
        show_headers = {"X-Session-Show": "synthetic_private_pilot"}
        assert client.get(
            "/operator/api/opportunities", headers=show_headers
        ).status_code == 200
        assert complete_app.state.conn.execute(
            "PRAGMA query_only"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            complete_app.state.conn.execute(
                "INSERT INTO runtime_metadata VALUES ('x','x','x','{}','x')"
            )
        rejected = client.post(
            "/operator/api/opportunities", json={}, headers=show_headers
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == (
            "the completed synthetic specimen is read-only"
        )

    unmarked = tmp_path / "unmarked.sqlite3"
    store.connect(unmarked).close()
    with pytest.raises(operator.OperatorConfigError, match="not marked|not a marked"):
        operator.create_operator_app(
            db_path=str(unmarked),
            auth_token=TOKEN,
            actor_id="ari_demo_owner",
            role="relationship_owner",
            tenant_id="ari_demo_review",
            synthetic_demo=True,
        )
    with pytest.raises(operator.OperatorConfigError, match="tenant"):
        operator.create_operator_app(
            db_path=str(review_path),
            auth_token=TOKEN,
            actor_id="ari_demo_owner",
            role="relationship_owner",
            tenant_id="ari_demo_complete",
            synthetic_demo=True,
        )
    with pytest.raises(operator.OperatorConfigError, match="raw /v1"):
        operator.create_operator_app(
            db_path=str(review_path),
            auth_token=TOKEN,
            actor_id="ari_demo_owner",
            role="relationship_owner",
            tenant_id="ari_demo_review",
            synthetic_demo=True,
            enable_raw_v1=True,
        )
    with pytest.raises(operator.OperatorConfigError, match="requires --synthetic-demo"):
        operator.create_operator_app(
            db_path=str(review_path),
            auth_token=TOKEN,
            actor_id="ari_demo_owner",
            role="relationship_owner",
            tenant_id="ari_demo_review",
        )

    secure_app = operator.create_operator_app(
        db_path=str(review_path),
        auth_token=TOKEN,
        actor_id="ari_demo_owner",
        role="relationship_owner",
        tenant_id="ari_demo_review",
        session_secret=SESSION_SECRET,
        synthetic_demo=True,
        secure_session_cookie=True,
    )
    with TestClient(secure_app) as client:
        response = client.post(
            "/operator/session", data={"token": TOKEN}, follow_redirects=False
        )
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Secure" in cookie


def test_operator_rejects_symlink_database_paths_before_sqlite(
    tmp_path: Path,
) -> None:
    pytest.importorskip("fastapi")
    authority = tmp_path / "authority.sqlite3"
    store.connect(authority).close()
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(authority.name)
    with pytest.raises(operator.OperatorConfigError, match="cannot use symlinks"):
        operator.create_operator_app(
            db_path=str(linked),
            auth_token=TOKEN,
            actor_id="fixture_owner",
            role="relationship_owner",
            tenant_id="fixture_tenant",
        )

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    nested_authority = real_parent / "authority.sqlite3"
    store.connect(nested_authority).close()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent.name, target_is_directory=True)
    with pytest.raises(operator.OperatorConfigError, match="cannot use symlinks"):
        operator.create_operator_app(
            db_path=str(linked_parent / nested_authority.name),
            auth_token=TOKEN,
            actor_id="fixture_owner",
            role="relationship_owner",
            tenant_id="fixture_tenant",
        )

    assert probe_synthetic_marker(authority) is None


def test_planner_ui_and_cloudflare_scripts_preserve_authority_boundaries() -> None:
    root = Path(__file__).resolve().parents[1]
    index = "\n".join(
        (root / "dashboard" / relative).read_text(encoding="utf-8")
        for relative in ("index.html", "assets/partnership-workspace.html")
    )
    login = (root / "dashboard" / "login.html").read_text(encoding="utf-8")
    partnership_js = "\n".join(
        (root / "dashboard" / "assets" / name).read_text(encoding="utf-8")
        for name in ("partnership.js", "partnership-workspace.js")
    )
    api_js = (root / "dashboard" / "assets" / "api.js").read_text(
        encoding="utf-8"
    )
    setup = (root / "scripts" / "setup-synthetic-demo-cloudflare.sh").read_text(
        encoding="utf-8"
    )
    start = (root / "scripts" / "start-synthetic-demo-cloudflare.sh").read_text(
        encoding="utf-8"
    )
    configure = (
        root / "scripts" / "configure_synthetic_demo_cloudflare.py"
    ).read_text(encoding="utf-8")
    helpers = "\n".join(
        (root / "dashboard" / "assets" / name).read_text(encoding="utf-8")
        for name in (
            "capabilities.mjs",
            "planner.mjs",
            "payloads.mjs",
            "clipboard.mjs",
        )
    )

    assert 'data-guide="synthetic-marker"' in index
    assert "Synthetic" in partnership_js
    assert "SYNTHETIC DEMO — NON-AUTHORITATIVE DATA" in login
    for label in (
        "Ranked action",
        "Assignments and timing",
        "Alternatives",
        "Constraints",
        "Rationale",
    ):
        assert label in index
    for kind in (
        "wait",
        "activate_set",
        "follow_up",
        "promote",
        "fallback_rehearsal",
        "pause",
    ):
        assert kind in partnership_js + helpers
    assert "startPilotRun" in api_js
    assert "loadPilotPlan" in api_js
    assert "savePilotDecision" in api_js
    assert "capabilities.canDraft" in partnership_js
    assert "capabilities.completed" in partnership_js
    assert "rankedDecisionKind" in partnership_js
    assert "shouldRequestPilotPlan" in partnership_js
    assert "access-ready.json" in start
    assert start.index("access-ready.json") < start.index("cloudflared tunnel")
    assert "http_status:404" in configure
    assert "127.0.0.1:8765" in configure
    assert "127.0.0.1:8766" in configure
    assert "Quick Tunnel" not in setup + start + configure
    assert "trycloudflare.com" not in setup + start + configure
    assert "LaunchAgent" not in setup + start + configure
    assert "HOSPES_OPERATOR_TOKEN" not in configure
    assert "CLOUDFLARE_API_TOKEN" in configure
    assert "policies" in configure and '"email"' in configure
    assert "default_deny" in configure
    assert "--verify-only" in setup + configure
    assert "hospes.cloudflare-access-receipt.v1" in configure
    assert "process-ledger.json" in start
    assert "--secure-session-cookie" in start


def test_private_custody_keeps_the_completed_specimen_checksum_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Encryption may only cost determinism where the docs say it may.

    With custody configured the review specimen gains encrypted rosters and
    touchpoints, so its bytes legitimately differ between builds — random
    nonces are a security requirement, not a defect. The completed specimen
    carries no private layer and must stay byte-identical, because it is the
    checksum-bound artifact the bundle validator pins.
    """
    monkeypatch.setenv(
        "HOSPES_MASTER_KEY_B64", base64.b64encode(b"k" * 32).decode("ascii")
    )
    generated_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    seed_synthetic_demo(first_dir, now=generated_at, allowed_demo_dir=first_dir)
    seed_synthetic_demo(second_dir, now=generated_at, allowed_demo_dir=second_dir)

    completed = str(SCENARIOS["complete"]["filename"])
    review = str(SCENARIOS["review_ready"]["filename"])
    assert (first_dir / completed).read_bytes() == (second_dir / completed).read_bytes()
    assert (first_dir / review).read_bytes() != (second_dir / review).read_bytes()

    # Both bundles must still validate; non-determinism is not corruption.
    validate_synthetic_bundle(first_dir)
    validate_synthetic_bundle(second_dir)
