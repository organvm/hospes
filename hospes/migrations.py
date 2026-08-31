"""Ordered, additive SQLite and PostgreSQL migrations for HOSPES.

The original service store created one monolithic schema on every connection.
That is safe only while the schema never changes.  This module keeps that
schema as migration 1 so existing databases remain valid, then applies later
changes exactly once through a small, stdlib-only migration ledger.

Migrations are intentionally additive: they may create tables, columns, and
indexes, but they do not drop or rewrite operator data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Callable, Mapping

from . import generation

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS appearance_opportunities (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    show_id TEXT NOT NULL,
    guest_name TEXT NOT NULL,
    why_guest TEXT NOT NULL,
    why_now TEXT NOT NULL,
    proposed_artifact TEXT NOT NULL,
    relationship_class TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    disposition TEXT,
    episode_thesis TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_opp_tenant ON appearance_opportunities(tenant_id);
CREATE INDEX IF NOT EXISTS ix_opp_status ON appearance_opportunities(status);

CREATE TABLE IF NOT EXISTS contact_routes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
    route_type TEXT NOT NULL,
    route_label TEXT NOT NULL,
    source_provenance TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    usable INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_route_opp ON contact_routes(opportunity_id);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    note TEXT,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS correspondence_drafts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_routes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
    city TEXT NOT NULL,
    studio_label TEXT NOT NULL,
    routed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episode_briefs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
    thesis TEXT NOT NULL,
    research_claims TEXT NOT NULL,
    segments TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_packages (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
    assets TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DECLARED',
    declared_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    completed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_commit_tenant ON commitments(tenant_id);
CREATE INDEX IF NOT EXISTS ix_commit_status ON commitments(status);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """Raised when the store cannot reach the latest schema version."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Any], None]


def _execute_script(conn: Any, script: str) -> None:
    """Execute a multi-statement script without an implicit transaction commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():  # pragma: no cover - internal migration authoring guard
        raise MigrationError("migration script ended with an incomplete SQL statement")


def _column_names(conn: Any, table: str) -> set[str]:
    if getattr(conn, "backend", "sqlite") == "postgresql":
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchall()
        return {str(row["column_name"] if isinstance(row, Mapping) else row[0]) for row in rows}
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def table_exists(conn: Any, table: str) -> bool:
    """Check catalog presence without issuing a query that can abort PostgreSQL."""
    if getattr(conn, "backend", "sqlite") == "postgresql":
        row = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchone()
        return row is not None
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column(conn: Any, table: str, definition: str) -> None:
    name = definition.split(maxsplit=1)[0]
    if name not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migration_001(conn: Any) -> None:
    _execute_script(conn, BASE_SCHEMA)


def _migration_002(conn: Any) -> None:
    """Add private-import metadata without rewriting existing opportunities."""
    additions = (
        "source_key TEXT",
        "category TEXT",
        "relationship_owner TEXT",
        "preferred_city TEXT",
        "social_cost_1_5 INTEGER CHECK (social_cost_1_5 BETWEEN 1 AND 5)",
        "ari_effort TEXT",
        "next_action TEXT",
        "next_action_date TEXT",
        "source_provenance TEXT",
    )
    for definition in additions:
        _add_column(conn, "appearance_opportunities", definition)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_opp_tenant_source "
        "ON appearance_opportunities(tenant_id, source_key) "
        "WHERE source_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_opp_tenant_owner ON appearance_opportunities(tenant_id, relationship_owner)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_opp_tenant_status ON appearance_opportunities(tenant_id, status)")


def _migration_003(conn: Any) -> None:
    """Add the opaque operational-receipt ledger used by lifecycle services."""
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS operational_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
        receipt_type TEXT NOT NULL,
        external_reference TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, opportunity_id, receipt_type, external_reference)
    );
    CREATE INDEX IF NOT EXISTS ix_receipt_opportunity_occurred
        ON operational_receipts(opportunity_id, occurred_at);
    CREATE INDEX IF NOT EXISTS ix_receipt_tenant_type
        ON operational_receipts(tenant_id, receipt_type);
    """,
    )


def _migration_004(conn: Any) -> None:
    """Add the reusable partnership command-center registry and audit trail."""
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS partnerships (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_key TEXT NOT NULL,
        label TEXT NOT NULL,
        purpose TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(tenant_id, partnership_key)
    );
    CREATE INDEX IF NOT EXISTS ix_partnership_tenant
        ON partnerships(tenant_id, status);

    CREATE TABLE IF NOT EXISTS partnership_items (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        item_key TEXT NOT NULL,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        owner TEXT NOT NULL,
        state TEXT NOT NULL,
        external_reference TEXT,
        due_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(partnership_id, item_key)
    );
    CREATE INDEX IF NOT EXISTS ix_partnership_item_category
        ON partnership_items(partnership_id, category, state);
    CREATE INDEX IF NOT EXISTS ix_partnership_item_due
        ON partnership_items(tenant_id, due_at);

    CREATE TABLE IF NOT EXISTS partnership_audit_events (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_partnership_audit
        ON partnership_audit_events(partnership_id, created_at);
    """,
    )


def _migration_005(conn: Any) -> None:
    """Add revisioned partnership records and computed-pilot support.

    Existing v4 rows remain the live rows.  Each receives an immutable revision
    1 snapshot without adding or rewriting partnership audit events.
    """
    _add_column(conn, "partnership_items", "revision INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "partnership_items", "superseded_at TEXT")
    _add_column(conn, "partnership_items", "superseded_by_item_id TEXT")
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS partnership_item_revisions (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        item_id TEXT NOT NULL REFERENCES partnership_items(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        owner TEXT NOT NULL,
        state TEXT NOT NULL,
        external_reference TEXT,
        due_at TEXT,
        superseded_by_item_id TEXT,
        change_kind TEXT NOT NULL,
        changed_by TEXT NOT NULL,
        changed_role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(item_id, revision)
    );
    CREATE INDEX IF NOT EXISTS ix_partnership_revision_history
        ON partnership_item_revisions(partnership_id, item_id, revision);

    CREATE TABLE IF NOT EXISTS partnership_template_imports (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        template_version INTEGER NOT NULL,
        template_digest TEXT NOT NULL,
        source_reference TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('created', 'adopted', 'unchanged')),
        item_count INTEGER NOT NULL,
        imported_by TEXT NOT NULL,
        imported_role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(partnership_id, template_digest)
    );
    CREATE INDEX IF NOT EXISTS ix_partnership_template_import
        ON partnership_template_imports(partnership_id, created_at);

    CREATE TABLE IF NOT EXISTS partnership_resource_links (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        item_id TEXT REFERENCES partnership_items(id) ON DELETE CASCADE,
        resource_type TEXT NOT NULL,
        resource_reference TEXT NOT NULL,
        label TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(partnership_id, item_id, resource_type, resource_reference)
    );
    CREATE INDEX IF NOT EXISTS ix_partnership_resource
        ON partnership_resource_links(partnership_id, resource_type);

    CREATE TABLE IF NOT EXISTS partnership_reviews (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        review_kind TEXT NOT NULL,
        decisions_count INTEGER NOT NULL,
        coverage_met INTEGER NOT NULL,
        coverage_total INTEGER NOT NULL,
        external_reference TEXT,
        reviewer_id TEXT NOT NULL,
        reviewer_role TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_partnership_review
        ON partnership_reviews(partnership_id, occurred_at);

    CREATE TABLE IF NOT EXISTS pilot_candidate_slots (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id) ON DELETE CASCADE,
        slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 3),
        selected_by TEXT NOT NULL,
        selected_role TEXT NOT NULL,
        selected_at TEXT NOT NULL,
        UNIQUE(partnership_id, slot),
        UNIQUE(partnership_id, opportunity_id)
    );
    CREATE INDEX IF NOT EXISTS ix_pilot_candidate_slot
        ON pilot_candidate_slots(partnership_id, slot);
    """,
    )
    conn.execute(
        "INSERT OR IGNORE INTO partnership_item_revisions ("
        "id, tenant_id, partnership_id, item_id, revision, item_key, category, "
        "title, summary, owner, state, external_reference, due_at, "
        "superseded_by_item_id, change_kind, changed_by, changed_role, created_at"
        ") SELECT id || ':r1', tenant_id, partnership_id, id, 1, item_key, "
        "category, title, summary, owner, state, external_reference, due_at, "
        "NULL, 'migrated', 'schema_migration', 'system', updated_at "
        "FROM partnership_items"
    )


