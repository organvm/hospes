"""Network-operator portfolio: cross-show metrics, drilldown, and health report.

This is the only HOSPES surface that reads *across* the shows of one tenant, so
it carries three boundaries the per-show organs do not need:

* **One role.** Every entry point takes the authenticated operator's role and
  admits ``network_operator`` alone. A host, producer, editorial owner, or
  relationship owner is refused here even for a show they otherwise operate:
  "how is the portfolio doing" is a different question from "how is my show
  doing", and only the network operator is scoped to ask it.
* **One tenant.** Scope comes from the authenticated identity and is never
  widened by a payload or a path segment. Every statement in this module
  carries ``tenant_id = ?``, and the drilldown resolves its show through the
  tenant's own active registry, so a second tenant's shows, guests, and revenue
  are unreachable through any argument a caller controls.
* **Every export leaves a receipt.** The health report is the one artifact that
  leaves the cockpit, so :func:`export_health_report` appends an attributable
  ``network_report_receipts`` row — actor, role, format, and the checksum of
  the exact document handed over — before it returns anything.

Nothing here writes show state. The portfolio is a projection over rows the
per-show views already own: :mod:`hospes.platform` for the show registry and
per-show counts, :mod:`hospes.sponsors` for revenue, and the canonical
lifecycle ordinals in ``spec/states.json`` for pipeline health. Adding a state
to that canon changes this projection without editing it.
"""

from __future__ import annotations

import hashlib
import html
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from . import generation, platform, sponsors, states, store


class NetworkDashboardError(platform.PlatformError):
    """A network-portfolio authorization, scope, or export error.

    It subclasses :class:`hospes.platform.PlatformError` so the HTTP adapter
    maps it to a status code through the boundary it already has.
    """


#: The portfolio is a network-operator surface. Holding a per-show role
#: somewhere in the tenant does not widen a read to the portfolio.
READ_ROLES = frozenset({"network_operator"})

#: Report renderings. ``html`` and ``json`` are stdlib-only so the surface never
#: depends on a rendering engine to answer; ``pdf`` needs the declared extra.
REPORT_FORMATS = ("html", "json", "pdf")

#: Lifecycle milestones the portfolio counts, cheapest question first. Their
#: ordinals are read from ``spec/states.json`` at call time rather than restated
#: here, so a canon change cannot leave this projection counting a stale ladder.
PIPELINE_MILESTONES = ("APPROVED", "BOOKED", "PUBLISHED")

#: Trailing window for booking velocity, in days. Four weeks is the shortest
#: window that survives one skipped recording week without reading as zero.
VELOCITY_WINDOW_DAYS = 28

#: Receipt event recorded for every rendered health report.
EXPORT_EVENT = "network.health_report_exported"


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _timestamp(now: datetime | None = None) -> str:
    return (now or generation.now()).astimezone(timezone.utc).isoformat()


def _require_role(actor_role: Any, action: str) -> str:
    if not isinstance(actor_role, str) or actor_role not in READ_ROLES:
        raise NetworkDashboardError(f"only a network operator may {action}", 403)
    return actor_role


def _tenant(tenant_id: Any) -> str:
    return platform._ref(tenant_id, "tenant_id")


def _checksum(value: str | bytes) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _instant(value: Any) -> datetime | None:
    """Parse a stored ISO timestamp, tolerating a ``Z`` suffix and naive text."""
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _milestone_ordinals() -> dict[str, int]:
    """Return the canonical ordinal of every main lifecycle state."""
    return {str(entry["state"]): int(entry["ordinal"]) for entry in states.load_states()["main_states"]}


# ---------------------------------------------------------------------------
# Per-show projections.
# ---------------------------------------------------------------------------


