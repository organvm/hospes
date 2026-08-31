"""Partnership projection overdue-detection tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from hospes import partnership_projections, partnerships, store


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "partnerships" / "example-partnership-private-pilot.yaml"


def test_overdue_items_detected_in_projection(tmp_path, monkeypatch) -> None:
    class SimulatedDate(date):
        @classmethod
        def today(cls) -> "SimulatedDate":
            return cls(2026, 8, 9)

    monkeypatch.setattr(partnership_projections, "date", SimulatedDate)
    conn = store.connect(tmp_path / "overdue.sqlite3")
    result = partnerships.import_template(
        conn, TEMPLATE,
        tenant_id="overdue_test",
        actor_id="producer_fixture",
        actor_role="producer",
    )
    center = partnerships.command_center(conn, result.partnership_id, "overdue_test")
    overdue = center.get("agenda", {}).get("overdue_obligations", [])
    assert isinstance(overdue, list)
    assert center.get("summary", {}).get("overdue") == 1
    past_due_keys = [item["item_key"] for item in overdue]
    assert "plan.pilot_1" in past_due_keys, (
        f"Expected plan.pilot_1 to be overdue, got: {past_due_keys}"
    )
