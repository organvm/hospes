"""Executable completion predicate for HOSPES issue #24.

Guest CRM — cross-season memory, the approval-card badge, and the do-not-contact
directive that is enforced at both ends of the loop: intake and outbound.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import synthetic_bearer_authenticator
from hospes import candidate_import, encryption, guest_crm, migrations
from hospes import nurture, platform, service, store
from hospes.api import create_app


UTC = timezone.utc
NOW = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)


def _wall_clock_today() -> str:
    """Guest-history dates written through ``service.record_decision`` are
    stamped from the live clock (``generation.now()``), not the frozen
    fixture ``NOW`` used to construct guest_crm inputs directly — mirror
    that real clock here so badge assertions track whatever day the suite
    actually runs on instead of drifting across a UTC midnight boundary.
    """
    return datetime.now(UTC).date().isoformat()


MASTER_REF = "credential://hospes/master-key"
MASTER_KEY = bytes(range(32))
TENANT = "fixture_tenant"
NETWORK = "fixture_network"
SHOW = "fixture_show"
OTHER_SHOW = "fixture_show_two"
GUEST = "crm:synthetic:issue-24"
PRIVATE_NOTE = "Synthetic private note: declined season two, asked us to try later."

OWNER = service.HumanActor(actor_id="ari_fixture", role=service.HumanRole.RELATIONSHIP_OWNER, tenant_id=TENANT)
PRODUCER = service.HumanActor(actor_id="producer_fixture", role=service.HumanRole.PRODUCER, tenant_id=TENANT)


def _vault() -> encryption.FieldVault:
    provider = encryption.StaticMasterKeyProvider({MASTER_REF: MASTER_KEY})
    return encryption.FieldVault(encryption.TenantKeyManager(provider, master_key_ref=MASTER_REF))


def _candidate(
    source_key: str = "crm:synthetic:issue-24",
    *,
    guest_name: str = "Synthetic Cross-Season Guest",
    guest_id: str | None = GUEST,
    season: str | None = "S3",
    episode: str | None = "E1",
    do_not_contact: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "source_key": source_key,
        "guest_name": guest_name,
        "why_guest": "This synthetic guest proves cross-season memory survives a season boundary.",
        "why_now": "The issue predicate needs a bounded prior-season fixture now.",
        "episode_thesis": "A booking desk that forgets its own history re-asks people who already declined.",
        "proposed_artifact": "A cross-season guest history entry",
        "relationship_class": "C2",
        "relationship_owner": "relationship_fixture",
        "route_type": "producer_system",
        "route_reference": "producer://fixture/route-24",
        "route_verified_at": "2026-08-10T12:00:00+00:00",
        "preferred_city": "Los Angeles",
        "social_cost_1_5": "2",
        "ari_effort": "review_only",
        "next_action": "Review this synthetic guest in the workbench.",
        "source_provenance": "Synthetic issue predicate fixture.",
    }
    if guest_id is not None:
        row["guest_id"] = guest_id
    if season is not None:
        row["season"] = season
    if episode is not None:
        row["episode"] = episode
    if do_not_contact is not None:
        row["do_not_contact"] = do_not_contact
    return row


def _import(conn, rows, *, actor_role: str = "producer", show_id: str = SHOW):
    return candidate_import.import_candidates(
        conn,
        rows,
        tenant_id=TENANT,
        network_id=NETWORK,
        show_id=show_id,
        actor_id="producer_fixture",
        actor_role=actor_role,
        now=NOW,
    )


def _fixture(tmp_path: Path):
    database = tmp_path / "guest-crm.sqlite3"
    conn = store.connect(database)
    opportunity_id = _import(conn, [_candidate()]).created_ids[0]
    conn.commit()
    return database, conn, _vault(), opportunity_id


def _approve(conn, opportunity_id: str, actor: service.HumanActor = OWNER):
    return service.record_decision(
        conn,
        opportunity_id,
        service.DecisionCreate.from_dict({"action": "approve"}),
        actor,
    )


# ---------------------------------------------------------------------------
# 1. Memory: a season's decision becomes the next season's badge.
# ---------------------------------------------------------------------------


def test_decision_becomes_idempotent_attributed_cross_season_memory(tmp_path: Path) -> None:
    _, conn, _vault_unused, opportunity_id = _fixture(tmp_path)
    stored = store.fetch_one(
        conn,
        "SELECT guest_id, season, episode FROM appearance_opportunities WHERE id = ?",
        (opportunity_id,),
    )
    assert stored == {"guest_id": GUEST, "season": "S3", "episode": "E1"}

    detail = _approve(conn, opportunity_id)
    assert detail["disposition"] == "APPROVED"
    assert detail["guest_id"] == GUEST
    assert detail["do_not_contact"] is False
    assert detail["guest_history_badge"] == f"Previously: S3E1 — APPROVED ({_wall_clock_today()})"
    entry = detail["guest_history"][0]
    assert entry["kind"] == "guest_history_entry"
    assert entry["source"] == "decision"
    assert entry["recorded_by"] == "ari_fixture"
    assert entry["recorded_by_role"] == "relationship_owner"
    assert entry["opportunity_id"] == opportunity_id
    assert entry["notes_ref"].startswith("guest-history://")

    # An idempotent re-decision is one memory, not two.
    _approve(conn, opportunity_id)
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM guest_history")["count"] == 1
    assert (
        store.fetch_one(
            conn,
            "SELECT COUNT(*) AS count FROM audit_events WHERE event_type = 'guest_history.recorded'",
        )["count"]
        == 1
    )

    # The badge follows the guest identity into the next season's opportunity.
    next_season = _import(conn, [_candidate(source_key="crm:synthetic:issue-24-s4", season="S4", episode="E2")])
    queue = service.approval_queue(conn, TENANT, show_id=SHOW)
    by_id = {row["id"]: row for row in queue}
    assert by_id[next_season.created_ids[0]]["guest_history_badge"] == (
        f"Previously: S3E1 — APPROVED ({_wall_clock_today()})"
    )
    assert by_id[next_season.created_ids[0]]["do_not_contact"] is False
    conn.close()


def test_memory_without_a_declared_season_is_never_half_recorded(tmp_path: Path) -> None:
    database = tmp_path / "no-season.sqlite3"
    conn = store.connect(database)
    opportunity_id = _import(conn, [_candidate(season=None, episode=None)]).created_ids[0]
    conn.commit()
    detail = _approve(conn, opportunity_id)
    assert detail["guest_history"] == []
    assert detail["guest_history_badge"] is None
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM guest_history")["count"] == 0

    # A note action carries no disposition, so it records no memory either.
    service.record_decision(
        conn,
        opportunity_id,
        service.DecisionCreate.from_dict({"action": "note", "note": "Synthetic note."}),
        OWNER,
    )
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM guest_history")["count"] == 0
    conn.close()


# ---------------------------------------------------------------------------
# 2. Directive: owner-only, and enforced at intake and every outbound edge.
# ---------------------------------------------------------------------------


def test_directive_is_owner_only_and_blocks_import_and_every_outbound_edge(
    tmp_path: Path,
) -> None:
    _, conn, vault, opportunity_id = _fixture(tmp_path)
    _approve(conn, opportunity_id)
    preview = service.preview_draft(
        conn,
        opportunity_id,
        service.DraftCreate.from_dict({"kind": "invitation"}),
        PRODUCER,
    )
    assert preview["persisted"] is False

    with pytest.raises(guest_crm.GuestHistoryError, match="relationship owner"):
        service.set_guest_do_not_contact(
            conn,
            opportunity_id,
            service.DoNotContactUpdate.from_dict({"do_not_contact": True}),
            PRODUCER,
        )
    assert guest_crm.is_do_not_contact(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST) is False

    blocked = service.set_guest_do_not_contact(
        conn,
        opportunity_id,
        service.DoNotContactUpdate.from_dict({"do_not_contact": True, "reason_ref": "registry://guest/opt-out-24"}),
        OWNER,
    )
    assert blocked["do_not_contact"] is True
    assert blocked["guest_directive"]["authority"] == "directive"
    assert blocked["guest_directive"]["reason_ref"] == "registry://guest/opt-out-24"
    assert blocked["guest_directive"]["set_by_role"] == "relationship_owner"
    # The directive entry is in the timeline but never becomes the badge.
    assert blocked["guest_history"][0]["source"] == "directive"
    assert blocked["guest_history_badge"] == f"Previously: S3E1 — APPROVED ({_wall_clock_today()})"
    assert (
        store.fetch_one(
            conn,
            "SELECT COUNT(*) AS count FROM audit_events WHERE event_type = 'guest.do_not_contact_set'",
        )["count"]
        == 1
    )

    for outbound in (service.preview_draft, service.create_draft):
        with pytest.raises(service.DomainError, match="do not contact") as raised:
            outbound(
                conn,
                opportunity_id,
                service.DraftCreate.from_dict({"kind": "invitation"}),
                PRODUCER,
            )
        assert raised.value.status_code == 403
    with pytest.raises(service.DomainError, match="do not contact"):
        service.record_receipt(
            conn,
            opportunity_id,
            service.ReceiptCreate.from_dict(
                {
                    "receipt_type": "outreach.sent",
                    "occurred_at": (NOW - timedelta(days=1)).isoformat(),
                    "external_reference": "registry://owner/receipt-24",
                }
            ),
            PRODUCER,
        )

    # Intake refuses the whole batch, so the innocent sibling row is not written.
    with pytest.raises(candidate_import.CandidateImportError, match="do-not-contact"):
        _import(
            conn,
            [
                _candidate(),
                _candidate(
                    source_key="crm:synthetic:issue-24-sibling",
                    guest_id="crm:synthetic:issue-24-sibling",
                    guest_name="Synthetic Sibling Guest",
                ),
            ],
        )
    assert (
        store.fetch_one(
            conn,
            "SELECT COUNT(*) AS count FROM appearance_opportunities WHERE source_key = ?",
            ("crm:synthetic:issue-24-sibling",),
        )["count"]
        == 0
    )

    # Suggestions and nurture cadence withhold a blocked guest.
    assert platform.suggest_guests(conn, tenant_id=TENANT, show_id=SHOW) == []
    assert (
        nurture.due_for_guest(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            guest_id="synthetic-guest-24",
            relationship_class="C2",
            last_contact=datetime(2020, 1, 1, tzinfo=UTC),
            now=NOW,
        )["due"]
        is True
    )

    lifted = service.set_guest_do_not_contact(
        conn,
        opportunity_id,
        service.DoNotContactUpdate.from_dict({"do_not_contact": False}),
        OWNER,
    )
    assert lifted["do_not_contact"] is False
    assert (
        store.fetch_one(
            conn,
            "SELECT COUNT(*) AS count FROM audit_events WHERE event_type = 'guest.contact_permitted'",
        )["count"]
        == 1
    )
    assert (
        service.preview_draft(
            conn,
            opportunity_id,
            service.DraftCreate.from_dict({"kind": "invitation"}),
            PRODUCER,
        )["persisted"]
        is False
    )
    assert _import(conn, [_candidate()]).unchanged_ids == [opportunity_id]
    assert [row["id"] for row in platform.suggest_guests(conn, tenant_id=TENANT, show_id=SHOW)] == [opportunity_id]
    del vault
    conn.close()


def test_legacy_history_marker_still_blocks_until_a_directive_lifts_it(
    tmp_path: Path,
) -> None:
    _, conn, _unused, opportunity_id = _fixture(tmp_path)
    # The pre-directive scaffold expressed do-not-contact as an append-only
    # history marker. A reader that ignored it would silently un-block a guest.
    platform.set_do_not_contact(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        guest_id="synthetic-legacy-24",
        notes_ref="vault://history/legacy-dnc",
        now=NOW,
    )
    state = guest_crm.directive_state(conn, tenant_id=TENANT, show_id=SHOW, guest_id="synthetic-legacy-24")
    assert state == {
        "guest_id": "synthetic-legacy-24",
        "do_not_contact": True,
        "reason_ref": None,
        "source": "legacy",
        "set_by": None,
        "set_by_role": None,
        "updated_at": None,
        "authority": "legacy_history",
    }
    assert (
        nurture.due_for_guest(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            guest_id="synthetic-legacy-24",
            relationship_class="C2",
            last_contact=datetime(2020, 1, 1, tzinfo=UTC),
            now=NOW,
        )["due"]
        is False
    )

    lifted = guest_crm.set_directive(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        guest_id="synthetic-legacy-24",
        do_not_contact=False,
        actor_id="ari_fixture",
        actor_role="relationship_owner",
        now=NOW,
    )
    assert lifted["authority"] == "directive"
    assert lifted["do_not_contact"] is False
    assert lifted["changed"] is True
    assert (
        guest_crm.blocked_guests(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            guest_ids=["synthetic-legacy-24", GUEST, "synthetic-legacy-24"],
        )
        == set()
    )
    # Re-declaring the same directive writes nothing new.
    repeat = guest_crm.set_directive(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        guest_id="synthetic-legacy-24",
        do_not_contact=False,
        actor_id="ari_fixture",
        actor_role="relationship_owner",
        now=NOW,
    )
    assert repeat["changed"] is False
    del opportunity_id
    conn.close()


# ---------------------------------------------------------------------------
# 3. Import: identity, season labels, and the owner-only declaration.
# ---------------------------------------------------------------------------


def test_import_identity_labels_and_owner_only_declaration(tmp_path: Path) -> None:
    database = tmp_path / "import.sqlite3"
    conn = store.connect(database)

    # Without an explicit guest_id the opaque source key IS the identity.
    inherited = _import(conn, [_candidate(source_key="crm:synthetic:inherited", guest_id=None)])
    assert store.fetch_one(
        conn,
        "SELECT guest_id FROM appearance_opportunities WHERE id = ?",
        (inherited.created_ids[0],),
    ) == {"guest_id": "crm:synthetic:inherited"}

    for row, field_name, message in (
        (_candidate(guest_id="not a key"), "guest_id", "opaque cross-season key"),
        (_candidate(season="S" * 40), "season", "must be 1-32 characters"),
        (_candidate(episode="E/3\\4"), "episode", "short opaque label"),
        (_candidate(do_not_contact="maybe"), "do_not_contact", "explicit boolean"),
        (
            _candidate(guest_id="mailto://guest@example.com"),
            "guest_id",
            "must remain in its external owner",
        ),
    ):
        with pytest.raises(candidate_import.CandidateImportError) as raised:
            _import(conn, [row])
        assert any(issue.field_name == field_name and message in issue.message for issue in raised.value.issues), (
            field_name,
            [issue.message for issue in raised.value.issues],
        )

    with pytest.raises(candidate_import.CandidateImportError, match="relationship owner"):
        _import(conn, [_candidate(do_not_contact="true")], actor_role="producer")
    assert guest_crm.is_do_not_contact(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST) is False

    declared = _import(conn, [_candidate(do_not_contact="true")], actor_role="relationship_owner")
    assert declared.created == 1
    assert guest_crm.is_do_not_contact(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST) is True
    # A row that agrees with the directive is not a re-ask, so it stays idempotent.
    repeat = _import(conn, [_candidate(do_not_contact="true")], actor_role="relationship_owner")
    assert repeat.unchanged_ids == declared.created_ids
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM guest_directives")["count"] == 1
    conn.close()


# ---------------------------------------------------------------------------
# 4. Validation, role, scope, and custody edges.
# ---------------------------------------------------------------------------


def test_validation_role_scope_and_custody_edges_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, conn, vault, opportunity_id = _fixture(tmp_path)
    base = {
        "guest_id": GUEST,
        "season": "S1",
        "episode": "E5",
        "disposition": "soft_decline",
        "date": "2024-03-15",
    }
    for invalid, message in (
        ([], "must be an object"),
        ({**base, "recorded_by": "spoofed"}, "authenticated session"),
        ({**base, "unsupported": "value"}, "unsupported fields"),
        ({key: value for key, value in base.items() if key != "guest_id"}, "is required"),
        ({**base, "guest_id": 42}, "opaque identifier"),
        ({**base, "opportunity_id": "bad id"}, "opaque identifier"),
        ({**base, "season": 7}, "short opaque label"),
        ({**base, "disposition": 7}, "disposition must be text"),
        ({**base, "disposition": "MAYBE"}, "disposition must be one of"),
        ({**base, "date": 7}, "calendar date"),
        ({**base, "date": "2024-13-40"}, "calendar date"),
        ({**base, "date": (NOW + timedelta(days=2)).date().isoformat()}, "future"),
        ({**base, "notes": 42}, "notes must be text"),
        ({**base, "notes": "   "}, "notes cannot be empty"),
        ({**base, "notes": "x" * 2_001}, "exceed"),
        ({**base, "notes": "bad\x00note"}, "control"),
        ({**base, "notes": "ok", "notes_ref": "vault://x/1"}, "never both"),
        ({**base, "notes_ref": "not-a-reference"}, "opaque owner reference"),
        ({**base, "notes_ref": "registry://guest/555-123-4567"}, "contact data"),
    ):
        with pytest.raises(guest_crm.GuestHistoryError, match=message):
            guest_crm.GuestHistoryInput.from_mapping(invalid, now=NOW)

    parsed = guest_crm.GuestHistoryInput.from_mapping({**base, "notes": PRIVATE_NOTE}, now=NOW)
    assert parsed.disposition == "SOFT_DECLINE"

    with pytest.raises(guest_crm.GuestHistoryError, match="cannot record"):
        guest_crm.record_history(
            conn,
            parsed,
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="network_fixture",
            actor_role="network_operator",
            field_vault=vault,
            now=NOW,
        )
    with pytest.raises(guest_crm.GuestHistoryError, match="custody is not configured"):
        guest_crm.record_history(
            conn,
            parsed,
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    with pytest.raises(guest_crm.GuestHistoryError, match="not found"):
        guest_crm.record_history(
            conn,
            guest_crm.GuestHistoryInput.from_mapping({**base, "opportunity_id": "missing-opportunity"}, now=NOW),
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            field_vault=vault,
            now=NOW,
        )
    with pytest.raises(guest_crm.GuestHistoryError, match="does not match"):
        guest_crm.record_history(
            conn,
            guest_crm.GuestHistoryInput.from_mapping(
                {**base, "guest_id": "crm:other", "opportunity_id": opportunity_id},
                now=NOW,
            ),
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            field_vault=vault,
            now=NOW,
        )

    recorded = guest_crm.record_history(
        conn,
        parsed,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_id="producer_fixture",
        actor_role="producer",
        field_vault=vault,
        now=NOW,
    )
    assert recorded["notes"] == PRIVATE_NOTE
    assert recorded["notes_available"] is True
    assert recorded["notes_ref"].startswith("private-field://")
    assert recorded["opportunity_id"] == opportunity_id
    repeat = guest_crm.record_history(
        conn,
        parsed,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_id="producer_fixture",
        actor_role="producer",
        field_vault=vault,
        now=NOW,
    )
    assert repeat["history_id"] == recorded["history_id"]
    with pytest.raises(guest_crm.GuestHistoryError, match="different evidence"):
        guest_crm.record_history(
            conn,
            guest_crm.GuestHistoryInput.from_mapping({**base, "notes": "Different."}, now=NOW),
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            field_vault=vault,
            now=NOW,
        )

    listed = guest_crm.list_history(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="producer",
        guest_id=GUEST,
        opportunity_id=opportunity_id,
        season="S1",
        field_vault=vault,
    )
    assert [item["history_id"] for item in listed] == [recorded["history_id"]]
    assert guest_crm.list_history(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer", season="S9") == []
    assert guest_crm.list_history(conn, tenant_id=TENANT, show_id=OTHER_SHOW, actor_role="producer") == []
    assert (
        guest_crm.list_history(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer", guest_id=GUEST)[0][
            "notes_available"
        ]
        is False
    )

    for kwargs, message in (
        ({"actor_role": "network_operator"}, "cannot view"),
        ({"actor_role": "producer", "limit": True}, "limit must be"),
        ({"actor_role": "producer", "limit": 0}, "limit must be"),
    ):
        with pytest.raises(guest_crm.GuestHistoryError, match=message):
            guest_crm.list_history(conn, tenant_id=TENANT, show_id=SHOW, **kwargs)

    row = store.fetch_one(conn, "SELECT * FROM guest_history WHERE id = ?", (recorded["history_id"],))
    with pytest.raises(guest_crm.GuestHistoryError, match="cannot view"):
        guest_crm.reveal_history(conn, row, actor_role="network_operator")
    with pytest.raises(guest_crm.GuestHistoryError, match="unavailable"):
        guest_crm.reveal_history(conn, {**row, "show_id": OTHER_SHOW}, actor_role="producer", field_vault=vault)
    store.update(conn, "guest_history", row["id"], {"notes_checksum": "0" * 64})
    conn.commit()
    tampered = store.fetch_one(conn, "SELECT * FROM guest_history WHERE id = ?", (row["id"],))
    with pytest.raises(guest_crm.GuestHistoryError, match="checksum"):
        guest_crm.reveal_history(conn, tampered, actor_role="producer", field_vault=vault)

    with pytest.raises(guest_crm.GuestHistoryError, match="must be a boolean"):
        guest_crm.set_directive(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            guest_id=GUEST,
            do_not_contact="yes",
            actor_id="ari_fixture",
            actor_role="relationship_owner",
        )
    with pytest.raises(service.ValidationError, match="reason_ref"):
        service.DoNotContactUpdate.from_dict({"do_not_contact": True, "reason_ref": "not-a-reference"})
    for invalid, message in (
        ([], "must be an object"),
        ({"do_not_contact": "true"}, "must be a boolean"),
        ({"do_not_contact": True, "extra": 1}, "unsupported fields"),
    ):
        with pytest.raises(service.ValidationError, match=message):
            service.DoNotContactUpdate.from_dict(invalid)

    real_insert = store.insert

    def fail_history(connection, table, values):
        if table == "guest_history":
            raise RuntimeError("synthetic database rejection")
        return real_insert(connection, table, values)

    monkeypatch.setattr(guest_crm.store, "insert", fail_history)
    with pytest.raises(guest_crm.GuestHistoryError, match="write was rejected"):
        guest_crm.record_history(
            conn,
            guest_crm.GuestHistoryInput.from_mapping(
                {**base, "season": "S2", "notes_ref": "registry://guest/s2"}, now=NOW
            ),
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    monkeypatch.undo()

    class FailingVault(encryption.FieldVault):
        def put_text(self, conn, scope, value, *, commit=True):
            super().put_text(conn, scope, value, commit=commit)
            raise encryption.EncryptionConfigurationError("synthetic custody outage")

    with pytest.raises(guest_crm.GuestHistoryError, match="encryption is unavailable"):
        guest_crm.record_history(
            conn,
            guest_crm.GuestHistoryInput.from_mapping(
                {**base, "season": "S0", "notes": "Synthetic outage note."}, now=NOW
            ),
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            field_vault=FailingVault(_vault().keys),
            now=NOW,
        )
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM guest_history WHERE season = 'S0'")["count"] == 0
    conn.close()


def test_legacy_projection_badge_and_public_history_edges(tmp_path: Path) -> None:
    _, conn, _unused, opportunity_id = _fixture(tmp_path)
    assert guest_crm.badge([]) is None
    assert guest_crm.badge([{"source": guest_crm.DIRECTIVE_SOURCE}]) is None
    legacy = {
        "id": "legacy-history-24",
        "tenant_id": TENANT,
        "show_id": SHOW,
        "guest_id": GUEST,
        "season": "S0",
        "episode": "E0",
        "disposition": "NO_RESPONSE",
        "occurred_at": "2022-04-01T00:00:00+00:00",
        "notes_ref": "vault://legacy/history-24",
        "do_not_contact": 0,
        "created_at": "2022-04-01T00:00:00+00:00",
    }
    store.insert(conn, "guest_history", legacy)
    conn.commit()
    projected = guest_crm.reveal_history(conn, legacy, actor_role="producer")
    assert projected["date"] == "2022-04-01"
    assert projected["source"] == "legacy"
    assert projected["recorded_by"] == "legacy"
    assert projected["notes"] is None
    assert projected["notes_available"] is False
    entries = guest_crm.public_history(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST, limit=500)
    assert guest_crm.badge(entries) == "Previously: S0E0 — NO_RESPONSE (2022-04-01)"
    assert guest_crm.guest_identity({"id": "x", "source_key": "sk"}) == "sk"
    assert guest_crm.guest_identity({"id": "x"}) == "x"
    del opportunity_id
    conn.close()


# ---------------------------------------------------------------------------
# 5. API surface, show scope, and role denials.
# ---------------------------------------------------------------------------


def test_api_guest_history_directive_scope_and_role_denials(tmp_path: Path) -> None:
    database, conn, vault, opportunity_id = _fixture(tmp_path)
    other_opportunity_id = _import(
        conn,
        [
            _candidate(
                source_key="crm:synthetic:issue-24-other",
                guest_id="crm:synthetic:issue-24-other",
                guest_name="Synthetic Other Show Guest",
            )
        ],
        show_id=OTHER_SHOW,
    ).created_ids[0]
    conn.commit()
    conn.close()

    owner_token = "synthetic-issue-24-owner-token"  # allow-secret: fixture
    producer_token = "synthetic-issue-24-producer-token"  # allow-secret: fixture
    network_token = "synthetic-issue-24-network-token"  # allow-secret: fixture
    other_token = "synthetic-issue-24-other-token"  # allow-secret: fixture
    app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        csrf_secret=b"synthetic-csrf-secret-issue-24-32",  # allow-secret: fixture
        field_vault=vault,
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {
                owner_token: ("ari_fixture", "relationship_owner", TENANT),
                producer_token: ("producer_fixture", "producer", TENANT),
                network_token: ("network_fixture", "network_operator", TENANT),
                other_token: ("producer_other", "producer", "other_tenant"),
            }
        ),
    )
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    producer_headers = {"Authorization": f"Bearer {producer_token}"}
    network_headers = {"Authorization": f"Bearer {network_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}

    with TestClient(app) as client:
        context = client.get("/v1/operator-context", headers=producer_headers).json()
        assert "SOFT_DECLINE" in context["guest_history_dispositions"]

        created = client.post(
            f"/v1/shows/{SHOW}/guest-history",
            headers=producer_headers,
            json={
                "opportunity_id": opportunity_id,
                "season": "S1",
                "episode": "E5",
                "disposition": "SOFT_DECLINE",
                "date": "2024-03-15",
                "notes": PRIVATE_NOTE,
            },
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store, private"
        assert created.json()["notes"] == PRIVATE_NOTE
        assert created.json()["recorded_by"] == "producer_fixture"

        listing = client.get(
            f"/v1/shows/{SHOW}/guest-history",
            headers=producer_headers,
            params={"guest_id": GUEST, "season": "S1"},
        )
        assert listing.status_code == 200
        assert listing.headers["cache-control"] == "no-store, private"
        assert [item["history_id"] for item in listing.json()] == [created.json()["history_id"]]
        timeline = client.get(
            f"/v1/opportunities/{opportunity_id}/guest-history",
            headers=producer_headers,
        )
        assert timeline.status_code == 200
        assert timeline.json()[0]["notes"] == PRIVATE_NOTE
        assert client.get(f"/v1/shows/{SHOW}/guest-history", headers=network_headers).status_code == 403

        denied = client.put(
            f"/v1/opportunities/{opportunity_id}/do-not-contact",
            headers=producer_headers,
            json={"do_not_contact": True},
        )
        assert denied.status_code == 403
        assert "relationship owner" in denied.json()["detail"]
        assert (
            client.put(
                f"/v1/opportunities/{opportunity_id}/do-not-contact",
                headers=owner_headers,
                json={"do_not_contact": "true"},
            ).status_code
            == 422
        )

        applied = client.put(
            f"/v1/opportunities/{opportunity_id}/do-not-contact",
            headers=owner_headers,
            json={"do_not_contact": True, "reason_ref": "registry://guest/opt-out-24"},
        )
        assert applied.status_code == 200
        assert applied.headers["cache-control"] == "no-store, private"
        assert applied.json()["do_not_contact"] is True
        queue = client.get("/v1/approval-queue", headers=owner_headers).json()
        assert [row["do_not_contact"] for row in queue if row["id"] == opportunity_id] == [True]

        # Cross-show and cross-tenant custody both fail closed.
        assert (
            client.get(
                f"/v1/opportunities/{other_opportunity_id}/guest-history",
                headers=producer_headers,
                params={"show": OTHER_SHOW},
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/v1/opportunities/{opportunity_id}/do-not-contact",
                headers=other_headers,
                json={"do_not_contact": True},
            ).status_code
            == 403
        )
        # A different tenant may write into its OWN scope, and still reads
        # nothing of this tenant's memory: the show path is not custody.
        tenant_timeline = [
            item["history_id"]
            for item in client.get(f"/v1/shows/{SHOW}/guest-history", headers=producer_headers).json()
        ]
        assert created.json()["history_id"] in tenant_timeline
        assert client.get(f"/v1/shows/{SHOW}/guest-history", headers=other_headers).json() == []
        assert (
            client.post(
                f"/v1/shows/{SHOW}/guest-history",
                headers=other_headers,
                json={
                    "guest_id": GUEST,
                    "season": "S1",
                    "episode": "E6",
                    "disposition": "DECLINED",
                    "date": "2024-04-15",
                },
            ).status_code
            == 201
        )
        assert [
            item["history_id"]
            for item in client.get(f"/v1/shows/{SHOW}/guest-history", headers=producer_headers).json()
        ] == tenant_timeline


# ---------------------------------------------------------------------------
# 6. Schema, dashboard, and documentation surface.
# ---------------------------------------------------------------------------


def test_migration_schema_dashboard_and_documentation_surface(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue24-schema.sqlite3")
    ledger = {row["version"]: row["name"] for row in migrations.applied_migrations(conn)}
    assert ledger[17] == "guest_crm_cross_season_memory"
    assert migrations.current_version(conn) == migrations.LATEST_VERSION
    opportunity_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(appearance_opportunities)")}
    assert {"guest_id", "season", "episode"} <= opportunity_columns
    history_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(guest_history)")}
    assert {
        "guest_id",
        "season",
        "episode",
        "disposition",
        "date",
        "notes_ref",
        "opportunity_id",
        "notes_checksum",
        "source",
        "recorded_by",
        "recorded_by_role",
    } <= history_columns
    directive_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(guest_directives)")}
    assert {"guest_id", "do_not_contact", "reason_ref", "set_by", "set_by_role"} <= (directive_columns)
    conn.close()

    root = Path(__file__).resolve().parents[2]
    candidate_schema = json.loads((root / "spec" / "candidate.schema.json").read_text(encoding="utf-8"))
    assert {"guest_id", "do_not_contact", "season", "episode"} <= set(candidate_schema["properties"])
    assert candidate_schema["properties"]["do_not_contact"]["type"] == "boolean"
    receipt_schema = json.loads((root / "spec" / "receipt.schema.json").read_text(encoding="utf-8"))
    assert "guest_history_entry" in receipt_schema["properties"]["kind"]["enum"]
    history_branch = next(
        branch["then"]
        for branch in receipt_schema["allOf"]
        if branch["if"]["properties"]["kind"]["const"] == "guest_history_entry"
    )
    assert {
        "guest_id",
        "season",
        "episode",
        "disposition",
        "date",
        "recorded_by",
        "recorded_by_role",
    } <= set(history_branch["required"])
    opportunity_schema = json.loads((root / "spec" / "appearance_opportunity.schema.json").read_text(encoding="utf-8"))
    assert {"guest_id", "season", "episode", "do_not_contact", "guest_history_badge"} <= (
        set(opportunity_schema["properties"])
    )

    for relative in (
        "dashboard/assets/api.js",
        "dashboard/assets/app.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/guide.json",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/styles.css",
        "config/domain_kernel.yaml",
        "spec/candidate.schema.json",
        "spec/receipt.schema.json",
        "spec/appearance_opportunity.schema.json",
    ):
        assert (root / relative).read_bytes() == (root / "hospes" / "resources" / relative).read_bytes(), relative

    api_javascript = (root / "dashboard/assets/api.js").read_text(encoding="utf-8")
    for symbol in (
        "loadGuestHistory",
        "loadShowGuestHistory",
        "saveGuestHistory",
        "setDoNotContact",
    ):
        assert symbol in api_javascript
    capabilities = (root / "dashboard/assets/capabilities.mjs").read_text(encoding="utf-8")
    assert "canSetDoNotContact" in capabilities
    assert "canViewGuestHistory" in capabilities
    queue_javascript = (root / "dashboard/assets/app.js").read_text(encoding="utf-8")
    assert "guest_history_badge" in queue_javascript
    assert "data-guest-memory" in queue_javascript
    assert 'data-action="${candidate.do_not_contact' in queue_javascript
    workspace = (root / "dashboard/assets/partnership-workspace.js").read_text(encoding="utf-8")
    assert "refreshGuestRegister" in workspace
    assert "saveGuestHistory(opportunity.show_id, data)" in workspace
    markup = (root / "dashboard/assets/partnership-workspace.html").read_text(encoding="utf-8")
    assert 'id="guest-register-panel"' in markup
    assert 'id="guest-history-timeline"' in markup
    assert 'id="guest-history-form"' in markup
    assert 'data-guide="guest-register"' in markup
    guide = json.loads((root / "dashboard/assets/guide.json").read_text(encoding="utf-8"))
    assert "guest-register" in guide["elements"]
    assert {"canViewGuestHistory", "canSetDoNotContact"} <= set(guide["capabilities"])

    domain = (root / "config" / "domain_kernel.yaml").read_text(encoding="utf-8")
    assert "guest_crm_contract:" in domain
    assert "GuestContactDirective" in domain
    for event in (
        "guest_history.recorded",
        "guest.do_not_contact_set",
        "guest.contact_permitted",
    ):
        assert event in domain
    assert "only a relationship owner may set or lift it" in domain

    documentation = (root / "docs" / "guest-crm.md").read_text(encoding="utf-8")
    for phrase in (
        "opaque guest identity",
        "Previously: S2E3 — SOFT_DECLINE (2024-03-15)",
        "Only a relationship owner may write it",
        "informational only",
        "Cache-Control: no-store",
        "no row in the batch is written",
    ):
        assert phrase in documentation, phrase
    assert "docs/guest-crm.md" in (root / "README.md").read_text(encoding="utf-8")


def test_directive_history_dates_are_the_directive_day(tmp_path: Path) -> None:
    _, conn, _unused, opportunity_id = _fixture(tmp_path)
    guest_crm.set_directive(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        guest_id=GUEST,
        do_not_contact=True,
        actor_id="ari_fixture",
        actor_role="relationship_owner",
        opportunity_id=opportunity_id,
        now=NOW,
    )
    entry = guest_crm.public_history(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST)[0]
    assert entry["date"] == NOW.date().isoformat()
    assert entry["season"] == guest_crm.DIRECTIVE_SEASON
    assert entry["episode"] == guest_crm.DIRECTIVE_EPISODE
    assert entry["do_not_contact"] is True
    assert date.fromisoformat(entry["date"]) == NOW.date()
    conn.close()
