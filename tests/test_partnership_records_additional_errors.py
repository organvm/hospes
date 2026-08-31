"""Tests for hospes.partnership_records additional error branches."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hospes import partnerships, store
from hospes.migrations import migrate


class TestPartnershipRecordsAdditionalErrors:
    """Tests for additional partnership_records error branches."""

    def setup_method(self):
        """Create a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False)
        self.temp_db.close()
        self.connection = store.connect(self.temp_db.name)
        migrate(self.connection)

    def teardown_method(self):
        """Clean up temporary database."""
        self.connection.close()
        Path(self.temp_db.name).unlink(missing_ok=True)

    def _create_partnership(self):
        """Create a partnership for testing."""
        partnership_result = partnerships.import_template(
            self.connection,
            Path("config/partnerships/example-private-pilot.yaml"),
            tenant_id="private_pilot",
            actor_id="example_operator",
            actor_role="relationship_owner",
        )
        self.connection.commit()
        return partnership_result

    def test_link_resource_rejects_invalid_type(self):
        """Test link_resource rejects invalid resource type."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.link_resource(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "resource_type": "invalid_type",
                    "resource_reference": "registry://owner/repo/issues/1",
                    "label": "Test",
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_link_resource_rejects_non_opaque_reference(self):
        """Test link_resource rejects non-opaque reference."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.link_resource(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "resource_type": "opportunity",
                    "resource_reference": "not-opaque",
                    "label": "Test",
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_link_resource_rejects_private_label(self):
        """Test link_resource rejects private label."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.link_resource(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "resource_type": "opportunity",
                    "resource_reference": "registry://owner/repo/issues/1",
                    "label": "email@example.com",  # Private content — email-like
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_link_resource_rejects_missing_linked_item(self):
        """Test link_resource rejects missing linked item."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.link_resource(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "resource_type": "opportunity",
                    "resource_reference": "registry://owner/repo/issues/1",
                    "label": "Test",
                    "item_id": "nonexistent-item",
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 404

    def test_link_resource_idempotent_on_duplicate(self):
        """Test link_resource is idempotent on duplicate."""
        partnership_result = self._create_partnership()

        # Create an item to link to
        item = partnerships.create_item(
            self.connection,
            partnership_result.partnership_id,
            partnerships.PartnershipItemInput.from_dict({
                "item_key": "test.link",
                "category": "engine",
                "title": "Test Link",
                "summary": "Test summary",
                "owner": "Partners",
                "state": "current",
            }),
            tenant_id="private_pilot",
            actor_id="example_operator",
            actor_role="relationship_owner",
        )
        self.connection.commit()

        # Link resource first time
        result1 = partnerships.link_resource(
            self.connection,
            partnership_result.partnership_id,
            payload={
                "resource_type": "opportunity",
                "resource_reference": "registry://owner/repo/issues/1",
                "label": "Test",
                "item_id": item["id"],
            },
            tenant_id="private_pilot",
            actor_id="example_operator",
            actor_role="relationship_owner",
        )
        self.connection.commit()

        # Link same resource again (should be idempotent)
        result2 = partnerships.link_resource(
            self.connection,
            partnership_result.partnership_id,
            payload={
                "resource_type": "opportunity",
                "resource_reference": "registry://owner/repo/issues/1",
                "label": "Test",
                "item_id": item["id"],
            },
            tenant_id="private_pilot",
            actor_id="example_operator",
            actor_role="relationship_owner",
        )
        self.connection.commit()

        # Should return the same resource
        assert result1["id"] == result2["id"]

    def test_record_review_rejects_invalid_kind(self):
        """Test record_review rejects invalid review kind."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.record_review(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "review_kind": "invalid_kind",
                    "occurred_at": "2026-01-01T00:00:00+00:00",
                    "decisions_count": 1,
                    "coverage_met": 1,
                    "coverage_total": 10,
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_record_review_rejects_counts_out_of_range(self):
        """Test record_review rejects counts out of range."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.record_review(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "review_kind": "ari_review",
                    "occurred_at": "2026-01-01T00:00:00+00:00",
                    "decisions_count": 101,  # > 100
                    "coverage_met": 1,
                    "coverage_total": 10,
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_record_review_rejects_coverage_met_exceeds_total(self):
        """Test record_review rejects coverage_met > coverage_total."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.record_review(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "review_kind": "ari_review",
                    "occurred_at": "2026-01-01T00:00:00+00:00",
                    "decisions_count": 1,
                    "coverage_met": 11,
                    "coverage_total": 10,
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_record_review_rejects_non_opaque_external_reference(self):
        """Test record_review rejects non-opaque external reference."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.record_review(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "review_kind": "ari_review",
                    "occurred_at": "2026-01-01T00:00:00+00:00",
                    "decisions_count": 1,
                    "coverage_met": 1,
                    "coverage_total": 10,
                    "external_reference": "not-opaque",
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_record_review_requires_reference_for_rehearsal_scorecard(self):
        """Test record_review requires reference for technical_rehearsal and pilot_scorecard."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.record_review(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "review_kind": "technical_rehearsal",
                    "occurred_at": "2026-01-01T00:00:00+00:00",
                    "decisions_count": 1,
                    "coverage_met": 1,
                    "coverage_total": 10,
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_record_review_rejects_bad_occurred_at_format(self):
        """Test record_review rejects bad occurred_at format."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.record_review(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "review_kind": "ari_review",
                    "occurred_at": "not-a-date",
                    "decisions_count": 1,
                    "coverage_met": 1,
                    "coverage_total": 10,
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_record_review_rejects_occurred_at_missing_tz(self):
        """Test record_review rejects occurred_at missing timezone."""
        partnership_result = self._create_partnership()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.record_review(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "review_kind": "ari_review",
                    "occurred_at": "2026-01-01T00:00:00",  # No timezone
                    "decisions_count": 1,
                    "coverage_met": 1,
                    "coverage_total": 10,
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422

    def test_record_review_rejects_occurred_at_future(self):
        """Test record_review rejects occurred_at in future."""
        partnership_result = self._create_partnership()

        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()

        with pytest.raises(partnerships.PartnershipError) as exc_info:
            partnerships.record_review(
                self.connection,
                partnership_result.partnership_id,
                payload={
                    "review_kind": "ari_review",
                    "occurred_at": future,
                    "decisions_count": 1,
                    "coverage_met": 1,
                    "coverage_total": 10,
                },
                tenant_id="private_pilot",
                actor_id="example_operator",
                actor_role="relationship_owner",
            )
        assert exc_info.value.status_code == 422


if __name__ == "__main__":
    pytest.main([__file__, "-v"])