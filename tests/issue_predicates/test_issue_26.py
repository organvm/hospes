"""Issue #26 predicate: the rights / clearance gate.

Close condition: clearance CRUD, filtering, reporting, evidence custody,
badges, and publication denial for every status other than cleared pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import synthetic_bearer_authenticator
from hospes import clearances, distribution, encryption, migrations, platform, store
from hospes.api import create_app


try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - exercised only without the api extra
    TestClient = None  # type: ignore[assignment]


UTC = timezone.utc
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 15, 9, 30, tzinfo=UTC)
MASTER_REF = "credential://hospes/master-key"
MASTER_KEY = bytes(range(32))
TENANT = "fixture_tenant"
SHOW = "flagship"
OTHER_SHOW = "field"
EPISODE = "episode-12"
OTHER_EPISODE = "episode-13"
RIGHTS_HOLDER = "Synthetic Rights Society"
LICENSE_TERMS = "One synchronization licence, twelve months, worldwide."
PRODUCER_TOKEN = "issue26-producer-token-0123456789abcdef"  # allow-secret: fixture
HOST_TOKEN = "issue26-host-token-0123456789abcdefabcd"  # allow-secret: fixture
ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")


class _SessionShowASGI:
    """Project the operator's active show into scope, as the operator shell does."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"x-session-show":
                    scope["hospes.operator_show"] = value.decode("utf-8")
        await self.app(scope, receive, send)


def _vault() -> encryption.FieldVault:
    provider = encryption.StaticMasterKeyProvider({MASTER_REF: MASTER_KEY})
    return encryption.FieldVault(encryption.TenantKeyManager(provider, master_key_ref=MASTER_REF))


def _database(tmp_path: Path, name: str = "clearances.sqlite3"):
    conn = store.connect(tmp_path / name)
    for show_id, label in ((SHOW, "Flagship Show"), (OTHER_SHOW, "Field Show")):
        platform.register_show(
            conn,
            tenant_id=TENANT,
            show_id=show_id,
            label=label,
            config_ref=f"config://{show_id}",
        )
    conn.commit()
    return tmp_path / name, conn


def _payload(**overrides) -> dict[str, object]:
    payload = {
        "episode_id": EPISODE,
        "type": "music",
        "rights_holder": RIGHTS_HOLDER,
        "license_terms": LICENSE_TERMS,
        "cost": 50_000,
        "due_date": "2026-09-01",
    }
    payload.update(overrides)
    return payload


def _record(conn, vault, *, show_id: str = SHOW, actor_role: str = "producer", **overrides):
    return clearances.record_clearance(
        conn,
        vault,
        clearances.ClearanceInput.from_mapping(_payload(**overrides)),
        tenant_id=TENANT,
        show_id=show_id,
        actor_id="producer_fixture",
        actor_role=actor_role,
        now=NOW,
    )


def _decide(conn, vault, clearance_id: str, status: str, evidence_ref: str, *, show_id: str = SHOW):
    return clearances.decide_clearance(
        conn,
        vault,
        clearances.ClearanceDecision.from_mapping({"status": status, "evidence_ref": evidence_ref}),
        tenant_id=TENANT,
        show_id=show_id,
        clearance_id=clearance_id,
        actor_id="producer_fixture",
        actor_role="producer",
        now=LATER,
    )


def _client(path: Path, *, vault: encryption.FieldVault | None) -> TestClient:
    app = create_app(
        str(path),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {
                PRODUCER_TOKEN: ("producer_fixture", "producer", TENANT),
                HOST_TOKEN: ("host_fixture", "host", TENANT),
            }
        ),
        csrf_required=False,
        field_vault=vault,
    )
    return TestClient(_SessionShowASGI(app))