def _pipeline_health(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    ordinals: Mapping[str, int],
    now: datetime | None = None,
) -> tuple[dict[str, int], dict[str, Any]]:
    """Return (pipeline counts, booking velocity) for one show.

    A candidate counts toward a milestone once it has *reached or passed* that
    ordinal, which is how an operator reads the funnel: an episode sitting in
    ``RECORDED`` was booked, even though it is no longer in ``BOOKED``. A
    branch state (``DECLINED``, ``PAUSED``, ``DO_NOT_CONTACT``, …) has no
    ordinal in the canon and is counted separately as off-ladder rather than
    being silently folded into the funnel.
    """
    rows = store.fetch_all(
        conn,
        "SELECT status, updated_at FROM appearance_opportunities WHERE tenant_id = ? AND show_id = ?",
        (tenant_id, show_id),
    )
    pipeline = {"candidates": len(rows), "off_ladder": 0}
    for milestone in PIPELINE_MILESTONES:
        pipeline[milestone.lower()] = 0
    cutoff = (now or generation.now()).astimezone(timezone.utc) - timedelta(days=VELOCITY_WINDOW_DAYS)
    booked_ordinal = ordinals["BOOKED"]
    booked_in_window = 0
    for row in rows:
        ordinal = ordinals.get(str(row["status"]))
        if ordinal is None:
            pipeline["off_ladder"] += 1
            continue
        for milestone in PIPELINE_MILESTONES:
            if ordinal >= ordinals[milestone]:
                pipeline[milestone.lower()] += 1
        moved_at = _instant(row["updated_at"])
        if ordinal >= booked_ordinal and moved_at is not None and moved_at >= cutoff:
            booked_in_window += 1
    weeks = VELOCITY_WINDOW_DAYS / 7
    velocity = {
        "window_days": VELOCITY_WINDOW_DAYS,
        "booked_in_window": booked_in_window,
        "bookings_per_week": round(booked_in_window / weeks, 2),
    }
    return pipeline, velocity


def _revenue(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, actor_role: str) -> dict[str, Any]:
    """Return the sponsor money line for one show, from the sponsor organ.

    The portfolio never recomputes revenue: it asks ``hospes.sponsors`` the same
    question the Revenue view asks, so the committed-slot and claim-approval
    gates that block publication are the ones already reported per show.
    """
    report = sponsors.revenue_report(conn, tenant_id=tenant_id, show_id=show_id, actor_role=actor_role)
    totals = report["totals"]
    return {
        "currency": report["currency"],
        "revenue_minor": int(totals["revenue_minor"]),
        "episodes": int(totals["episodes"]),
        "slots_total": int(totals["slots_total"]),
        "slots_sold": int(totals["slots_sold"]),
        "slots_available": int(totals["slots_available"]),
        "committed_unfilled": int(totals["committed_unfilled"]),
        "blocked_episodes": int(totals["blocked_episodes"]),
    }


