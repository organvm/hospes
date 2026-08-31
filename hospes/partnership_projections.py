"""Computed partnership, coverage, and private-pilot projections."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from . import store
from .partnerships import (
    CATEGORIES,
    PILOT_SLOT_INELIGIBLE_STATES,
    PILOT_SLOT_LABELS,
    _OPAQUE_REFERENCE,
    _partnership,
)
from .service import REQUIRED_PREFLIGHT_ASSETS

def command_center(
    conn: sqlite3.Connection, partnership_id: str, tenant_id: str
) -> dict[str, Any]:
    partnership = _partnership(conn, partnership_id, tenant_id)
    items = store.fetch_all(
        conn,
        "SELECT * FROM partnership_items WHERE partnership_id = ? ORDER BY category, due_at, title",
        (partnership_id,),
    )
    today = date.today().isoformat()
    groups = {category: [] for category in CATEGORIES}
    for item in items:
        groups[item["category"]].append(item)
    current_items = [item for item in items if item["state"] != "superseded"]
    present_categories = {item["category"] for item in current_items}
    covered = [category for category in CATEGORIES if category in present_categories]
    missing = [category for category in CATEGORIES if category not in covered]
    readiness = _pilot_readiness(conn, partnership_id, tenant_id)
    pilot_execution = _pilot_execution(conn, partnership_id, tenant_id)
    linked_resources = store.fetch_all(
        conn,
        "SELECT * FROM partnership_resource_links WHERE partnership_id = ? "
        "ORDER BY resource_type, label",
        (partnership_id,),
    )
    reviews = store.fetch_all(
        conn,
        "SELECT * FROM partnership_reviews WHERE partnership_id = ? "
        "ORDER BY occurred_at DESC",
        (partnership_id,),
    )
    overdue_items = [
        item for item in current_items
        if item["due_at"] and item["due_at"] < today and item["state"] != "done"
    ]
    return {
        "partnership": partnership,
        "summary": {
            "total": len(current_items),
            "superseded": len(items) - len(current_items),
            "done": sum(item["state"] == "done" for item in current_items),
            "active": sum(
                item["state"] in {"current", "agreed", "in_progress"}
                for item in current_items
            ),
            "unknown": sum(item["state"] == "unknown" for item in current_items),
            "blocked": sum(item["state"] == "blocked" for item in current_items),
            "overdue": len(overdue_items),
        },
        "categories": groups,
        "coverage": {
            "covered": len(covered),
            "total": len(CATEGORIES),
            "covered_categories": covered,
            "missing_categories": missing,
        },
        "engine_capabilities": _engine_capabilities(conn),
        "pilot_readiness": readiness,
        "pilot_execution": pilot_execution,
        "candidate_slate": readiness["candidate_slate"],
        "linked_resources": linked_resources,
        "reviews": reviews,
        "template_imports": store.fetch_all(
            conn,
            "SELECT * FROM partnership_template_imports WHERE partnership_id = ? "
            "ORDER BY created_at DESC",
            (partnership_id,),
        ),
        "item_history": store.fetch_all(
            conn,
            "SELECT * FROM partnership_item_revisions WHERE partnership_id = ? "
            "ORDER BY item_id, revision DESC",
            (partnership_id,),
        ),
        "agenda": {
            "decisions_required": [
                item for item in current_items
                if item["category"] == "decision" and item["state"] in {"planned", "unknown", "blocked"}
            ],
            "overdue_obligations": overdue_items,
            "blockers": [item for item in current_items if item["state"] == "blocked"],
            "risks": [item for item in current_items if item["category"] == "risk"],
            "unknowns": [item for item in current_items if item["state"] == "unknown"],
            "next_recording_gate": readiness["next_gate"],
        },
        "recent_events": store.fetch_all(
            conn,
            "SELECT * FROM partnership_audit_events WHERE partnership_id = ? "
            "ORDER BY created_at DESC LIMIT 40",
            (partnership_id,),
        ),
    }


def _pilot_execution(
    conn: sqlite3.Connection, partnership_id: str, tenant_id: str
) -> dict[str, Any]:
    """Return the safe current policy plus an opaque latest-run pointer."""
    policy = store.fetch_one(
        conn,
        "SELECT * FROM pilot_policies WHERE partnership_id = ? AND tenant_id = ? "
        "ORDER BY policy_version DESC, created_at DESC LIMIT 1",
        (partnership_id, tenant_id),
    )
    run = store.fetch_one(
        conn,
        "SELECT * FROM pilot_runs WHERE partnership_id = ? AND tenant_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (partnership_id, tenant_id),
    )
    current_policy = None
    if policy is not None:
        current_policy = {
            "id": policy["id"],
            "policy_key": policy["policy_key"],
            "policy_version": policy["policy_version"],
            "policy_digest": policy["policy_digest"],
            "deadline_at": policy["deadline_at"],
            "timezone": policy["timezone"],
            "candidate_eligibility": {
                "count": policy["candidate_count"],
                "network_id": policy["candidate_network_id"],
                "city": policy["candidate_city"],
                "relationship_classes": policy["allowed_relationship_classes"],
                "max_social_cost": policy["max_social_cost"],
            },
            "follow_up_limit": policy["follow_up_limit"],
            "relationship_exposure_budget": policy[
                "relationship_exposure_budget"
            ],
            "human_authority_rules": policy["human_authority_rules"],
        }
    latest_run = None
    if run is not None:
        latest_run = {
            "id": run["id"],
            "policy_id": run["policy_id"],
            "lifecycle_state": run["lifecycle_state"],
            "revision": run["revision"],
            "deadline_at": run["deadline_at"],
            "updated_at": run["updated_at"],
        }
    return {
        "current_policy": current_policy,
        "latest_run": latest_run,
    }


def _engine_capabilities(conn: sqlite3.Connection) -> dict[str, bool]:
    tables = store.table_names(conn)
    return {
        "candidate_intake": "appearance_opportunities" in tables,
        "human_decisions": "decisions" in tables,
        "draft_preview": "correspondence_drafts" in tables,
        "typed_receipts": "operational_receipts" in tables,
        "partnership_revisions": "partnership_item_revisions" in tables,
        "resource_links": "partnership_resource_links" in tables,
        "bounded_reviews": "partnership_reviews" in tables,
        "candidate_slate": "pilot_candidate_slots" in tables,
        "pilot_planner": "pilot_plan_projections" in tables,
        "runtime_markers": "runtime_metadata" in tables,
    }


def _pilot_readiness(
    conn: sqlite3.Connection, partnership_id: str, tenant_id: str
) -> dict[str, Any]:
    slots = store.fetch_all(
        conn,
        "SELECT s.slot, s.opportunity_id, s.selected_at, o.show_id, o.guest_name, o.status, "
        "o.disposition, o.episode_thesis, o.proposed_artifact, o.relationship_class, "
        "o.relationship_owner, o.social_cost_1_5, o.ari_effort, o.preferred_city, "
        "o.next_action, r.route_type, r.route_label, r.source_provenance AS route_provenance, "
        "r.verified_at AS route_verified_at, r.usable AS route_usable "
        "FROM pilot_candidate_slots s "
        "JOIN appearance_opportunities o ON o.id = s.opportunity_id "
        "LEFT JOIN contact_routes r ON r.opportunity_id = o.id "
        "WHERE s.partnership_id = ? AND s.tenant_id = ? ORDER BY s.slot",
        (partnership_id, tenant_id),
    )
    for slot in slots:
        slot["slot_label"] = PILOT_SLOT_LABELS[int(slot["slot"])]
    opportunity_ids = [slot["opportunity_id"] for slot in slots]
    primary_ids = [
        slot["opportunity_id"] for slot in slots if int(slot["slot"]) == 1
    ]
    placeholders = ",".join("?" for _ in primary_ids)
    receipts: list[dict[str, Any]] = []
    briefs = routes = 0
    primary_packages: list[dict[str, Any]] = []
    if primary_ids:
        params = [tenant_id, *primary_ids]
        receipts = store.fetch_all(
            conn,
            f"SELECT * FROM operational_receipts WHERE tenant_id = ? "
            f"AND opportunity_id IN ({placeholders}) ORDER BY occurred_at",
            params,
        )
        briefs = int(conn.execute(
            f"SELECT COUNT(*) FROM episode_briefs WHERE opportunity_id IN ({placeholders})",
            primary_ids,
        ).fetchone()[0])
        primary_packages = store.fetch_all(
            conn,
            f"SELECT * FROM asset_packages WHERE opportunity_id IN ({placeholders}) "
            "ORDER BY created_at DESC",
            primary_ids,
        )
    if opportunity_ids:
        route_placeholders = ",".join("?" for _ in opportunity_ids)
        routes = int(conn.execute(
            f"SELECT COUNT(*) FROM contact_routes WHERE usable = 1 "
            "AND source_provenance != '' AND verified_at IS NOT NULL "
            f"AND opportunity_id IN ({route_placeholders})",
            opportunity_ids,
        ).fetchone()[0])
    receipt_types = {receipt["receipt_type"] for receipt in receipts}
    review_rows = store.fetch_all(
        conn,
        "SELECT * FROM partnership_reviews WHERE partnership_id = ?",
        (partnership_id,),
    )
    review_kinds = {review["review_kind"] for review in review_rows}
    complete_primary_package = next(
        (
            package
            for package in primary_packages
            if REQUIRED_PREFLIGHT_ASSETS
            <= {asset.get("kind") for asset in package.get("assets", [])}
        ),
        None,
    )
    recurring_window = store.fetch_one(
        conn,
        "SELECT id, external_reference FROM partnership_items "
        "WHERE partnership_id = ? AND tenant_id = ? "
        "AND category = 'decision' AND state IN ('agreed', 'current', 'done') "
        "AND external_reference LIKE 'calendar://%' ORDER BY updated_at DESC LIMIT 1",
        (partnership_id, tenant_id),
    )
    recurring_window_met = bool(
        recurring_window
        and recurring_window["external_reference"].startswith("calendar://")
        and _OPAQUE_REFERENCE.fullmatch(recurring_window["external_reference"])
    )
    guest_recorded = any(
        receipt["receipt_type"] == "recording.completed"
        and receipt.get("details", {}).get("session_kind") == "guest_pilot"
        for receipt in receipts
    )
    rehearsal_recorded = any(
        receipt["receipt_type"] == "recording.completed"
        and receipt.get("details", {}).get("session_kind") == "technical_rehearsal"
        for receipt in receipts
    ) or "technical_rehearsal" in review_kinds
    checks = [
        {
            "key": "ari_review",
            "label": "Operator review and decisions",
            "met": any(
                review["review_kind"] == "ari_review"
                and int(review["decisions_count"]) >= 3
                for review in review_rows
            ),
        },
        {
            "key": "candidate_slate",
            "label": "One primary and two ordered C2/C3 backups",
            "met": len(slots) == 3
            and all(
                slot["relationship_class"] in {"C2", "C3"}
                and slot["status"] not in PILOT_SLOT_INELIGIBLE_STATES
                for slot in slots
            ),
        },
        {"key": "verified_routes", "label": "Verified route provenance", "met": len(slots) == 3 and routes == 3},
        {"key": "recurring_window", "label": "Agreed recurring recording window", "met": recurring_window_met},
        {"key": "outreach", "label": "Human-sent invitation receipt", "met": "outreach.sent" in receipt_types},
        {"key": "booking", "label": "Studio, producer, and recording time", "met": "booking.confirmed" in receipt_types},
        {"key": "consent", "label": "Private-pilot and clip-scope consent", "met": "consent.signed" in receipt_types},
        {"key": "brief", "label": "Verified brief and segment plan", "met": briefs > 0},
        {"key": "assets", "label": "One complete primary six-kind asset package", "met": complete_primary_package is not None},
        {"key": "preflight", "label": "Recording readiness admitted", "met": "recording.ready" in receipt_types},
        {"key": "technical_rehearsal", "label": "Technical rehearsal with evidence", "met": rehearsal_recorded},
        {"key": "guest_recording", "label": "Private guest pilot recorded", "met": guest_recorded},
        {"key": "media", "label": "Masters and checksums in canonical custody", "met": "media.ingested" in receipt_types},
        {"key": "scorecard", "label": "External pilot scorecard", "met": "pilot_scorecard" in review_kinds},
    ]
    next_check = next((check for check in checks if not check["met"]), None)
    return {
        "checks": checks,
        "met": sum(bool(check["met"]) for check in checks),
        "total": len(checks),
        "ready_for_recording": all(check["met"] for check in checks[:10]),
        "guest_recording_allowed": all(check["met"] for check in checks[:11]),
        "pilot_complete": all(check["met"] for check in checks),
        "technical_rehearsal_recorded": rehearsal_recorded,
        "guest_pilot_recorded": guest_recorded,
        "next_gate": next_check,
        "candidate_slate": slots,
        "complete_primary_asset_package_id": (
            complete_primary_package["id"] if complete_primary_package else None
        ),
    }


__all__ = ["command_center"]