def _headers(
    token: str = PRODUCER_TOKEN,  # allow-secret: synthetic fixture bearer
    show: str | None = SHOW,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if show is not None:
        headers["X-Session-Show"] = show
    return headers


# --------------------------------------------------------------------------
# Custody: private counterparty values never rest in the clear.
# --------------------------------------------------------------------------


def test_private_values_are_sealed_and_the_row_keeps_only_references(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    record = _record(conn, vault)

    assert record["custody_mode"] == "sealed"
    assert record["status"] == "pending"
    assert record["blocks_publication"] is True
    assert record["rights_holder"] == RIGHTS_HOLDER
    assert record["license_terms"] == LICENSE_TERMS
    assert record["rights_holder_available"] is True
    assert record["license_terms_available"] is True
    assert record["rights_holder_ref"].startswith("private-field://")
    assert record["license_terms_ref"].startswith("private-field://")
    assert record["recorded_by"] == "producer_fixture"
    assert record["recorded_by_role"] == "producer"
    assert record["evidence_ref"] is None

    stored = store.fetch_one(conn, "SELECT * FROM clearances WHERE id = ?", (record["clearance_id"],))
    serialized = json.dumps(stored, sort_keys=True)
    assert RIGHTS_HOLDER not in serialized
    assert LICENSE_TERMS not in serialized

    ciphertext_rows = store.fetch_all(
        conn,
        "SELECT * FROM private_field_values WHERE owner_table = 'clearances'",
    )
    assert {str(row["field_name"]) for row in ciphertext_rows} == {
        "rights_holder",
        "license_terms",
    }
    assert {str(row["category"]) for row in ciphertext_rows} == {"contact", "financial"}
    for row in ciphertext_rows:
        assert row["algorithm"] == "AES-256-GCM"
        assert RIGHTS_HOLDER not in json.dumps(row, sort_keys=True)
    conn.close()


def test_unconfigured_custody_refuses_a_literal_and_records_the_external_mode(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    with pytest.raises(clearances.ClearanceError) as literal:
        _record(conn, None)
    assert literal.value.status_code == 503
    assert "custody is not configured" in literal.value.detail

    external = _record(
        conn,
        None,
        rights_holder="rights://owner/synthetic-society",
        license_terms="license://owner/synthetic-terms",
    )
    assert external["custody_mode"] == "external_reference"
    assert external["rights_holder_ref"] == "rights://owner/synthetic-society"
    assert external["rights_holder"] is None
    assert external["rights_holder_available"] is False
    assert external["license_terms_available"] is False
    conn.close()


def test_reveal_fails_closed_on_role_tampering_and_ciphertext_loss(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    record = _record(conn, vault)
    row = store.fetch_one(conn, "SELECT * FROM clearances WHERE id = ?", (record["clearance_id"],))

    with pytest.raises(clearances.ClearanceError) as denied:
        clearances.reveal_clearance(conn, vault, row, actor_role="guest")
    assert denied.value.status_code == 403

    store.update(
        conn,
        "clearances",
        record["clearance_id"],
        {"rights_holder_checksum": "0" * 64},
    )
    conn.commit()
    tampered = store.fetch_one(conn, "SELECT * FROM clearances WHERE id = ?", (record["clearance_id"],))
    with pytest.raises(clearances.ClearanceError) as checksum:
        clearances.reveal_clearance(conn, vault, tampered, actor_role="producer")
    assert checksum.value.status_code == 409

    class _FailingVault(encryption.FieldVault):
        def reveal_text(self, *args, **kwargs):  # type: ignore[override]
            raise encryption.EncryptionError("ciphertext unavailable")

    broken = _FailingVault(
        encryption.TenantKeyManager(
            encryption.StaticMasterKeyProvider({MASTER_REF: MASTER_KEY}),
            master_key_ref=MASTER_REF,
        )
    )
    with pytest.raises(clearances.ClearanceError) as unavailable:
        clearances.reveal_clearance(conn, broken, tampered, actor_role="producer")
    assert unavailable.value.status_code == 503
    conn.close()


def test_sealing_failure_rolls_the_whole_clearance_write_back(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)

    class _SealFailure(encryption.FieldVault):
        def put_text(self, *args, **kwargs):  # type: ignore[override]
            raise encryption.EncryptionError("sealing unavailable")

    broken = _SealFailure(
        encryption.TenantKeyManager(
            encryption.StaticMasterKeyProvider({MASTER_REF: MASTER_KEY}),
            master_key_ref=MASTER_REF,
        )
    )
    with pytest.raises(clearances.ClearanceError) as error:
        _record(conn, broken)
    assert error.value.status_code == 503
    assert store.fetch_all(conn, "SELECT * FROM clearances") == []
    assert store.fetch_all(conn, "SELECT * FROM clearance_receipts") == []
    conn.close()


def test_a_rejected_row_write_leaves_no_partial_clearance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    original = store.insert

    def _fail_on_clearance(connection, table, values):
        if table == "clearances":
            raise RuntimeError("synthetic row rejection")
        return original(connection, table, values)

    monkeypatch.setattr(clearances.store, "insert", _fail_on_clearance)
    with pytest.raises(clearances.ClearanceError) as error:
        _record(conn, vault)
    assert error.value.status_code == 409
    monkeypatch.undo()
    assert store.fetch_all(conn, "SELECT * FROM clearances") == []
    assert store.fetch_all(conn, "SELECT * FROM private_field_values WHERE owner_table = 'clearances'") == []
    conn.close()


# --------------------------------------------------------------------------
# CRUD identity, decisions, and immutable receipts.
# --------------------------------------------------------------------------


def test_identity_is_idempotent_and_conflicting_terms_fail_closed(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    first = _record(conn, vault)
    repeat = _record(conn, vault)
    assert repeat["clearance_id"] == first["clearance_id"]
    assert len(store.fetch_all(conn, "SELECT * FROM clearances")) == 1

    with pytest.raises(clearances.ClearanceError) as conflict:
        _record(conn, vault, cost=99_000)
    assert conflict.value.status_code == 409
    assert "different terms" in conflict.value.detail

    # A different licence from the same holder is a distinct rights item.
    second = _record(conn, vault, license_terms="A second, narrower clip licence.")
    assert second["clearance_id"] != first["clearance_id"]
    assert len(store.fetch_all(conn, "SELECT * FROM clearances")) == 2
    conn.close()


def test_every_decision_requires_evidence_and_appends_one_immutable_receipt(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    record = _record(conn, vault)
    clearance_id = record["clearance_id"]

    cleared = _decide(conn, vault, clearance_id, "cleared", "registry://owner/licence-12")
    assert cleared["status"] == "cleared"
    assert cleared["blocks_publication"] is False
    assert cleared["evidence_ref"] == "registry://owner/licence-12"
    assert cleared["decided_by"] == "producer_fixture"
    assert cleared["decided_by_role"] == "producer"
    assert cleared["decided_at"] == LATER.isoformat()

    # Re-submitting the same decision with the same evidence is idempotent.
    again = _decide(conn, vault, clearance_id, "cleared", "registry://owner/licence-12")
    assert again["status"] == "cleared"

    # The same status under different evidence would overwrite custody.
    with pytest.raises(clearances.ClearanceError) as conflict:
        _decide(conn, vault, clearance_id, "cleared", "registry://owner/other-licence")
    assert conflict.value.status_code == 409

    denied = _decide(conn, vault, clearance_id, "denied", "registry://owner/refusal-12")
    assert denied["status"] == "denied"
    reopened = _decide(conn, vault, clearance_id, "pending", "registry://owner/reopen-12")
    assert reopened["status"] == "pending"

    receipts = clearances.clearance_receipts(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="producer",
        clearance_id=clearance_id,
    )
    assert [row["event_type"] for row in receipts] == [
        "clearance.recorded",
        "clearance.cleared",
        "clearance.denied",
        "clearance.reopened",
    ]
    assert [row["from_status"] for row in receipts] == [None, "pending", "cleared", "denied"]
    assert [row["to_status"] for row in receipts] == [
        "pending",
        "cleared",
        "denied",
        "pending",
    ]
    assert receipts[0]["evidence_ref"] is None
    assert all(row["evidence_ref"] for row in receipts[1:])
    assert [row["correlation_id"] for row in receipts] == [
        f"clearance://{clearance_id}/{ordinal}" for ordinal in (1, 2, 3, 4)
    ]
    assert {row["actor_id"] for row in receipts} == {"producer_fixture"}
    conn.close()


def test_decisions_fail_closed_on_scope_role_and_rejected_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    record = _record(conn, vault)
    clearance_id = record["clearance_id"]

    with pytest.raises(clearances.ClearanceError) as missing:
        _decide(conn, vault, clearance_id, "cleared", "registry://owner/x", show_id=OTHER_SHOW)
    assert missing.value.status_code == 404

    with pytest.raises(clearances.ClearanceError) as role:
        clearances.decide_clearance(
            conn,
            vault,
            clearances.ClearanceDecision.from_mapping({"status": "cleared", "evidence_ref": "registry://owner/x"}),
            tenant_id=TENANT,
            show_id=SHOW,
            clearance_id=clearance_id,
            actor_id="host_fixture",
            actor_role="host",
        )
    assert role.value.status_code == 403

    def _fail_update(connection, table, record_id, changes):
        raise RuntimeError("synthetic update rejection")

    monkeypatch.setattr(clearances.store, "update", _fail_update)
    with pytest.raises(clearances.ClearanceError) as rejected:
        _decide(conn, vault, clearance_id, "cleared", "registry://owner/licence-12")
    assert rejected.value.status_code == 409
    monkeypatch.undo()
    unchanged = store.fetch_one(conn, "SELECT * FROM clearances WHERE id = ?", (clearance_id,))
    assert unchanged["status"] == "pending"
    conn.close()


def test_recording_requires_a_decision_role(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    with pytest.raises(clearances.ClearanceError) as error:
        _record(conn, _vault(), actor_role="host")
    assert error.value.status_code == 403
    conn.close()


# --------------------------------------------------------------------------
# Validation: strict intake at both dataclass boundaries.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"episode_id": None}, "episode_id"),
        ({"episode_id": "bad id"}, "episode_id"),
        ({"episode_id": 12}, "episode_id"),
        ({"type": "merch"}, "type must be one of"),
        ({"type": None}, "type must be one of"),
        ({"rights_holder": ""}, "rights_holder is required"),
        ({"rights_holder": 7}, "rights_holder must be text"),
        ({"rights_holder": "x" * 501}, "exceeds 500 characters"),
        ({"rights_holder": "bad\x07holder"}, "control characters"),
        ({"license_terms": "x" * 4001}, "exceeds 4000 characters"),
        ({"cost": True}, "whole number"),
        ({"cost": -1}, "between 0 and"),
        ({"cost": 1_000_000_001}, "between 0 and"),
        ({"due_date": "01-09-2026"}, "ISO 8601 calendar date"),
        ({"due_date": 20260901}, "ISO 8601 calendar date"),
        ({"due_date": "2026-13-45"}, "ISO 8601 calendar date"),
    ],
)
def test_clearance_intake_rejects_malformed_fields(payload, fragment) -> None:
    with pytest.raises(clearances.ClearanceError) as error:
        clearances.ClearanceInput.from_mapping(_payload(**payload))
    assert fragment in error.value.detail


def test_clearance_intake_rejects_ambiguous_and_unsupported_payloads() -> None:
    with pytest.raises(clearances.ClearanceError, match="must be an object"):
        clearances.ClearanceInput.from_mapping(["not", "a", "mapping"])  # type: ignore[arg-type]
    with pytest.raises(clearances.ClearanceError, match="unsupported fields"):
        clearances.ClearanceInput.from_mapping(_payload(status="cleared"))
    with pytest.raises(clearances.ClearanceError, match="exactly once"):
        clearances.ClearanceInput.from_mapping(_payload(clearance_type="music"))
    with pytest.raises(clearances.ClearanceError, match="exactly once"):
        clearances.ClearanceInput.from_mapping(_payload(cost_minor=10))

    parsed = clearances.ClearanceInput.from_mapping(
        {
            "episode_id": EPISODE,
            "clearance_type": "clip",
            "rights_holder": RIGHTS_HOLDER,
            "license_terms": LICENSE_TERMS,
        }
    )
    assert (parsed.clearance_type, parsed.cost_minor, parsed.due_date) == ("clip", 0, None)


def test_decision_intake_requires_a_status_and_opaque_evidence() -> None:
    with pytest.raises(clearances.ClearanceError, match="must be an object"):
        clearances.ClearanceDecision.from_mapping("cleared")  # type: ignore[arg-type]
    with pytest.raises(clearances.ClearanceError, match="unsupported fields"):
        clearances.ClearanceDecision.from_mapping({"status": "cleared", "evidence_ref": "registry://a/b", "note": "x"})
    with pytest.raises(clearances.ClearanceError, match="status must be one of"):
        clearances.ClearanceDecision.from_mapping({"status": "approved", "evidence_ref": "registry://a/b"})
    for evidence in (None, "", "not-a-reference", 12):
        with pytest.raises(clearances.ClearanceError, match="opaque custody reference"):
            clearances.ClearanceDecision.from_mapping({"status": "cleared", "evidence_ref": evidence})


# --------------------------------------------------------------------------
# Filtering, badges, and the CSV report.
# --------------------------------------------------------------------------


def test_filters_are_bounded_scoped_and_validated(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    music = _record(conn, vault)
    clip = _record(
        conn,
        vault,
        type="clip",
        episode_id=OTHER_EPISODE,
        rights_holder="Second Synthetic Holder",
        due_date="2026-10-15",
    )
    _decide(conn, vault, clip["clearance_id"], "cleared", "registry://owner/licence-13")
    _record(conn, vault, show_id=OTHER_SHOW, rights_holder="Other Show Holder")

    def listed(**kwargs):
        return clearances.list_clearances(
            conn,
            vault,
            tenant_id=TENANT,
            show_id=SHOW,
            actor_role="producer",
            **kwargs,
        )

    assert {row["clearance_id"] for row in listed()} == {
        music["clearance_id"],
        clip["clearance_id"],
    }
    assert [row["clearance_id"] for row in listed(episode_id=EPISODE)] == [music["clearance_id"]]
    assert [row["status"] for row in listed(status="cleared")] == ["cleared"]
    assert [row["type"] for row in listed(clearance_type="clip")] == ["clip"]
    assert [row["clearance_id"] for row in listed(due_before="2026-09-30")] == [music["clearance_id"]]
    assert [row["clearance_id"] for row in listed(blocking_only=True)] == [music["clearance_id"]]
    assert len(listed(limit=1)) == 1

    # Another show's rights items are never visible from this scope.
    other = clearances.list_clearances(conn, vault, tenant_id=TENANT, show_id=OTHER_SHOW, actor_role="producer")
    assert [row["show_id"] for row in other] == [OTHER_SHOW]

    for kwargs, fragment in (
        ({"limit": 0}, "between 1 and 500"),
        ({"limit": 501}, "between 1 and 500"),
        ({"limit": True}, "between 1 and 500"),
        ({"limit": "20"}, "between 1 and 500"),
        ({"blocking_only": "yes"}, "must be a boolean"),
        ({"status": "approved"}, "status must be one of"),
        ({"clearance_type": "merch"}, "type must be one of"),
        ({"due_before": "soon"}, "ISO 8601 calendar date"),
        ({"episode_id": "bad id"}, "opaque identifier"),
    ):
        with pytest.raises(clearances.ClearanceError) as error:
            listed(**kwargs)
        assert fragment in error.value.detail

    for call in (
        lambda: clearances.list_clearances(conn, vault, tenant_id=TENANT, show_id=SHOW, actor_role="guest"),
        lambda: clearances.episode_clearance_summary(conn, tenant_id=TENANT, show_id=SHOW, actor_role="guest"),
        lambda: clearances.clearance_receipts(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            actor_role="guest",
            clearance_id=music["clearance_id"],
        ),
        lambda: clearances.clearance_report_csv(conn, tenant_id=TENANT, show_id=SHOW, actor_role="guest"),
    ):
        with pytest.raises(clearances.ClearanceError) as denied:
            call()
        assert denied.value.status_code == 403
    conn.close()


def test_episode_badges_count_every_status_and_render_the_operator_string(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    first = _record(conn, vault)
    _record(conn, vault, rights_holder="Second Synthetic Holder", due_date="2026-08-20")
    third = _record(
        conn,
        vault,
        episode_id=OTHER_EPISODE,
        rights_holder="Third Synthetic Holder",
        due_date=None,
    )

    badges = {
        row["episode_id"]: row
        for row in clearances.episode_clearance_summary(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer")
    }
    assert badges[EPISODE]["pending"] == 2
    assert badges[EPISODE]["blocking"] == 2
    assert badges[EPISODE]["publishable"] is False
    assert badges[EPISODE]["badge"] == "⚠ 2 clearances pending"
    assert badges[EPISODE]["next_due_date"] == "2026-08-20"
    assert badges[OTHER_EPISODE]["badge"] == "⚠ 1 clearance pending"
    assert badges[OTHER_EPISODE]["next_due_date"] is None

    _decide(conn, vault, first["clearance_id"], "denied", "registry://owner/refusal-12")
    mixed = clearances.episode_clearance_summary(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="producer",
        episode_id=EPISODE,
    )
    assert [row["episode_id"] for row in mixed] == [EPISODE]
    assert mixed[0]["badge"] == "⚠ 1 pending · 1 denied"
    assert mixed[0]["denied"] == 1

    _decide(conn, vault, third["clearance_id"], "cleared", "registry://owner/licence-13")
    cleared = clearances.episode_clearance_summary(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="producer",
        episode_id=OTHER_EPISODE,
    )
    assert cleared[0]["badge"] == "✓ 1 clearance cleared"
    assert cleared[0]["publishable"] is True

    assert clearances._badge(0, 2, 0) == "⚠ 2 clearances denied"
    assert clearances._badge(0, 0, 2) == "✓ 2 clearances cleared"
    assert clearances._badge(0, 0, 0) == "No clearances recorded"
    conn.close()


def test_the_csv_report_carries_references_only_and_neutralizes_formulas(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    record = _record(conn, vault)
    _decide(conn, vault, record["clearance_id"], "cleared", "registry://owner/licence-12")
    _record(conn, vault, episode_id=OTHER_EPISODE, rights_holder="Second Holder")

    rendered = clearances.clearance_report_csv(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer")
    lines = rendered.strip().splitlines()
    assert lines[0] == ",".join(clearances.CSV_COLUMNS)
    assert len(lines) == 3
    assert RIGHTS_HOLDER not in rendered
    assert LICENSE_TERMS not in rendered
    assert "private-field://" in rendered
    assert "registry://owner/licence-12" in rendered

    scoped = clearances.clearance_report_csv(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="producer",
        episode_id=EPISODE,
        status="cleared",
    )
    assert len(scoped.strip().splitlines()) == 2

    assert clearances._csv_cell("=cmd|calc") == "'=cmd|calc"
    assert clearances._csv_cell("+1") == "'+1"
    assert clearances._csv_cell("-1") == "'-1"
    assert clearances._csv_cell("@sum") == "'@sum"
    assert clearances._csv_cell(None) == ""
    assert clearances._csv_cell("music") == "music"
    conn.close()


# --------------------------------------------------------------------------
# Publication denial for every status other than cleared.
# --------------------------------------------------------------------------


def test_publication_is_denied_for_pending_and_denied_and_allowed_only_when_cleared(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    vault = _vault()
    record = _record(conn, vault)

    pending_gate = clearances.publication_gate(conn, tenant_id=TENANT, show_id=SHOW, episode_id=EPISODE)
    assert pending_gate["publishable"] is False
    assert pending_gate["pending"] == 1
    assert pending_gate["denied"] == 0
    assert pending_gate["blocking"][0]["clearance_id"] == record["clearance_id"]
    assert "1 pending, 0 denied" in pending_gate["reason"]

    blocked = distribution.create_draft(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        platform_name="rss",
        metadata={"title": "Synthetic episode"},
        idempotency_key=f"{EPISODE}:rss",
        now=NOW,
    )
    assert blocked["status"] == "blocked"
    with pytest.raises(platform.PlatformError, match="uncleared rights items"):
        distribution.authorize_publish(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=blocked["id"],
            outbound_mode="manual_receipt",
            authorized_by="producer_fixture",
            authorization_ref="receipt://owner/authorization-12",
            idempotency_key=f"{EPISODE}:auth",
            now=NOW,
        )

    _decide(conn, vault, record["clearance_id"], "denied", "registry://owner/refusal-12")
    denied_gate = clearances.publication_gate(conn, tenant_id=TENANT, show_id=SHOW, episode_id=EPISODE)
    assert denied_gate["publishable"] is False
    assert denied_gate["denied"] == 1
    with pytest.raises(platform.PlatformError, match="0 pending, 1 denied"):
        distribution.authorize_publish(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=blocked["id"],
            outbound_mode="manual_receipt",
            authorized_by="producer_fixture",
            authorization_ref="receipt://owner/authorization-12",
            idempotency_key=f"{EPISODE}:auth",
            now=NOW,
        )

    _decide(conn, vault, record["clearance_id"], "cleared", "registry://owner/licence-12")
    assert clearances.publication_gate(conn, tenant_id=TENANT, show_id=SHOW, episode_id=EPISODE) == {
        "episode_id": EPISODE,
        "publishable": True,
        "blocking": [],
        "pending": 0,
        "denied": 0,
        "reason": None,
    }
    authorized = distribution.authorize_publish(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        distribution_id=blocked["id"],
        outbound_mode="manual_receipt",
        authorized_by="producer_fixture",
        authorization_ref="receipt://owner/authorization-12",
        idempotency_key=f"{EPISODE}:auth",
        now=NOW,
    )
    assert authorized["status"] == "authorized"

    # A clearance reopened after authorization still denies mark-published.
    _decide(conn, vault, record["clearance_id"], "pending", "registry://owner/reopen-12")
    with pytest.raises(platform.PlatformError, match="uncleared rights items"):
        distribution.mark_published(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=blocked["id"],
            external_id_ref="external://owner/published-12",
            now=NOW,
        )

    _decide(conn, vault, record["clearance_id"], "cleared", "registry://owner/licence-12")
    published = distribution.mark_published(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        distribution_id=blocked["id"],
        external_id_ref="external://owner/published-12",
        now=NOW,
    )
    assert published["status"] == "published"

    # An episode with no rights items at all is publishable.
    empty = clearances.publication_gate(conn, tenant_id=TENANT, show_id=SHOW, episode_id="episode-99")
    assert empty["publishable"] is True
    assert empty["blocking"] == []
    conn.close()


# --------------------------------------------------------------------------
# HTTP surface: scoped, private-response, role-constrained.
# --------------------------------------------------------------------------


def test_the_clearance_api_covers_crud_filtering_badges_receipts_and_report(
    tmp_path: Path,
) -> None:
    database, conn = _database(tmp_path, "clearance-api.sqlite3")
    conn.close()
    with _client(database, vault=_vault()) as client:
        created = client.post(f"/v1/shows/{SHOW}/clearances", json=_payload(), headers=_headers())
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["custody_mode"] == "sealed"
        assert body["rights_holder"] == RIGHTS_HOLDER
        assert created.headers["Cache-Control"] == "no-store, private"
        clearance_id = body["clearance_id"]

        listed = client.get(f"/v1/shows/{SHOW}/clearances", headers=_headers())
        assert listed.status_code == 200
        assert [row["clearance_id"] for row in listed.json()] == [clearance_id]
        assert listed.headers["Cache-Control"] == "no-store, private"

        filtered = client.get(
            f"/v1/shows/{SHOW}/clearances",
            params={"status": "cleared", "clearance_type": "music", "blocking_only": False},
            headers=_headers(),
        )
        assert filtered.json() == []

        badges = client.get(f"/v1/shows/{SHOW}/clearance-badges", headers=_headers())
        assert badges.json()[0]["badge"] == "⚠ 1 clearance pending"

        decided = client.post(
            f"/v1/shows/{SHOW}/clearances/{clearance_id}/decision",
            json={"status": "cleared", "evidence_ref": "registry://owner/licence-12"},
            headers=_headers(),
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "cleared"

        receipts = client.get(f"/v1/shows/{SHOW}/clearances/{clearance_id}/receipts", headers=_headers())
        assert [row["event_type"] for row in receipts.json()] == [
            "clearance.recorded",
            "clearance.cleared",
        ]

        report = client.get(f"/v1/shows/{SHOW}/clearance-report", headers=_headers())
        assert report.status_code == 200
        assert report.headers["content-type"].startswith("text/csv")
        assert report.headers["Cache-Control"] == "no-store, private"
        assert "clearance-report.csv" in report.headers["Content-Disposition"]
        assert RIGHTS_HOLDER not in report.text
        assert "registry://owner/licence-12" in report.text

        # Validation, authorization, and scope all fail closed over HTTP.
        assert (
            client.post(f"/v1/shows/{SHOW}/clearances", json={"episode_id": EPISODE}, headers=_headers()).status_code
            == 422
        )
        assert (
            client.post(f"/v1/shows/{SHOW}/clearances", json=_payload(), headers=_headers(HOST_TOKEN)).status_code
            == 403
        )
        assert (
            client.post(
                f"/v1/shows/{SHOW}/clearances/{clearance_id}/decision",
                json={"status": "denied", "evidence_ref": "registry://owner/refusal"},
                headers=_headers(HOST_TOKEN),
            ).status_code
            == 403
        )
        missing = client.post(
            f"/v1/shows/{SHOW}/clearances/does-not-exist/decision",
            json={"status": "cleared", "evidence_ref": "registry://owner/x"},
            headers=_headers(),
        )
        assert missing.status_code == 404

        cross = client.get(f"/v1/shows/{OTHER_SHOW}/clearances", headers=_headers(show=SHOW))
        assert cross.status_code == 403
        assert "show scope" in cross.json()["detail"]
        for path in (
            f"/v1/shows/{OTHER_SHOW}/clearance-badges",
            f"/v1/shows/{OTHER_SHOW}/clearance-report",
        ):
            assert client.get(path, headers=_headers(show=SHOW)).status_code == 403

        # A host may read the gate even though it may not decide it.
        assert client.get(f"/v1/shows/{SHOW}/clearances", headers=_headers(HOST_TOKEN)).status_code == 200


def test_the_api_reports_unconfigured_custody_instead_of_storing_a_literal(
    tmp_path: Path,
) -> None:
    database, conn = _database(tmp_path, "clearance-nocustody.sqlite3")
    conn.close()
    with _client(database, vault=None) as client:
        literal = client.post(f"/v1/shows/{SHOW}/clearances", json=_payload(), headers=_headers())
        assert literal.status_code == 503
        assert "custody is not configured" in literal.json()["detail"]

        external = client.post(
            f"/v1/shows/{SHOW}/clearances",
            json=_payload(
                rights_holder="rights://owner/synthetic-society",
                license_terms="license://owner/synthetic-terms",
            ),
            headers=_headers(),
        )
        assert external.status_code == 201
        assert external.json()["custody_mode"] == "external_reference"
        assert external.json()["rights_holder_available"] is False


# --------------------------------------------------------------------------
# Schema, storage, packaging, and documentation receipts.
# --------------------------------------------------------------------------


def test_migration_schema_packaging_and_documentation_surfaces_are_complete(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "clearance-schema.sqlite3")
    assert migrations.current_version(conn) >= 15
    ledger = {str(row["name"]) for row in migrations.applied_migrations(conn)}
    assert "rights_clearance_custody" in ledger
    columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(clearances)")}
    assert {
        "custody_mode",
        "rights_holder_checksum",
        "license_terms_checksum",
        "recorded_by",
        "recorded_by_role",
        "decided_by",
        "decided_by_role",
        "decided_at",
        "evidence_ref",
        "correlation_id",
    } <= columns
    indexes = {row["name"] for row in store.fetch_all(conn, "PRAGMA index_list(clearances)")}
    assert {
        "ux_clearance_scope_id",
        "ux_clearance_identity",
        "ix_clearance_status_due",
        "ix_clearance_type_status",
    } <= indexes
    receipt_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(clearance_receipts)")}
    assert {
        "clearance_id",
        "episode_id",
        "event_type",
        "from_status",
        "to_status",
        "actor_id",
        "actor_role",
        "evidence_ref",
        "correlation_id",
    } <= receipt_columns
    receipt_indexes = {row["name"] for row in store.fetch_all(conn, "PRAGMA index_list(clearance_receipts)")}
    assert {
        "ux_clearance_receipt_correlation",
        "ix_clearance_receipt_timeline",
        "ix_clearance_receipt_episode",
    } <= receipt_indexes
    conn.close()

    schema = json.loads((ROOT / "spec" / "clearance.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["type"]["enum"] == list(clearances.CLEARANCE_TYPES)
    assert schema["properties"]["status"]["enum"] == list(clearances.CLEARANCE_STATUSES)
    assert schema["properties"]["custody_mode"]["enum"] == list(clearances.CUSTODY_MODES)
    assert set(schema["required"]) >= {"rights_holder_ref", "license_terms_ref", "custody_mode"}
    assert "rights_holder" not in schema["properties"]
    assert schema["additionalProperties"] is False
    assert schema["allOf"][0]["then"]["required"] == ["evidence_ref"]

    receipt_schema = json.loads((ROOT / "spec" / "clearance_receipt.schema.json").read_text(encoding="utf-8"))
    assert receipt_schema["properties"]["event_type"]["enum"] == [
        "clearance.recorded",
        "clearance.cleared",
        "clearance.denied",
        "clearance.reopened",
    ]
    assert receipt_schema["properties"]["actor_role"]["enum"] == sorted(
        clearances.CLEARANCE_DECISION_ROLES, key=["producer", "editorial_owner", "relationship_owner"].index
    )

    for relative in (
        "spec/clearance.schema.json",
        "spec/clearance_receipt.schema.json",
        "config/domain_kernel.yaml",
        "dashboard/index.html",
        "dashboard/assets/api.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/guide.json",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/styles.css",
    ):
        assert (ROOT / relative).read_bytes() == (ROOT / "hospes" / "resources" / relative).read_bytes(), relative

    html = (ROOT / "dashboard" / "assets" / "partnership-workspace.html").read_text(encoding="utf-8")
    javascript = (ROOT / "dashboard" / "assets" / "partnership-workspace.js").read_text(encoding="utf-8")
    api_javascript = (ROOT / "dashboard" / "assets" / "api.js").read_text(encoding="utf-8")
    capabilities = (ROOT / "dashboard" / "assets" / "capabilities.mjs").read_text(encoding="utf-8")
    for marker in (
        'id="clearance-panel"',
        'id="clearance-badges"',
        'id="clearance-list"',
        'id="clearance-form"',
        'id="btn-clearance-report"',
        'data-clearance-filter="pending"',
    ):
        assert marker in html, marker
    assert "escapeHTML(item.status)" in javascript
    assert "escapeHTML(holder)" in javascript
    assert "decideClearance(form.dataset.showId" in javascript
    assert "loadClearanceReport" in api_javascript
    assert "clearance-badges" in api_javascript
    assert "canManageClearances" in capabilities
    assert "canViewClearances" in capabilities
    guide = json.loads((ROOT / "dashboard" / "assets" / "guide.json").read_text(encoding="utf-8"))
    assert {"canViewClearances", "canManageClearances"} <= set(guide["capabilities"])
    assert guide["elements"]["clearance-tab"]["capability"] == "canViewClearances"

    documentation = (ROOT / "docs" / "rights-clearance.md").read_text(encoding="utf-8")
    for phrase in (
        "AES-256-GCM",
        "every status other than `cleared`",
        "external_reference",
        "Cache-Control: no-store",
        "clearance.reopened",
        "⚠ 2 clearances pending",
        "tenant/show",
    ):
        assert phrase in documentation, phrase
    assert "docs/rights-clearance.md" in (ROOT / "README.md").read_text(encoding="utf-8")

    domain = (ROOT / "config" / "domain_kernel.yaml").read_text(encoding="utf-8")
    assert "clearance_contract:" in domain
    assert "Publication is denied for every status other than cleared" in domain
    assert "Every status decision requires an opaque evidence reference" in domain