def _show_projection(
    conn: store.DatabaseConnection,
    *,
    show: Mapping[str, Any],
    tenant_id: str,
    actor_role: str,
    ordinals: Mapping[str, int],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the bounded per-show row the portfolio and drilldown both use."""
    show_id = str(show["show_id"])
    pipeline, velocity = _pipeline_health(conn, tenant_id=tenant_id, show_id=show_id, ordinals=ordinals, now=now)
    return {
        "show_id": show_id,
        "label": str(show["label"]),
        "status": str(show["status"]),
        "config_ref": str(show["config_ref"]),
        # The per-show counters the cockpit already publishes, unchanged, so the
        # portfolio and the show's own overview cannot disagree about a number.
        "summary": platform.tenant_summary(conn, tenant_id=tenant_id, show_id=show_id),
        "pipeline": pipeline,
        "booking_velocity": velocity,
        "revenue": _revenue(conn, tenant_id=tenant_id, show_id=show_id, actor_role=actor_role),
    }


def _guest_keys(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str) -> list[dict[str, Any]]:
    """Return the distinct guest identities this show has a candidate for.

    ``guest_id`` is the cross-season identity added by the guest-CRM migration;
    an older row predating it falls back to its import ``source_key`` and then
    to the recorded name, which is the same COALESCE ladder the suggestion and
    directive readers use.
    """
    return store.fetch_all(
        conn,
        "SELECT DISTINCT COALESCE(guest_id, source_key, guest_name) AS guest_key, guest_name "
        "FROM appearance_opportunities WHERE tenant_id = ? AND show_id = ? ORDER BY guest_key",
        (tenant_id, show_id),
    )


# ---------------------------------------------------------------------------
# Portfolio and drilldown.
# ---------------------------------------------------------------------------


def portfolio(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return every active show in one tenant with its health, money, and overlap.

    ``totals.currency`` is the currency every show agreed on, or ``None`` when
    the tenant's shows declare different ones. The summed ``revenue_minor`` is
    still reported in that case, but the per-show rows stay authoritative.
    """
    role = _require_role(actor_role, "read the network portfolio")
    tenant = _tenant(tenant_id)
    ordinals = _milestone_ordinals()
    labels: dict[str, str] = {}
    guest_shows: dict[str, dict[str, Any]] = {}
    show_rows: list[dict[str, Any]] = []
    for show in platform.list_shows(conn, tenant_id=tenant):
        projection = _show_projection(conn, show=show, tenant_id=tenant, actor_role=role, ordinals=ordinals, now=now)
        show_rows.append(projection)
        show_id = projection["show_id"]
        labels[show_id] = projection["label"]
        for guest in _guest_keys(conn, tenant_id=tenant, show_id=show_id):
            key = str(guest["guest_key"])
            entry = guest_shows.setdefault(key, {"guest_name": str(guest["guest_name"]), "show_ids": set()})
            entry["show_ids"].add(show_id)
    overlap = [
        {
            "guest_id": key,
            "guest_name": entry["guest_name"],
            "show_ids": sorted(entry["show_ids"]),
            "shows": [{"show_id": show_id, "label": labels[show_id]} for show_id in sorted(entry["show_ids"])],
            "show_count": len(entry["show_ids"]),
        }
        for key, entry in sorted(guest_shows.items())
        if len(entry["show_ids"]) > 1
    ]
    currencies = {row["revenue"]["currency"] for row in show_rows}
    booked_in_window = sum(row["booking_velocity"]["booked_in_window"] for row in show_rows)
    totals = {
        "shows": len(show_rows),
        "candidates": sum(row["pipeline"]["candidates"] for row in show_rows),
        "off_ladder": sum(row["pipeline"]["off_ladder"] for row in show_rows),
        "pending_clearances": sum(int(row["summary"]["pending_clearances"]) for row in show_rows),
        "currency": currencies.pop() if len(currencies) == 1 else None,
        "revenue_minor": sum(row["revenue"]["revenue_minor"] for row in show_rows),
        "slots_sold": sum(row["revenue"]["slots_sold"] for row in show_rows),
        "committed_unfilled": sum(row["revenue"]["committed_unfilled"] for row in show_rows),
        "blocked_episodes": sum(row["revenue"]["blocked_episodes"] for row in show_rows),
        "overlapping_guests": len(overlap),
        "booked_in_window": booked_in_window,
        "bookings_per_week": round(booked_in_window / (VELOCITY_WINDOW_DAYS / 7), 2),
    }
    for milestone in PIPELINE_MILESTONES:
        name = milestone.lower()
        totals[name] = sum(row["pipeline"][name] for row in show_rows)
    return {
        "tenant_id": tenant,
        "generated_at": _timestamp(now),
        "window_days": VELOCITY_WINDOW_DAYS,
        "shows": show_rows,
        "overlap": overlap,
        "totals": totals,
    }


def show_detail(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return one show's portfolio row plus the guests it shares with siblings.

    This is the drilldown target: the cockpit opens the show's own dashboard by
    switching the active show, and this answers "what am I about to open" from
    the tenant's active registry. A show that is not registered active in *this*
    tenant is a 404 here, whatever the caller passed.
    """
    role = _require_role(actor_role, "read a network show")
    tenant, show = platform._scope(tenant_id, show_id)
    registered = {str(row["show_id"]): row for row in platform.list_shows(conn, tenant_id=tenant)}
    if show not in registered:
        raise NetworkDashboardError("show is not registered active in this tenant", 404)
    ordinals = _milestone_ordinals()
    projection = _show_projection(
        conn, show=registered[show], tenant_id=tenant, actor_role=role, ordinals=ordinals, now=now
    )
    keys = {str(guest["guest_key"]) for guest in _guest_keys(conn, tenant_id=tenant, show_id=show)}
    siblings: list[dict[str, Any]] = []
    for other_id, other in sorted(registered.items()):
        if other_id == show:
            continue
        shared = [
            guest for guest in _guest_keys(conn, tenant_id=tenant, show_id=other_id) if str(guest["guest_key"]) in keys
        ]
        for guest in shared:
            siblings.append(
                {
                    "guest_id": str(guest["guest_key"]),
                    "guest_name": str(guest["guest_name"]),
                    "show_id": other_id,
                    "label": str(other["label"]),
                }
            )
    return {"tenant_id": tenant, "generated_at": _timestamp(now), "show": projection, "overlap": siblings}


# ---------------------------------------------------------------------------
# Health report: rendering, receipts, and export.
# ---------------------------------------------------------------------------


def _money(rate_minor: int, currency: str | None) -> str:
    return f"{sponsors._major_units(int(rate_minor))} {currency or ''}".strip()


def _render_html(data: Mapping[str, Any]) -> str:
    """Render the portfolio as a self-contained, fully escaped HTML report.

    Every interpolated value passes through :func:`html.escape`, including show
    labels and guest names, which are operator-supplied text. The stylesheet is
    inline because the report is handed over as one file — and because the PDF
    renderer resolves no external asset.
    """
    totals = data["totals"]
    currency = totals["currency"]
    show_rows = (
        "".join(
            "<tr>"
            f'<th scope="row">{html.escape(str(row["label"]))}</th>'
            f"<td>{row['pipeline']['candidates']}</td>"
            f"<td>{row['pipeline']['approved']}</td>"
            f"<td>{row['pipeline']['booked']}</td>"
            f"<td>{row['booking_velocity']['bookings_per_week']}</td>"
            f"<td>{html.escape(_money(row['revenue']['revenue_minor'], row['revenue']['currency']))}</td>"
            f"<td>{row['summary']['pending_clearances']}</td>"
            "</tr>"
            for row in data["shows"]
        )
        or '<tr><td colspan="7">No show is registered active in this tenant.</td></tr>'
    )
    overlap_rows = (
        "".join(
            "<tr>"
            f'<th scope="row">{html.escape(str(entry["guest_name"]))}</th>'
            f"<td>{html.escape(', '.join(str(show['label']) for show in entry['shows']))}</td>"
            f"<td>{entry['show_count']}</td>"
            "</tr>"
            for entry in data["overlap"]
        )
        or '<tr><td colspan="3">No guest appears on more than one show.</td></tr>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>HOSPES network health</title><style>"
        "body{font:13px/1.5 system-ui,sans-serif;margin:32px;color:#111}"
        "h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}"
        ".eyebrow{color:#666;font-size:11px;letter-spacing:.08em;text-transform:uppercase}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ccc;padding:6px 9px;text-align:left}"
        "thead th{background:#f2f2f2}"
        "</style></head><body>"
        '<div class="eyebrow">HOSPES · network health report</div>'
        "<h1>Portfolio</h1>"
        f"<p>Tenant {html.escape(str(data['tenant_id']))} · generated "
        f"{html.escape(str(data['generated_at']))} · booking velocity over the last "
        f"{int(data['window_days'])} days.</p>"
        "<h2>Portfolio totals</h2><table><tbody>"
        f'<tr><th scope="row">Shows</th><td>{totals["shows"]}</td></tr>'
        f'<tr><th scope="row">Candidates</th><td>{totals["candidates"]}</td></tr>'
        f'<tr><th scope="row">Approved</th><td>{totals["approved"]}</td></tr>'
        f'<tr><th scope="row">Booked</th><td>{totals["booked"]}</td></tr>'
        f'<tr><th scope="row">Bookings per week</th><td>{totals["bookings_per_week"]}</td></tr>'
        f'<tr><th scope="row">Sponsor revenue</th>'
        f"<td>{html.escape(_money(totals['revenue_minor'], currency))}</td></tr>"
        f'<tr><th scope="row">Pending clearances</th><td>{totals["pending_clearances"]}</td></tr>'
        f'<tr><th scope="row">Guests on more than one show</th><td>{totals["overlapping_guests"]}</td></tr>'
        "</tbody></table>"
        '<h2>Shows</h2><table><thead><tr><th scope="col">Show</th><th scope="col">Candidates</th>'
        '<th scope="col">Approved</th><th scope="col">Booked</th><th scope="col">Bookings/week</th>'
        '<th scope="col">Revenue</th><th scope="col">Pending clearances</th></tr></thead>'
        f"<tbody>{show_rows}</tbody></table>"
        '<h2>Guest overlap</h2><table><thead><tr><th scope="col">Guest</th>'
        '<th scope="col">Shows</th><th scope="col">Count</th></tr></thead>'
        f"<tbody>{overlap_rows}</tbody></table>"
        "</body></html>"
    )


def _render_pdf(document: str) -> bytes:
    """Render the report document as PDF through the declared optional extra."""
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise NetworkDashboardError(
            "PDF export requires the declared optional extra: pip install -e '.[pdf]'", 503
        ) from exc
    rendered = HTML(string=document).write_pdf()
    if not rendered:
        raise NetworkDashboardError("the PDF renderer produced no output", 503)
    return bytes(rendered)


def _record_export(
    conn: store.DatabaseConnection,
    *,
    data: Mapping[str, Any],
    report_format: str,
    body: str | bytes,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append the attributable receipt for one rendered health report."""
    totals = data["totals"]
    row = {
        "id": generation.new_id("network_report_receipt"),
        "tenant_id": str(data["tenant_id"]),
        "event_type": EXPORT_EVENT,
        "report_format": report_format,
        "document_checksum": _checksum(body),
        "show_count": int(totals["shows"]),
        "overlap_count": int(totals["overlapping_guests"]),
        "revenue_minor": int(totals["revenue_minor"]),
        "actor_id": actor_id,
        "actor_role": actor_role,
        "details": {
            "candidates": int(totals["candidates"]),
            "booked": int(totals["booked"]),
            "currency": totals["currency"],
            "window_days": int(data["window_days"]),
        },
        "created_at": _timestamp(now),
    }
    store.insert(conn, "network_report_receipts", row)
    conn.commit()
    return row


def export_health_report(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    actor_role: str,
    actor_id: str,
    format: str = "html",
    now: datetime | None = None,
) -> str | bytes:
    """Render the network health report and receipt the hand-over.

    The receipt is written before the body is returned and carries the checksum
    of the exact bytes handed over, so "which report did the network operator
    take out of the cockpit" has an answer that does not depend on the file
    surviving anywhere.
    """
    role = _require_role(actor_role, "export the network health report")
    actor = platform._ref(actor_id, "actor_id")
    if format not in REPORT_FORMATS:
        raise NetworkDashboardError(f"format must be one of {', '.join(REPORT_FORMATS)}")
    data = portfolio(conn, tenant_id=tenant_id, actor_role=role, now=now)
    if format == "json":
        body: str | bytes = json.dumps(data, sort_keys=True, indent=2)
    else:
        document = _render_html(data)
        body = document if format == "html" else _render_pdf(document)
    _record_export(conn, data=data, report_format=format, body=body, actor_id=actor, actor_role=role, now=now)
    return body


def list_report_receipts(
    conn: store.DatabaseConnection, *, tenant_id: str, actor_role: str, limit: int = 100
) -> list[dict[str, Any]]:
    """Return the newest-first health-report receipt trail for one tenant."""
    _require_role(actor_role, "read the network report receipts")
    tenant = _tenant(tenant_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise NetworkDashboardError("limit must be an integer between 1 and 500")
    return store.fetch_all(
        conn,
        "SELECT * FROM network_report_receipts WHERE tenant_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (tenant, limit),
    )


__all__ = [
    "EXPORT_EVENT",
    "NetworkDashboardError",
    "PIPELINE_MILESTONES",
    "READ_ROLES",
    "REPORT_FORMATS",
    "VELOCITY_WINDOW_DAYS",
    "export_health_report",
    "list_report_receipts",
    "portfolio",
    "show_detail",
]