def _migration_006(conn: Any) -> None:
    """Add the universal, revisioned Pilot execution kernel."""
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS pilot_policies (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        policy_key TEXT NOT NULL,
        policy_version INTEGER NOT NULL CHECK(policy_version > 0),
        policy_digest TEXT NOT NULL,
        deadline_at TEXT NOT NULL,
        timezone TEXT NOT NULL,
        candidate_count INTEGER NOT NULL CHECK(candidate_count > 0),
        candidate_network_id TEXT NOT NULL,
        candidate_city TEXT NOT NULL,
        allowed_relationship_classes TEXT NOT NULL,
        max_social_cost INTEGER NOT NULL CHECK(max_social_cost BETWEEN 1 AND 5),
        follow_up_limit INTEGER NOT NULL CHECK(follow_up_limit >= 0),
        initial_response_hours INTEGER NOT NULL CHECK(initial_response_hours > 0),
        follow_up_response_hours INTEGER NOT NULL CHECK(follow_up_response_hours > 0),
        production_gate_durations TEXT NOT NULL,
        safety_reserve_hours INTEGER NOT NULL CHECK(safety_reserve_hours >= 0),
        human_authority_rules TEXT NOT NULL,
        relationship_exposure_budget INTEGER NOT NULL CHECK(relationship_exposure_budget > 0),
        rehearsal_lead_hours INTEGER NOT NULL CHECK(rehearsal_lead_hours > 0),
        created_by TEXT NOT NULL,
        created_role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(partnership_id, policy_key, policy_version),
        UNIQUE(partnership_id, policy_digest)
    );
    CREATE INDEX IF NOT EXISTS ix_pilot_policy_partnership
        ON pilot_policies(partnership_id, policy_version);

    CREATE TABLE IF NOT EXISTS pilot_runs (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        partnership_id TEXT NOT NULL REFERENCES partnerships(id) ON DELETE CASCADE,
        policy_id TEXT NOT NULL REFERENCES pilot_policies(id),
        lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN (
            'ACTIVE', 'PAUSED', 'REHEARSAL_FALLBACK', 'COMPLETED'
        )),
        revision INTEGER NOT NULL CHECK(revision > 0),
        deadline_at TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_pilot_run_partnership
        ON pilot_runs(partnership_id, lifecycle_state, created_at);

    CREATE TABLE IF NOT EXISTS pilot_candidate_assignments (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        pilot_run_id TEXT NOT NULL REFERENCES pilot_runs(id) ON DELETE CASCADE,
        opportunity_id TEXT NOT NULL REFERENCES appearance_opportunities(id),
        slot_order INTEGER NOT NULL CHECK(slot_order > 0),
        relationship_class TEXT NOT NULL,
        social_cost INTEGER NOT NULL CHECK(social_cost BETWEEN 1 AND 5),
        route_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        active_state TEXT NOT NULL CHECK(active_state IN (
            'AVAILABLE', 'ACTIVATION_APPROVED', 'AWAITING_REPLY',
            'FOLLOW_UP_DUE', 'FOLLOW_UP_APPROVED', 'PROMOTION_DUE',
            'REPLIED', 'BOOKED', 'REVISIT_LATER', 'OPTED_OUT',
            'INFEASIBLE', 'PAUSED'
        )),
        not_before TEXT,
        response_due_at TEXT,
        follow_ups_sent INTEGER NOT NULL DEFAULT 0 CHECK(follow_ups_sent >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(pilot_run_id, slot_order),
        UNIQUE(pilot_run_id, opportunity_id)
    );
    CREATE INDEX IF NOT EXISTS ix_pilot_assignment_run_state
        ON pilot_candidate_assignments(pilot_run_id, active_state, slot_order);

    CREATE TABLE IF NOT EXISTS pilot_assignment_events (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        pilot_run_id TEXT NOT NULL REFERENCES pilot_runs(id) ON DELETE CASCADE,
        assignment_id TEXT NOT NULL REFERENCES pilot_candidate_assignments(id) ON DELETE CASCADE,
        run_revision INTEGER NOT NULL CHECK(run_revision > 0),
        event_kind TEXT NOT NULL,
        previous_state TEXT,
        resulting_state TEXT NOT NULL,
        evidence_reference TEXT,
        details TEXT NOT NULL DEFAULT '{}',
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_pilot_assignment_history
        ON pilot_assignment_events(assignment_id, occurred_at, run_revision);

    CREATE TABLE IF NOT EXISTS pilot_plan_projections (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        pilot_run_id TEXT NOT NULL REFERENCES pilot_runs(id) ON DELETE CASCADE,
        run_revision INTEGER NOT NULL CHECK(run_revision > 0),
        action_kind TEXT NOT NULL,
        required_role TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        remaining_slack_minutes INTEGER NOT NULL,
        ranked_action TEXT NOT NULL,
        alternatives TEXT NOT NULL,
        candidate_timing TEXT NOT NULL,
        constraints TEXT NOT NULL,
        rationale TEXT NOT NULL,
        evidence TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(pilot_run_id, run_revision)
    );
    CREATE INDEX IF NOT EXISTS ix_pilot_projection_current
        ON pilot_plan_projections(pilot_run_id, run_revision DESC);

    CREATE TABLE IF NOT EXISTS pilot_decisions (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        pilot_run_id TEXT NOT NULL REFERENCES pilot_runs(id) ON DELETE CASCADE,
        expected_revision INTEGER NOT NULL,
        resulting_revision INTEGER NOT NULL,
        decision_kind TEXT NOT NULL CHECK(decision_kind IN (
            'wait', 'follow_up', 'promote', 'activate_set',
            'fallback_rehearsal', 'pause'
        )),
        decision_payload TEXT NOT NULL,
        resulting_state_changes TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(pilot_run_id, resulting_revision)
    );
    CREATE INDEX IF NOT EXISTS ix_pilot_decision_history
        ON pilot_decisions(pilot_run_id, resulting_revision);
    """,
    )


def _migration_007(conn: Any) -> None:
    """Add an explicit, tenant-scoped runtime marker for non-authority stores."""
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS runtime_metadata (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        metadata_key TEXT NOT NULL,
        metadata_value TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, metadata_key)
    );
    CREATE INDEX IF NOT EXISTS ix_runtime_metadata_tenant
        ON runtime_metadata(tenant_id, metadata_key);
    """,
    )


def _migration_008(conn: Any) -> None:
    """Add the configurable multi-show product layer.

    These tables intentionally sit beside the accepted Phase 0 kernel. They
    are append-friendly registries and workflow projections; existing pilot
    records are never rewritten or reclassified by this migration.
    """
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS show_registry (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        label TEXT NOT NULL,
        config_ref TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id)
    );
    CREATE INDEX IF NOT EXISTS ix_show_registry_tenant ON show_registry(tenant_id, status);

    CREATE TABLE IF NOT EXISTS guest_contact_routes (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        route_type TEXT NOT NULL,
        route_ref TEXT NOT NULL,
        provenance_ref TEXT NOT NULL,
        verified_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, guest_id, route_type, route_ref)
    );
    CREATE INDEX IF NOT EXISTS ix_guest_route_lookup ON guest_contact_routes(tenant_id, show_id, guest_id);

    CREATE TABLE IF NOT EXISTS touchpoint_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        notes_ref TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        initiator TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, guest_id, channel, occurred_at, initiator)
    );

    CREATE TABLE IF NOT EXISTS relationship_edges (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        source_guest_id TEXT NOT NULL,
        target_guest_id TEXT NOT NULL,
        edge_type TEXT NOT NULL,
        relationship_class TEXT NOT NULL,
        provenance_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, source_guest_id, target_guest_id, edge_type)
    );
    CREATE INDEX IF NOT EXISTS ix_relationship_edge_graph ON relationship_edges(tenant_id, show_id, source_guest_id);

    CREATE TABLE IF NOT EXISTS guest_history (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        season TEXT NOT NULL,
        episode TEXT NOT NULL,
        disposition TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        notes_ref TEXT NOT NULL,
        do_not_contact INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_guest_history_lookup ON guest_history(tenant_id, show_id, guest_id, occurred_at);

    CREATE TABLE IF NOT EXISTS sponsors (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        name TEXT NOT NULL,
        contact_ref TEXT NOT NULL,
        terms_ref TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, name)
    );

    CREATE TABLE IF NOT EXISTS sponsorships (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        sponsor_id TEXT NOT NULL REFERENCES sponsors(id) ON DELETE CASCADE,
        episode_id TEXT NOT NULL,
        slot_type TEXT NOT NULL,
        rate_minor INTEGER NOT NULL CHECK(rate_minor >= 0),
        status TEXT NOT NULL DEFAULT 'available',
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, sponsor_id, episode_id, slot_type)
    );

    CREATE TABLE IF NOT EXISTS clearances (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        episode_id TEXT NOT NULL,
        clearance_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        rights_holder_ref TEXT NOT NULL,
        license_terms_ref TEXT NOT NULL,
        cost_minor INTEGER NOT NULL DEFAULT 0 CHECK(cost_minor >= 0),
        due_date TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_clearance_episode ON clearances(tenant_id, show_id, episode_id, status);

    CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        recipient_role TEXT NOT NULL,
        notification_type TEXT NOT NULL,
        title TEXT NOT NULL,
        body_ref TEXT NOT NULL,
        entity_ref TEXT NOT NULL,
        read_at TEXT,
        due_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_notification_queue ON notifications(tenant_id, show_id, recipient_role, read_at, due_at);

    CREATE TABLE IF NOT EXISTS distributions (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        episode_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        metadata TEXT NOT NULL DEFAULT '{}',
        authorization_receipt_ref TEXT,
        idempotency_key TEXT NOT NULL,
        external_id_ref TEXT,
        scheduled_at TEXT,
        published_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, idempotency_key)
    );

    CREATE TABLE IF NOT EXISTS analytics_metrics (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        episode_id TEXT NOT NULL,
        metrics TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        source_receipt_ref TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, provider, episode_id, period_start, period_end)
    );

    CREATE TABLE IF NOT EXISTS portal_tokens (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        token_digest TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS portal_intakes (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        portal_token_id TEXT NOT NULL REFERENCES portal_tokens(id),
        consent_ref TEXT NOT NULL,
        intake_ref TEXT NOT NULL,
        availability_ref TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        UNIQUE(portal_token_id)
    );

    CREATE TABLE IF NOT EXISTS research_jobs (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        provider TEXT NOT NULL,
        brief TEXT NOT NULL DEFAULT '{}',
        citations TEXT NOT NULL DEFAULT '[]',
        counterarguments TEXT NOT NULL DEFAULT '[]',
        cost_minor INTEGER NOT NULL DEFAULT 0 CHECK(cost_minor >= 0),
        max_cost_minor INTEGER NOT NULL CHECK(max_cost_minor >= 0),
        review_receipt_ref TEXT,
        lock_receipt_ref TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS authorization_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        action TEXT NOT NULL,
        subject_ref TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        authorized_by TEXT NOT NULL,
        authorization_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, action, idempotency_key)
    );

    CREATE TABLE IF NOT EXISTS provider_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        capability TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL,
        receipt_ref TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    """,
    )


def _migration_009(conn: Any) -> None:
    """Add scoped hosted-runtime identity, work, session, and receipt tables."""
    for definition in (
        "show_id TEXT",
        "subject_ref TEXT",
        "correlation_id TEXT",
        "idempotency_key TEXT",
    ):
        _add_column(conn, "audit_events", definition)
    for definition in (
        "show_id TEXT",
        "subject_ref TEXT",
        "correlation_id TEXT",
    ):
        _add_column(conn, "partnership_audit_events", definition)
    for definition in (
        "preview_checksum TEXT",
        "policy_version TEXT",
        "authorized_at TEXT",
    ):
        _add_column(conn, "authorization_receipts", definition)
    for definition in (
        "actor_id TEXT",
        "actor_role TEXT",
        "correlation_id TEXT",
        "payload_checksum TEXT",
        "attempt INTEGER NOT NULL DEFAULT 1",
    ):
        _add_column(conn, "provider_receipts", definition)
    _execute_script(
        conn,
        """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_distribution_scope_id
        ON distributions(tenant_id, show_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_authorization_scope_id
        ON authorization_receipts(tenant_id, show_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_authorization_delivery_subject
        ON authorization_receipts(tenant_id, show_id, id, subject_ref, action);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_portal_token_scope_id
        ON portal_tokens(tenant_id, show_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_sponsor_scope_id
        ON sponsors(tenant_id, show_id, id);

    CREATE TABLE IF NOT EXISTS tenant_encryption_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        key_version INTEGER NOT NULL CHECK(key_version > 0),
        algorithm TEXT NOT NULL DEFAULT 'AES-256-GCM',
        wrapped_key_ref TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'retired', 'revoked')),
        created_at TEXT NOT NULL,
        retired_at TEXT,
        UNIQUE(tenant_id, key_version)
    );
    CREATE INDEX IF NOT EXISTS ix_tenant_key_active
        ON tenant_encryption_keys(tenant_id, status, key_version);

    CREATE TABLE IF NOT EXISTS identity_mappings (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT,
        provider TEXT NOT NULL,
        subject_hash TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        FOREIGN KEY(tenant_id, show_id) REFERENCES show_registry(tenant_id, show_id),
        UNIQUE(tenant_id, provider, subject_hash, show_id)
    );
    CREATE INDEX IF NOT EXISTS ix_identity_subject
        ON identity_mappings(tenant_id, provider, subject_hash, revoked_at);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_identity_tenant_subject
        ON identity_mappings(tenant_id, provider, subject_hash)
        WHERE show_id IS NULL;

    CREATE TABLE IF NOT EXISTS assignments (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        entity_ref TEXT NOT NULL,
        assignment_type TEXT NOT NULL,
        assignee_actor_id TEXT,
        assignee_role TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'in_progress', 'done', 'cancelled')),
        due_at TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id) REFERENCES show_registry(tenant_id, show_id),
        UNIQUE(tenant_id, show_id, assignment_type, entity_ref, assignee_role)
    );
    CREATE INDEX IF NOT EXISTS ix_assignment_queue
        ON assignments(tenant_id, show_id, assignee_role, status, due_at);

    CREATE TABLE IF NOT EXISTS background_jobs (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        job_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled')),
        payload_ref TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
        leased_by TEXT,
        lease_token_hash TEXT,
        lease_expires_at TEXT,
        timeout_seconds INTEGER NOT NULL DEFAULT 300 CHECK(timeout_seconds > 0),
        cost_limit_minor INTEGER NOT NULL DEFAULT 0 CHECK(cost_limit_minor >= 0),
        cost_spent_minor INTEGER NOT NULL DEFAULT 0 CHECK(cost_spent_minor >= 0),
        not_before TEXT,
        last_error_ref TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id) REFERENCES show_registry(tenant_id, show_id),
        UNIQUE(tenant_id, show_id, job_type, idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS ix_background_job_lease
        ON background_jobs(status, priority, not_before, lease_expires_at);

    CREATE TABLE IF NOT EXISTS delivery_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        distribution_id TEXT NOT NULL,
        authorization_receipt_id TEXT NOT NULL,
        authorization_action TEXT NOT NULL DEFAULT 'publish'
            CHECK(authorization_action = 'publish'),
        provider TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK(attempt > 0),
        result TEXT NOT NULL,
        external_reference TEXT NOT NULL,
        payload_checksum TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id, distribution_id)
            REFERENCES distributions(tenant_id, show_id, id),
        FOREIGN KEY(tenant_id, show_id, authorization_receipt_id)
            REFERENCES authorization_receipts(tenant_id, show_id, id),
        FOREIGN KEY(tenant_id, show_id, authorization_receipt_id,
                    distribution_id, authorization_action)
            REFERENCES authorization_receipts(tenant_id, show_id, id,
                                               subject_ref, action),
        UNIQUE(tenant_id, show_id, distribution_id, provider, attempt)
    );
    CREATE INDEX IF NOT EXISTS ix_delivery_receipt_subject
        ON delivery_receipts(tenant_id, show_id, distribution_id, completed_at);

    CREATE TABLE IF NOT EXISTS portal_sessions (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        portal_token_id TEXT NOT NULL,
        session_digest TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id, portal_token_id)
            REFERENCES portal_tokens(tenant_id, show_id, id),
        UNIQUE(portal_token_id)
    );
    CREATE INDEX IF NOT EXISTS ix_portal_session_expiry
        ON portal_sessions(tenant_id, show_id, expires_at, revoked_at);
    """,
    )


def _migration_010(conn: Any) -> None:
    """Add tenant-envelope ciphertext and encrypted artifact custody records."""
    for definition in (
        "wrapped_key_ciphertext TEXT",
        "wrap_nonce TEXT",
        "wrap_aad_checksum TEXT",
    ):
        _add_column(conn, "tenant_encryption_keys", definition)
    _execute_script(
        conn,
        """
    UPDATE tenant_encryption_keys
       SET status = 'revoked',
           retired_at = COALESCE(retired_at, created_at)
     WHERE wrapped_key_ciphertext IS NULL
        OR wrap_nonce IS NULL
        OR wrap_aad_checksum IS NULL;

    CREATE UNIQUE INDEX IF NOT EXISTS ux_tenant_active_encryption_key
        ON tenant_encryption_keys(tenant_id)
        WHERE status = 'active';

    CREATE TABLE IF NOT EXISTS private_field_values (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        category TEXT NOT NULL CHECK(category IN (
            'contact', 'correspondence', 'consent', 'financial',
            'private_relationship', 'research', 'artifact_metadata',
            'artifact_content'
        )),
        owner_table TEXT NOT NULL,
        owner_record_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        media_type TEXT NOT NULL,
        algorithm TEXT NOT NULL DEFAULT 'AES-256-GCM'
            CHECK(algorithm = 'AES-256-GCM'),
        key_version INTEGER NOT NULL CHECK(key_version > 0),
        nonce TEXT NOT NULL,
        ciphertext TEXT NOT NULL,
        aad_checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id)
            REFERENCES show_registry(tenant_id, show_id),
        FOREIGN KEY(tenant_id, key_version)
            REFERENCES tenant_encryption_keys(tenant_id, key_version),
        UNIQUE(tenant_id, show_id, owner_table, owner_record_id, field_name)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_private_field_scope_id
        ON private_field_values(tenant_id, show_id, id);
    CREATE INDEX IF NOT EXISTS ix_private_field_owner
        ON private_field_values(
            tenant_id, show_id, category, owner_table, owner_record_id
        );

    CREATE TABLE IF NOT EXISTS artifact_objects (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        backend TEXT NOT NULL CHECK(backend IN ('local', 's3')),
        object_key_ref TEXT NOT NULL UNIQUE,
        content_key_version INTEGER NOT NULL CHECK(content_key_version > 0),
        ciphertext_checksum TEXT NOT NULL,
        content_checksum TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        metadata_field_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('staging', 'ready', 'failed')),
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id)
            REFERENCES show_registry(tenant_id, show_id),
        FOREIGN KEY(tenant_id, content_key_version)
            REFERENCES tenant_encryption_keys(tenant_id, key_version),
        FOREIGN KEY(tenant_id, show_id, metadata_field_id)
            REFERENCES private_field_values(tenant_id, show_id, id)
    );
    CREATE INDEX IF NOT EXISTS ix_artifact_custody
        ON artifact_objects(tenant_id, show_id, status, created_at);
    """,
    )


def _migration_011(conn: Any) -> None:
    """Add executable job policy and immutable attempt receipts."""
    for definition in (
        "provisioned_by TEXT",
        "revoked_by TEXT",
        "revocation_ref TEXT",
    ):
        _add_column(conn, "identity_mappings", definition)
    for definition in (
        "execution_kind TEXT NOT NULL DEFAULT 'operator' "
        "CHECK(execution_kind IN ('operator', 'scheduled', 'internal'))",
        "operation TEXT NOT NULL DEFAULT 'read' CHECK(operation IN ('read', 'prepare'))",
        "payload_checksum TEXT",
        "policy_version TEXT NOT NULL DEFAULT 'jobs-v1'",
        "created_by TEXT",
        "correlation_id TEXT",
        "lease_started_at TEXT",
        "last_heartbeat_at TEXT",
        "result_ref TEXT",
        "completed_at TEXT",
    ):
        _add_column(conn, "background_jobs", definition)
    _execute_script(
        conn,
        """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_identity_global_tenant_wide
        ON identity_mappings(provider, subject_hash)
        WHERE show_id IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS ux_identity_global_show_scope
        ON identity_mappings(provider, subject_hash, show_id)
        WHERE show_id IS NOT NULL;

    CREATE UNIQUE INDEX IF NOT EXISTS ux_background_job_scope_id
        ON background_jobs(tenant_id, show_id, id);
    CREATE INDEX IF NOT EXISTS ix_background_job_worker
        ON background_jobs(leased_by, status, lease_expires_at);

    CREATE TABLE IF NOT EXISTS job_attempt_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK(attempt > 0),
        worker_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN (
            'succeeded', 'retry', 'failed', 'timeout'
        )),
        result_ref TEXT,
        error_ref TEXT,
        cost_minor INTEGER NOT NULL DEFAULT 0 CHECK(cost_minor >= 0),
        payload_checksum TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id, job_id)
            REFERENCES background_jobs(tenant_id, show_id, id),
        UNIQUE(tenant_id, show_id, job_id, attempt)
    );
    CREATE INDEX IF NOT EXISTS ix_job_attempt_receipt_subject
        ON job_attempt_receipts(tenant_id, show_id, job_id, completed_at);
    """,
    )


def _migration_012(conn: Any) -> None:
    """Add encrypted, role-typed contact rosters for candidate opportunities."""
    _execute_script(
        conn,
        """
    DROP INDEX IF EXISTS ux_opp_tenant_source;
    CREATE UNIQUE INDEX IF NOT EXISTS ux_opp_tenant_show_source
        ON appearance_opportunities(tenant_id, show_id, source_key)
        WHERE source_key IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS ux_appearance_scope_id
        ON appearance_opportunities(tenant_id, show_id, id);

    CREATE TABLE IF NOT EXISTS contact_rosters (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        opportunity_id TEXT NOT NULL,
        role_kind TEXT NOT NULL CHECK(role_kind IN (
            'publicist', 'manager', 'agent', 'direct'
        )),
        name_ref TEXT NOT NULL CHECK(name_ref LIKE 'private-field://%'),
        email_ref TEXT CHECK(
            email_ref IS NULL OR email_ref LIKE 'private-field://%'
        ),
        phone_ref TEXT CHECK(
            phone_ref IS NULL OR phone_ref LIKE 'private-field://%'
        ),
        notes_ref TEXT CHECK(
            notes_ref IS NULL OR notes_ref LIKE 'private-field://%'
        ),
        provenance_ref TEXT NOT NULL,
        verified_at TEXT,
        usable INTEGER NOT NULL DEFAULT 0 CHECK(usable IN (0, 1)),
        preferred INTEGER NOT NULL DEFAULT 0 CHECK(preferred IN (0, 1)),
        permission_status TEXT NOT NULL DEFAULT 'pending_verification'
            CHECK(permission_status IN (
                'pending_verification', 'permitted', 'do_not_use', 'opted_out'
            )),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id)
            REFERENCES show_registry(tenant_id, show_id),
        FOREIGN KEY(tenant_id, show_id, opportunity_id)
            REFERENCES appearance_opportunities(tenant_id, show_id, id),
        UNIQUE(tenant_id, show_id, opportunity_id, role_kind)
    );
    CREATE INDEX IF NOT EXISTS ix_contact_roster_opportunity
        ON contact_rosters(
            tenant_id, show_id, opportunity_id, usable, permission_status
        );
    """,
    )


def _migration_013(conn: Any) -> None:
    """Bind encrypted informal touchpoints to live opportunity/audit scope."""
    for definition in (
        "opportunity_id TEXT",
        "partnership_id TEXT",
        "initiator_role TEXT NOT NULL DEFAULT 'legacy'",
        "correlation_id TEXT",
        "notes_checksum TEXT CHECK(notes_checksum IS NULL OR length(notes_checksum) = 64)",
    ):
        _add_column(conn, "touchpoint_receipts", definition)
    _execute_script(
        conn,
        """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_partnership_tenant_id
        ON partnerships(tenant_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_touchpoint_scope_id
        ON touchpoint_receipts(tenant_id, show_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_touchpoint_correlation
        ON touchpoint_receipts(tenant_id, show_id, correlation_id)
        WHERE correlation_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS ix_touchpoint_scope_timeline
        ON touchpoint_receipts(tenant_id, show_id, occurred_at);
    CREATE INDEX IF NOT EXISTS ix_touchpoint_guest_timeline
        ON touchpoint_receipts(tenant_id, show_id, guest_id, occurred_at);
    CREATE INDEX IF NOT EXISTS ix_touchpoint_initiator_timeline
        ON touchpoint_receipts(tenant_id, show_id, initiator, occurred_at);
    CREATE INDEX IF NOT EXISTS ix_touchpoint_partnership_timeline
        ON touchpoint_receipts(tenant_id, partnership_id, occurred_at);
    """,
    )


def _migration_014(conn: Any) -> None:
    """Bind partnership, Pilot, and operator selection state to one show."""
    _add_column(conn, "partnerships", "show_id TEXT NOT NULL DEFAULT 'legacy'")
    _add_column(conn, "partnerships", "template_key TEXT")
    for table in (
        "partnership_items",
        "partnership_item_revisions",
        "partnership_template_imports",
        "partnership_resource_links",
        "partnership_reviews",
        "pilot_candidate_slots",
        "pilot_policies",
        "pilot_runs",
    ):
        _add_column(conn, table, "show_id TEXT NOT NULL DEFAULT 'legacy'")
    for table in (
        "pilot_candidate_assignments",
        "pilot_assignment_events",
        "pilot_plan_projections",
        "pilot_decisions",
    ):
        _add_column(conn, table, "show_id TEXT NOT NULL DEFAULT 'legacy'")

    conn.execute("UPDATE partnerships SET template_key = partnership_key WHERE template_key IS NULL")
    conn.execute(
        "UPDATE partnerships SET show_id = COALESCE("
        "(SELECT MIN(o.show_id) FROM pilot_candidate_slots s "
        " JOIN appearance_opportunities o ON o.id = s.opportunity_id "
        " WHERE s.partnership_id = partnerships.id "
        " AND EXISTS (SELECT 1 FROM show_registry r "
        " WHERE r.tenant_id = partnerships.tenant_id "
        " AND r.show_id = o.show_id AND r.status = 'active') "
        " AND (SELECT COUNT(DISTINCT o2.show_id) FROM pilot_candidate_slots s2 "
        " JOIN appearance_opportunities o2 ON o2.id = s2.opportunity_id "
        " WHERE s2.partnership_id = partnerships.id) = 1 "
        " HAVING COUNT(DISTINCT o.show_id) = 1), "
        "(SELECT MIN(r.show_id) FROM show_registry r "
        " WHERE r.tenant_id = partnerships.tenant_id AND r.status = 'active' "
        " HAVING COUNT(*) = 1), 'legacy') "
        "WHERE show_id = 'legacy'"
    )
    for table in (
        "partnership_items",
        "partnership_audit_events",
        "partnership_item_revisions",
        "partnership_template_imports",
        "partnership_resource_links",
        "partnership_reviews",
        "pilot_candidate_slots",
        "pilot_policies",
        "pilot_runs",
    ):
        conn.execute(
            f"UPDATE {table} SET show_id = COALESCE("
            f"(SELECT p.show_id FROM partnerships p "
            f"WHERE p.id = {table}.partnership_id), 'legacy') "
            "WHERE show_id IS NULL OR show_id = 'legacy'"
        )
    for table in (
        "pilot_candidate_assignments",
        "pilot_assignment_events",
        "pilot_plan_projections",
        "pilot_decisions",
    ):
        conn.execute(
            f"UPDATE {table} SET show_id = COALESCE("
            f"(SELECT r.show_id FROM pilot_runs r "
            f"WHERE r.id = {table}.pilot_run_id), 'legacy') "
            "WHERE show_id = 'legacy'"
        )
    _execute_script(
        conn,
        """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_partnership_scope_id
        ON partnerships(tenant_id, show_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_partnership_show_template
        ON partnerships(tenant_id, show_id, template_key);
    CREATE INDEX IF NOT EXISTS ix_partnership_show_status
        ON partnerships(tenant_id, show_id, status, label);
    CREATE INDEX IF NOT EXISTS ix_partnership_item_show
        ON partnership_items(tenant_id, show_id, partnership_id, state);
    CREATE INDEX IF NOT EXISTS ix_partnership_audit_show
        ON partnership_audit_events(tenant_id, show_id, partnership_id, created_at);
    CREATE INDEX IF NOT EXISTS ix_pilot_slot_show
        ON pilot_candidate_slots(tenant_id, show_id, partnership_id, slot);
    CREATE INDEX IF NOT EXISTS ix_pilot_policy_show
        ON pilot_policies(tenant_id, show_id, partnership_id, policy_version);
    CREATE INDEX IF NOT EXISTS ix_pilot_run_show
        ON pilot_runs(tenant_id, show_id, partnership_id, lifecycle_state);
    CREATE INDEX IF NOT EXISTS ix_pilot_assignment_show
        ON pilot_candidate_assignments(tenant_id, show_id, pilot_run_id, slot_order);
    """,
    )


def _migration_015(conn: Any) -> None:
    """Give rights clearances decision custody, evidence receipts, and identity."""
    for definition in (
        "custody_mode TEXT NOT NULL DEFAULT 'external_reference'",
        "rights_holder_checksum TEXT CHECK(rights_holder_checksum IS NULL OR length(rights_holder_checksum) = 64)",
        "license_terms_checksum TEXT CHECK(license_terms_checksum IS NULL OR length(license_terms_checksum) = 64)",
        "recorded_by TEXT",
        "recorded_by_role TEXT",
        "decided_by TEXT",
        "decided_by_role TEXT",
        "decided_at TEXT",
        "evidence_ref TEXT",
        "correlation_id TEXT",
    ):
        _add_column(conn, "clearances", definition)
    conn.execute("UPDATE clearances SET correlation_id = 'clearance://' || id WHERE correlation_id IS NULL")
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS clearance_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        clearance_id TEXT NOT NULL REFERENCES clearances(id) ON DELETE CASCADE,
        episode_id TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'clearance.recorded', 'clearance.cleared',
            'clearance.denied', 'clearance.reopened'
        )),
        from_status TEXT,
        to_status TEXT NOT NULL CHECK(to_status IN ('pending', 'cleared', 'denied')),
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        evidence_ref TEXT,
        correlation_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_clearance_scope_id
        ON clearances(tenant_id, show_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_clearance_identity
        ON clearances(tenant_id, show_id, episode_id, clearance_type,
                      rights_holder_checksum, license_terms_checksum)
        WHERE rights_holder_checksum IS NOT NULL
          AND license_terms_checksum IS NOT NULL;
    CREATE INDEX IF NOT EXISTS ix_clearance_status_due
        ON clearances(tenant_id, show_id, status, due_date);
    CREATE INDEX IF NOT EXISTS ix_clearance_type_status
        ON clearances(tenant_id, show_id, clearance_type, status);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_clearance_receipt_correlation
        ON clearance_receipts(tenant_id, show_id, correlation_id);
    CREATE INDEX IF NOT EXISTS ix_clearance_receipt_timeline
        ON clearance_receipts(tenant_id, show_id, clearance_id, created_at);
    CREATE INDEX IF NOT EXISTS ix_clearance_receipt_episode
        ON clearance_receipts(tenant_id, show_id, episode_id, created_at);
    """,
    )


def _migration_016(conn: Any) -> None:
    """Add sold/available ad inventory, sponsor claims, and sponsorship receipts.

    Migration 008 shipped ``sponsors`` and ``sponsorships`` as a bare join. That
    pair can record that a sponsor holds a slot but not that a slot *exists and
    is unsold*, so "sold versus available" and the committed-slot publication
    gate were unrepresentable. ``sponsor_slots`` is that missing inventory, and
    ``ux_sponsorship_slot`` makes one episode slot sellable to exactly one
    sponsor.
    """
    for definition in ("category TEXT", "updated_at TEXT"):
        _add_column(conn, "sponsors", definition)
    for definition in (
        "slot_id TEXT",
        "rate_currency TEXT NOT NULL DEFAULT 'USD'",
        "sold_at TEXT",
        "updated_at TEXT",
        "recorded_by TEXT",
        "recorded_by_role TEXT",
    ):
        _add_column(conn, "sponsorships", definition)
    conn.execute("UPDATE sponsors SET updated_at = created_at WHERE updated_at IS NULL")
    conn.execute("UPDATE sponsorships SET updated_at = created_at WHERE updated_at IS NULL")
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS sponsor_slots (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        episode_id TEXT NOT NULL,
        slot_type TEXT NOT NULL,
        rate_minor INTEGER NOT NULL CHECK(rate_minor >= 0),
        rate_currency TEXT NOT NULL DEFAULT 'USD',
        committed INTEGER NOT NULL DEFAULT 0 CHECK(committed IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, episode_id, slot_type)
    );
    CREATE INDEX IF NOT EXISTS ix_sponsor_slot_episode
        ON sponsor_slots(tenant_id, show_id, episode_id, slot_type);

    CREATE TABLE IF NOT EXISTS sponsor_claims (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        sponsor_id TEXT NOT NULL REFERENCES sponsors(id) ON DELETE CASCADE,
        claim TEXT NOT NULL,
        claim_checksum TEXT NOT NULL,
        source_url TEXT NOT NULL,
        verified_date TEXT NOT NULL,
        approved_for_external_use INTEGER NOT NULL DEFAULT 0
            CHECK(approved_for_external_use IN (0, 1)),
        approved_by TEXT,
        approved_by_role TEXT,
        approved_at TEXT,
        recorded_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, sponsor_id, claim_checksum)
    );
    CREATE INDEX IF NOT EXISTS ix_sponsor_claim_sponsor
        ON sponsor_claims(tenant_id, show_id, sponsor_id, approved_for_external_use);

    CREATE TABLE IF NOT EXISTS sponsorship_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        episode_id TEXT,
        sponsor_id TEXT,
        subject_ref TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_sponsorship_receipt_scope
        ON sponsorship_receipts(tenant_id, show_id, created_at);

    CREATE UNIQUE INDEX IF NOT EXISTS ux_sponsorship_slot
        ON sponsorships(tenant_id, show_id, episode_id, slot_type);
    CREATE INDEX IF NOT EXISTS ix_sponsorship_revenue
        ON sponsorships(tenant_id, show_id, episode_id, status);
    """,
    )


def _migration_017(conn: Any) -> None:
    """Bind cross-season guest memory to opportunities and a live DNC directive."""
    for definition in ("guest_id TEXT", "season TEXT", "episode TEXT"):
        _add_column(conn, "appearance_opportunities", definition)
    for definition in (
        "opportunity_id TEXT",
        "date TEXT",
        "notes_checksum TEXT CHECK(notes_checksum IS NULL OR length(notes_checksum) = 64)",
        "source TEXT NOT NULL DEFAULT 'legacy'",
        "recorded_by TEXT NOT NULL DEFAULT 'legacy'",
        "recorded_by_role TEXT NOT NULL DEFAULT 'legacy'",
    ):
        _add_column(conn, "guest_history", definition)
    conn.execute("UPDATE appearance_opportunities SET guest_id = COALESCE(source_key, id) WHERE guest_id IS NULL")
    conn.execute("UPDATE guest_history SET date = substr(occurred_at, 1, 10) WHERE date IS NULL")
    _execute_script(
        conn,
        """
    CREATE INDEX IF NOT EXISTS ix_opportunity_guest
        ON appearance_opportunities(tenant_id, show_id, guest_id);
    CREATE INDEX IF NOT EXISTS ix_guest_history_scope
        ON guest_history(tenant_id, show_id, guest_id, date, source);
    CREATE INDEX IF NOT EXISTS ix_guest_history_opportunity
        ON guest_history(tenant_id, show_id, opportunity_id, date);

    CREATE TABLE IF NOT EXISTS guest_directives (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        do_not_contact INTEGER NOT NULL DEFAULT 0
            CHECK(do_not_contact IN (0, 1)),
        reason_ref TEXT,
        source TEXT NOT NULL DEFAULT 'operator',
        set_by TEXT NOT NULL,
        set_by_role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(tenant_id, show_id, guest_id)
    );
    CREATE INDEX IF NOT EXISTS ix_guest_directive_active
        ON guest_directives(tenant_id, show_id, do_not_contact);
    """,
    )


def _migration_018(conn: Any) -> None:
    """Bind notifications to assignments, read controls, and delivery previews."""
    for definition in (
        "assignment_id TEXT",
        "severity TEXT NOT NULL DEFAULT 'normal' CHECK(severity IN ('normal', 'critical'))",
        "dedupe_key TEXT",
        "read_by TEXT",
        "updated_at TEXT",
    ):
        _add_column(conn, "notifications", definition)
    for definition in ("title TEXT", "completed_at TEXT", "completed_by TEXT"):
        _add_column(conn, "assignments", definition)
    conn.execute("UPDATE notifications SET updated_at = created_at WHERE updated_at IS NULL")
    conn.execute("UPDATE assignments SET title = assignment_type WHERE title IS NULL")
    _execute_script(
        conn,
        """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_scope_id
        ON notifications(tenant_id, show_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_dedupe
        ON notifications(tenant_id, show_id, dedupe_key)
        WHERE dedupe_key IS NOT NULL;
    CREATE INDEX IF NOT EXISTS ix_notification_role_type
        ON notifications(tenant_id, show_id, recipient_role, notification_type, read_at);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_assignment_scope_id
        ON assignments(tenant_id, show_id, id);
    CREATE INDEX IF NOT EXISTS ix_assignment_actor_queue
        ON assignments(tenant_id, show_id, assignee_actor_id, status, due_at);

    CREATE TABLE IF NOT EXISTS notification_deliveries (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        notification_id TEXT NOT NULL,
        channel TEXT NOT NULL CHECK(channel IN ('email', 'webhook')),
        status TEXT NOT NULL DEFAULT 'preview'
            CHECK(status IN ('preview', 'authorized', 'delivered')),
        target_ref TEXT NOT NULL,
        preview_checksum TEXT NOT NULL CHECK(length(preview_checksum) = 64),
        authorization_receipt_ref TEXT,
        delivery_receipt_ref TEXT,
        idempotency_key TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(tenant_id, show_id, notification_id)
            REFERENCES notifications(tenant_id, show_id, id),
        UNIQUE(tenant_id, show_id, idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS ix_notification_delivery_subject
        ON notification_deliveries(tenant_id, show_id, notification_id, status);
    """,
    )


def _migration_019(conn: Any) -> None:
    """Attribute, cost-account, and freeze bounded provider research jobs.

    Research briefs already carried citations. What they could not carry was
    which provider produced each source, what the retrieval cost, which sources
    the allowlist refused, and whether reviewed content was still the content a
    human approved. These columns are additive; existing rows keep their brief.
    """
    for definition in (
        "query_text TEXT",
        "seeds TEXT NOT NULL DEFAULT '[]'",
        "evidence TEXT NOT NULL DEFAULT '[]'",
        "segment_candidates TEXT NOT NULL DEFAULT '[]'",
        "risk_flags TEXT NOT NULL DEFAULT '[]'",
        "background_job_id TEXT",
        "provider_receipt_ref TEXT",
        "content_checksum TEXT",
        "failure_reason TEXT",
        "requested_by TEXT",
        "reviewed_at TEXT",
        "locked_at TEXT",
    ):
        _add_column(conn, "research_jobs", definition)
    _execute_script(
        conn,
        """
    CREATE INDEX IF NOT EXISTS ix_research_job_scope
        ON research_jobs(tenant_id, show_id, guest_id, status);
    CREATE INDEX IF NOT EXISTS ix_research_job_background
        ON research_jobs(tenant_id, show_id, background_job_id);
    CREATE INDEX IF NOT EXISTS ix_provider_receipt_scope
        ON provider_receipts(tenant_id, show_id, capability, created_at);
    """,
    )


def _migration_020(conn: Any) -> None:
    """Give the guest portal its offered dates, date picks, and own receipts.

    Migration 009 shipped ``portal_tokens``/``portal_intakes``/``portal_sessions``
    as custody-reference stubs: a link could be minted and an intake recorded,
    but the operator had nowhere to declare *which* dates a guest may choose
    from, the guest had nowhere to put the three they picked, and the completed
    intake left no attributable receipt. Those three gaps are closed here.

    ``portal_receipts`` is deliberately its own ledger rather than a row in
    ``operational_receipts``. That table is keyed to an ``appearance_opportunities``
    foreign key and carries no ``show_id``, while the portal is scoped
    ``(tenant, show, guest)`` and is reachable by an unauthenticated guest — the
    isolated boundary the portal exists to keep. ``consent.signed`` also already
    means something narrower there (``service.ReceiptType``, with its own detail
    contract and a ``BOOKED -> PREP_IN_PROGRESS`` transition), so reusing that
    table would overload one event name with two shapes.
    """
    for definition in (
        "offered_dates TEXT NOT NULL DEFAULT '[]'",
        "created_by TEXT",
        "created_by_role TEXT",
    ):
        _add_column(conn, "portal_tokens", definition)
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS portal_date_choices (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        portal_intake_id TEXT NOT NULL REFERENCES portal_intakes(id) ON DELETE CASCADE,
        rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 3),
        preferred_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(portal_intake_id, rank),
        UNIQUE(portal_intake_id, preferred_date)
    );
    CREATE INDEX IF NOT EXISTS ix_portal_date_choice_scope
        ON portal_date_choices(tenant_id, show_id, guest_id, rank);

    CREATE TABLE IF NOT EXISTS portal_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        guest_id TEXT NOT NULL,
        portal_intake_id TEXT NOT NULL REFERENCES portal_intakes(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'guest_intake.completed', 'consent.signed', 'availability.provided'
        )),
        subject_ref TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(portal_intake_id, event_type)
    );
    CREATE INDEX IF NOT EXISTS ix_portal_receipt_scope
        ON portal_receipts(tenant_id, show_id, created_at);
    """,
    )


def _migration_021(conn: Any) -> None:
    """Give distribution jobs a package, a schedule, and retry-safe evidence.

    Migration 9 shipped ``distributions`` as a draft/authorize/publish triple
    and ``delivery_receipts`` as the immutable evidence table underneath it,
    but nothing recorded *which* attempt produced a delivery, who scheduled a
    publication, or what payload was authorized. Publishing was therefore
    unauditable across retries: a second attempt looked exactly like the first.

    This adds the missing attribution and package custody on ``distributions``
    and ``distribution_receipts`` as the append-only, attributable event trail
    (the sibling of ``clearance_receipts`` and ``sponsorship_receipts``).
    """
    for definition in (
        "target_platform TEXT",
        "package_checksum TEXT",
        "attempt_count INTEGER NOT NULL DEFAULT 0",
        "last_attempt_at TEXT",
        "last_error_ref TEXT",
        "recorded_by TEXT",
        "recorded_by_role TEXT",
        "scheduled_by TEXT",
        "scheduled_by_role TEXT",
        "correlation_id TEXT",
    ):
        _add_column(conn, "distributions", definition)
    # Every delivery attempt says how it was evidenced. Today that is always a
    # manual receipt: no live distribution adapter has an authenticated smoke.
    _add_column(
        conn,
        "delivery_receipts",
        "delivery_mode TEXT NOT NULL DEFAULT 'manual_receipt' "
        "CHECK(delivery_mode IN ('manual_receipt', 'provider_connected'))",
    )
    conn.execute("UPDATE distributions SET correlation_id = 'distribution://' || id WHERE correlation_id IS NULL")
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS distribution_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        show_id TEXT NOT NULL,
        distribution_id TEXT NOT NULL REFERENCES distributions(id) ON DELETE CASCADE,
        episode_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'distribution.drafted', 'distribution.scheduled',
            'distribution.authorized', 'distribution.published',
            'distribution.failed', 'distribution.clip_queued'
        )),
        from_status TEXT,
        to_status TEXT NOT NULL CHECK(to_status IN (
            'draft', 'blocked', 'authorized', 'scheduled', 'published', 'failed'
        )),
        attempt INTEGER NOT NULL CHECK(attempt >= 0),
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        evidence_ref TEXT,
        correlation_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_distribution_receipt_correlation
        ON distribution_receipts(tenant_id, show_id, correlation_id);
    CREATE INDEX IF NOT EXISTS ix_distribution_receipt_timeline
        ON distribution_receipts(tenant_id, show_id, distribution_id, created_at);
    CREATE INDEX IF NOT EXISTS ix_distribution_receipt_episode
        ON distribution_receipts(tenant_id, show_id, episode_id, created_at);
    CREATE INDEX IF NOT EXISTS ix_distribution_episode_platform
        ON distributions(tenant_id, show_id, episode_id, platform);
    CREATE INDEX IF NOT EXISTS ix_distribution_schedule
        ON distributions(tenant_id, show_id, status, scheduled_at);
    """,
    )


def _migration_022(conn: Any) -> None:
    """Receipt every network health report handed out of the cockpit."""
    _execute_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS network_report_receipts (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        report_format TEXT NOT NULL
            CHECK(report_format IN ('html', 'json', 'pdf')),
        document_checksum TEXT NOT NULL
            CHECK(length(document_checksum) = 64),
        show_count INTEGER NOT NULL,
        overlap_count INTEGER NOT NULL,
        revenue_minor INTEGER NOT NULL,
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        details TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_network_report_receipt_scope
        ON network_report_receipts(tenant_id, created_at);
    """,
    )


