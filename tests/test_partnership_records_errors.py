"""Deterministic coverage for every partnership-record rejection boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hospes import partnership_records
from hospes.partnerships import PartnershipError, PartnershipItemInput

TENANT = "private_pilot"
PARTNERSHIP_ID = "partnership-fixture"
NOW = datetime(2026, 7, 22, 18, 0, tzinfo=UTC)


def item_input(item_key: str = "fixture.item") -> PartnershipItemInput:
    return PartnershipItemInput.from_dict(
        {
            "item_key": item_key,
            "category": "decision",
            "title": "Synthetic decision",
            "summary": "A bounded synthetic decision used only for validation.",
            "owner": "Partners",
            "state": "unknown",
            "external_reference": "fixture://partnership/item",
        }
    )


def allow_partnership(monkeypatch) -> None:
    monkeypatch.setattr(
        partnership_records,
        "_partnership",
        lambda *_args, **_kwargs: {"partnership_key": "fixture", "show_id": "private_pilot"},
    )


def test_owner_role_is_required() -> None:
    with pytest.raises(PartnershipError, match="explicit owner role"):
        partnership_records._require_owner_role("viewer")


def test_create_item_rejects_duplicate_key(monkeypatch) -> None:
    allow_partnership(monkeypatch)
    monkeypatch.setattr(
        partnership_records.store,
        "fetch_one",
        lambda *_args, **_kwargs: {"id": "existing"},
    )
    with pytest.raises(PartnershipError, match="item_key already exists"):
        partnership_records.create_item(
            None,
            PARTNERSHIP_ID,
            item_input(),
            tenant_id=TENANT,
            actor_id="producer_fixture",
            actor_role="producer",
        )


@pytest.mark.parametrize(
    ("existing", "payload", "expected_revision", "message"),
    [
        (None, item_input(), 1, "partnership item not found"),
        (
            {"state": "superseded", "item_key": "fixture.item", "revision": 1},
            item_input(),
            1,
            "superseded items are immutable",
        ),
        (
            {"state": "current", "item_key": "fixture.item", "revision": 1},
            item_input("fixture.changed"),
            1,
            "item_key cannot change",
        ),
        (
            {"state": "current", "item_key": "fixture.item", "revision": 1},
            item_input(),
            0,
            "positive integer",
        ),
        (
            {"state": "current", "item_key": "fixture.item", "revision": 2},
            item_input(),
            1,
            "stale partnership item revision",
        ),
    ],
)
def test_update_item_rejection_branches(
    monkeypatch,
    existing: dict | None,
    payload: PartnershipItemInput,
    expected_revision: int,
    message: str,
) -> None:
    allow_partnership(monkeypatch)
    monkeypatch.setattr(
        partnership_records.store,
        "fetch_one",
        lambda *_args, **_kwargs: existing,
    )
    with pytest.raises(PartnershipError, match=message):
        partnership_records.update_item(
            None,
            PARTNERSHIP_ID,
            "item-fixture",
            payload,
            expected_revision=expected_revision,
            tenant_id=TENANT,
            actor_id="producer_fixture",
            actor_role="producer",
        )


@pytest.mark.parametrize(
    ("item_id", "successor_id", "existing", "successor", "revision", "message"),
    [
        ("item", "next", None, {"state": "current"}, 1, "item or successor not found"),
        (
            "same",
            "same",
            {"state": "current", "revision": 1},
            {"state": "current"},
            1,
            "cannot supersede itself",
        ),
        (
            "item",
            "next",
            {"state": "current", "revision": 1},
            {"state": "superseded"},
            1,
            "successor item is already superseded",
        ),
        (
            "item",
            "next",
            {"state": "superseded", "revision": 1},
            {"state": "current"},
            1,
            "item is already superseded",
        ),
        (
            "item",
            "next",
            {"state": "current", "revision": 2},
            {"state": "current"},
            1,
            "stale partnership item revision",
        ),
    ],
)
def test_supersede_item_rejection_branches(
    monkeypatch,
    item_id: str,
    successor_id: str,
    existing: dict | None,
    successor: dict,
    revision: int,
    message: str,
) -> None:
    allow_partnership(monkeypatch)
    records = iter([existing, successor])
    monkeypatch.setattr(
        partnership_records.store,
        "fetch_one",
        lambda *_args, **_kwargs: next(records),
    )
    with pytest.raises(PartnershipError, match=message):
        partnership_records.supersede_item(
            None,
            PARTNERSHIP_ID,
            item_id,
            successor_item_id=successor_id,
            expected_revision=revision,
            tenant_id=TENANT,
            actor_id="producer_fixture",
            actor_role="producer",
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "resource_type": "unsupported",
                "resource_reference": "fixture://resource/item",
                "label": "Synthetic item",
            },
            "resource_type must be one of",
        ),
        (
            {
                "resource_type": "issue",
                "resource_reference": "producer@example.com",
                "label": "Synthetic item",
            },
            "safe opaque reference",
        ),
        (
            {
                "resource_type": "issue",
                "resource_reference": "fixture://resource/item",
                "label": "producer@example.com",
            },
            "label contains private content",
        ),
    ],
)
def test_resource_link_validation_branches(monkeypatch, payload: dict, message: str) -> None:
    allow_partnership(monkeypatch)
    with pytest.raises(PartnershipError, match=message):
        partnership_records.link_resource(
            None,
            PARTNERSHIP_ID,
            payload,
            tenant_id=TENANT,
            actor_id="producer_fixture",
            actor_role="producer",
        )


def test_resource_link_rejects_missing_item(monkeypatch) -> None:
    allow_partnership(monkeypatch)
    monkeypatch.setattr(
        partnership_records.store,
        "fetch_one",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(PartnershipError, match="linked partnership item not found"):
        partnership_records.link_resource(
            None,
            PARTNERSHIP_ID,
            {
                "resource_type": "issue",
                "resource_reference": "fixture://resource/item",
                "label": "Synthetic item",
                "item_id": "missing-item",
            },
            tenant_id=TENANT,
            actor_id="producer_fixture",
            actor_role="producer",
        )


def review_payload(**overrides) -> dict:
    payload = {
        "review_kind": "ari_review",
        "decisions_count": 3,
        "coverage_met": 3,
        "coverage_total": 3,
        "external_reference": "fixture://review/ari",
        "occurred_at": (NOW - timedelta(minutes=1)).isoformat(),
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("payload", "actor_role", "message"),
    [
        (review_payload(review_kind="unknown"), "relationship_owner", "review_kind"),
        (review_payload(), "producer", "requires the relationship owner"),
        *[
            (review_payload(**{field: value}), "relationship_owner",
             "review counts must be integers")
            for field in ("decisions_count", "coverage_met", "coverage_total")
            for value in (True, False)
        ],
        (
            review_payload(decisions_count="three"),
            "relationship_owner",
            "review counts must be integers",
        ),
        (
            review_payload(coverage_met=4, coverage_total=3),
            "relationship_owner",
            "coverage_met cannot exceed",
        ),
        (
            review_payload(external_reference="producer@example.com"),
            "relationship_owner",
            "safe opaque reference",
        ),
        (
            review_payload(review_kind="technical_rehearsal", external_reference=None),
            "producer",
            "requires an opaque external evidence reference",
        ),
        (
            review_payload(occurred_at="not-a-date"),
            "relationship_owner",
            "ISO timestamp",
        ),
        (
            review_payload(occurred_at="2026-07-22T17:59:00"),
            "relationship_owner",
            "include a timezone",
        ),
        (
            review_payload(occurred_at=(NOW + timedelta(minutes=1)).isoformat()),
            "relationship_owner",
            "cannot be in the future",
        ),
    ],
)
def test_review_rejection_branches(monkeypatch, payload: dict, actor_role: str, message: str) -> None:
    allow_partnership(monkeypatch)
    with pytest.raises(PartnershipError, match=message):
        partnership_records.record_review(
            None,
            PARTNERSHIP_ID,
            payload,
            tenant_id=TENANT,
            actor_id="ari_owner",
            actor_role=actor_role,
            now=NOW,
        )


def select_candidate(
    monkeypatch,
    *,
    actor_role: str = "relationship_owner",
    slot: int = 1,
    opportunity: dict | None = None,
) -> None:
    allow_partnership(monkeypatch)
    monkeypatch.setattr(
        partnership_records.store,
        "fetch_one",
        lambda *_args, **_kwargs: opportunity,
    )
    partnership_records.select_pilot_candidate(
        None,
        PARTNERSHIP_ID,
        "opportunity-fixture",
        slot,
        tenant_id=TENANT,
        actor_id="ari_owner",
        actor_role=actor_role,
    )


def eligible_opportunity(**overrides) -> dict:
    opportunity = {
        "disposition": "APPROVED",
        "status": "APPROVED",
        "relationship_class": "C2",
    }
    opportunity.update(overrides)
    return opportunity


def test_candidate_selection_requires_owner_role(monkeypatch) -> None:
    with pytest.raises(PartnershipError, match="host or explicit owner"):
        select_candidate(monkeypatch, actor_role="viewer")


def test_candidate_selection_rejects_invalid_slot(monkeypatch) -> None:
    with pytest.raises(PartnershipError, match="slot must be"):
        select_candidate(monkeypatch, slot=4)


def test_candidate_selection_rejects_missing_opportunity(monkeypatch) -> None:
    with pytest.raises(PartnershipError, match="opportunity not found"):
        select_candidate(monkeypatch)


def test_candidate_selection_requires_approved_disposition(monkeypatch) -> None:
    with pytest.raises(PartnershipError, match="only an approved candidate"):
        select_candidate(
            monkeypatch,
            opportunity=eligible_opportunity(disposition="PENDING"),
        )


def test_candidate_selection_rejects_ineligible_status(monkeypatch) -> None:
    with pytest.raises(PartnershipError, match="cannot enter the pilot slate"):
        select_candidate(
            monkeypatch,
            opportunity=eligible_opportunity(status="DECLINED"),
        )


def test_candidate_selection_rejects_protected_relationship(monkeypatch) -> None:
    with pytest.raises(PartnershipError, match="C4/C5"):
        select_candidate(
            monkeypatch,
            opportunity=eligible_opportunity(relationship_class="C4"),
        )