MIGRATIONS = (
    Migration(1, "initial_service_store", _migration_001),
    Migration(2, "candidate_import_metadata", _migration_002),
    Migration(3, "operational_receipts", _migration_003),
    Migration(4, "partnership_command_center", _migration_004),
    Migration(5, "partnership_cockpit", _migration_005),
    Migration(6, "pilot_execution_kernel", _migration_006),
    Migration(7, "runtime_metadata", _migration_007),
    Migration(8, "configurable_product_layer", _migration_008),
    Migration(9, "scoped_hosted_runtime", _migration_009),
    Migration(10, "tenant_encryption_and_artifact_custody", _migration_010),
    Migration(11, "authenticated_leased_jobs", _migration_011),
    Migration(12, "encrypted_contact_rosters", _migration_012),
    Migration(13, "encrypted_informal_touchpoints", _migration_013),
    Migration(14, "multi_show_partnership_and_pilot_scope", _migration_014),
    Migration(15, "rights_clearance_custody", _migration_015),
    Migration(16, "sponsor_inventory_and_claims", _migration_016),
    Migration(17, "guest_crm_cross_season_memory", _migration_017),
    Migration(18, "team_notifications_and_assignments", _migration_018),
    Migration(19, "bounded_research_provider_jobs", _migration_019),
    Migration(20, "guest_portal_intake_and_receipts", _migration_020),
    Migration(21, "distribution_publishing_pipeline", _migration_021),
    Migration(22, "network_report_receipts", _migration_022),
)
LATEST_VERSION = MIGRATIONS[-1].version


def _row_value(row: Any, index: int, key: str) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def applied_migrations(conn: Any) -> list[dict[str, object]]:
    """Return the ordered migration ledger without changing transaction state."""
    if not table_exists(conn, "schema_migrations"):
        return []
    rows = conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version").fetchall()
    return [
        {
            "version": int(_row_value(row, 0, "version")),
            "name": str(_row_value(row, 1, "name")),
            "applied_at": str(_row_value(row, 2, "applied_at")),
        }
        for row in rows
    ]


def current_version(conn: Any) -> int:
    ledger = applied_migrations(conn)
    return int(ledger[-1]["version"]) if ledger else 0


def migrate(conn: Any) -> list[dict[str, object]]:
    """Apply every unapplied migration in order and return the final ledger.

    Each version is applied and recorded in one transaction. Re-running this
    function at the latest version is a no-op.
    """
    if conn.in_transaction:
        raise MigrationError("migrations require a connection with no active transaction")
    conn.execute(_LEDGER_SCHEMA)
    conn.commit()

    for migration in MIGRATIONS:
        try:
            conn.execute("BEGIN" if getattr(conn, "backend", "sqlite") == "postgresql" else "BEGIN IMMEDIATE")
            if getattr(conn, "backend", "sqlite") == "postgresql":
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('hospes:migrations'))")
            already_applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (migration.version,)
            ).fetchone()
            if already_applied:
                conn.commit()
                continue
            migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    generation.now().astimezone(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise MigrationError(f"migration {migration.version} ({migration.name}) failed: {exc}") from exc

    return applied_migrations(conn)
