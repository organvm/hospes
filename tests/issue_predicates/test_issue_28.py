"""Executable completion predicate for HOSPES issue #28.

Close condition: RSS, video, and clip packages plus previews, adapters, manual
receipts, authorization, retry-safe transitions, and immutable delivery evidence
pass.
"""

from __future__ import annotations

import dataclasses
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import synthetic_bearer_authenticator
from hospes import configuration, distribution, migrations, platform, providers, sponsors, store
from hospes.api import create_app

try:  # optional extra
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - exercised only without the optional extra
    TestClient = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
NOW = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
TENANT = "hospes"
#: ``field`` is the one shipped show whose outbound mode is ``manual_receipt``,
#: so it is the only one where the authorization gate can actually open.
SHOW = "field"
DRAFT_ONLY_SHOW = "flagship"
OTHER_SHOW = "client-x"
EPISODE = "episode-12"
PRODUCER_TOKEN = "issue28-producer-token-0123456789abcdef"  # allow-secret: fixture
OWNER_TOKEN = "issue28-owner-token-0123456789abcdefgh"  # allow-secret: fixture
HOST_TOKEN = "issue28-host-token-0123456789abcdefghij"  # allow-secret: fixture

pytestmark = pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


class _SessionShowASGI:
    """Project the operator's active show into request scope, as the shell does."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"x-session-show":
                    scope["hospes.operator_show"] = value.decode("utf-8")
        await self.app(scope, receive, send)


class _ShowBoundAuthenticator:
    """Bind a synthetic bearer identity to one show, mirroring identity_mappings."""

    def __init__(self, inner, show_id: str) -> None:
        self.inner = inner
        self.show_id = show_id

    def authenticate(self, presented_bearer):
        identity = self.inner.authenticate(presented_bearer)
        return dataclasses.replace(identity, show_id=self.show_id)


def _authenticator():
    return synthetic_bearer_authenticator(
        {
            PRODUCER_TOKEN: ("producer_fixture", "producer", TENANT),
            OWNER_TOKEN: ("owner_fixture", "editorial_owner", TENANT),
            HOST_TOKEN: ("host_fixture", "host", TENANT),
        }
    )


def _headers(
    token: str = PRODUCER_TOKEN,  # allow-secret: fixture
    session_show: str | None = SHOW,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}  # allow-secret: fixture
    if session_show is not None:
        headers["X-Session-Show"] = session_show
    return headers


def _seed(path: Path) -> None:
    conn = store.connect(path)
    for show_id, label in ((SHOW, "Field Show"), (DRAFT_ONLY_SHOW, "Flagship Show"), (OTHER_SHOW, "Client X")):
        platform.register_show(
            conn,
            tenant_id=TENANT,
            show_id=show_id,
            label=label,
            config_ref=f"config/shows/{show_id}.yaml",
        )
    conn.commit()
    conn.close()


def _client(path: Path, *, bound_show: str | None = None):
    authenticator = _authenticator()
    if bound_show is not None:
        authenticator = _ShowBoundAuthenticator(authenticator, bound_show)
    app = create_app(
        str(path),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=authenticator,
        csrf_required=False,
    )
    return TestClient(_SessionShowASGI(app))


RSS_PACKAGE = {
    "title": "Episode 12 — the artifact",
    "description": "What the guest actually built, and what broke.",
    "audio_url": "https://cdn.example.test/episode-12.mp3",
    "duration": "1:04:12",
    "chapters": [
        {"start_seconds": 0, "title": "Cold open"},
        {"start_seconds": 420, "title": "The claim"},
    ],
    "guid": "feed://episode-12",
}
VIDEO_PACKAGE = {
    "title": "Episode 12 — the artifact",
    "description": "Full conversation.",
    "tags": ["podcast", "interview"],
    "chapters": [{"start_seconds": 0, "title": "Cold open"}],
    "playlist": "Season 2",
    "thumbnail_ref": "asset://episode-12/thumbnail",
}
CLIP_PACKAGE = {
    "start_seconds": 30,
    "end_seconds": 90,
    "target_platform": "tiktok",
    "caption_ref": "caption://episode-12/clip-1",
    "hashtags": ["podcast", "#clips"],
}


def _draft(
    conn, *, platform_name: str = "rss", metadata=None, show_id: str = SHOW, episode_id: str = EPISODE, key=None
):
    return distribution.create_draft(
        conn,
        tenant_id=TENANT,
        show_id=show_id,
        episode_id=episode_id,
        platform_name=platform_name,
        metadata=RSS_PACKAGE if metadata is None else metadata,
        idempotency_key=key or f"{episode_id}:{platform_name}",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )


def _authorize(conn, job, *, key: str | None = None, show_id: str = SHOW):
    return distribution.authorize_publish(
        conn,
        tenant_id=TENANT,
        show_id=show_id,
        distribution_id=job["id"],
        outbound_mode="manual_receipt",
        authorized_by="owner_fixture",
        authorization_ref="receipt://owner/publish-12",
        idempotency_key=key or f"{job['id']}:publish",
        actor_role="editorial_owner",
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Packages: three validated families.
# ---------------------------------------------------------------------------


def test_the_three_package_families_validate_and_refuse_out_of_family_fields() -> None:
    rss = distribution.DistributionPackage.from_mapping("rss", RSS_PACKAGE)
    assert rss.family == "rss"
    assert rss.values["duration"] == "01:04:12"
    assert len(rss.checksum) == 64

    video = distribution.DistributionPackage.from_mapping("youtube", VIDEO_PACKAGE)
    assert video.family == "video"
    assert video.values["tags"] == ["podcast", "interview"]

    clip = distribution.DistributionPackage.from_mapping("clip_queue", CLIP_PACKAGE)
    assert clip.family == "clip"
    # Hashtags normalize to a single leading hash whether or not one was typed.
    assert clip.values["hashtags"] == ["#podcast", "#clips"]

    # Every platform maps to exactly one family, and the short-form platforms
    # carry the clip family rather than inventing a fourth shape.
    assert set(distribution.PACKAGE_FAMILIES) == set(distribution.PLATFORMS)
    assert {distribution.PACKAGE_FAMILIES[name] for name in distribution.SHORT_FORM_PLATFORMS} == {"clip"}

    for platform_name, metadata, fragment in (
        ("nowhere", RSS_PACKAGE, "platform must be one of"),
        ("rss", {"title": "A", "tags": ["x"]}, "rss package has unsupported fields"),
        ("youtube", {"title": "A", "audio_url": "https://example.test/a.mp3"}, "video package has unsupported fields"),
        ("rss", {}, "rss package requires"),
        ("clip_queue", {"start_seconds": 0}, "clip package requires"),
        ("rss", "not-an-object", "package metadata must be an object"),
    ):
        with pytest.raises(distribution.DistributionError, match=fragment):
            distribution.DistributionPackage.from_mapping(platform_name, metadata)


@pytest.mark.parametrize(
    ("platform_name", "metadata", "fragment"),
    [
        ("rss", {"title": ""}, "title is required"),
        ("rss", {"title": 12}, "title must be text"),
        ("rss", {"title": "x" * 161}, "title exceeds 160 characters"),
        ("rss", {"title": "A\x07B"}, "unsupported control characters"),
        ("rss", {"title": "A", "duration": "nope"}, "duration must be HH:MM:SS"),
        ("rss", {"title": "A", "duration": True}, "duration must be HH:MM:SS"),
        ("rss", {"title": "A", "duration": ["01:00"]}, "duration must be HH:MM:SS"),
        ("rss", {"title": "A", "duration": 99999999}, "between 0 and 86400"),
        ("rss", {"title": "A", "duration": None, "description": None, "audio_url": None}, None),
        ("rss", {"title": "A", "chapters": "no"}, "chapters must be a list"),
        ("rss", {"title": "A", "chapters": ["no"]}, "each chapter must be an object"),
        ("rss", {"title": "A", "chapters": [{"start_seconds": 0, "start": 0, "title": "x"}]}, "exactly once"),
        ("rss", {"title": "A", "chapters": [{"start_seconds": 0, "title": "x", "nope": 1}]}, "unsupported fields"),
        ("rss", {"title": "A", "chapters": [{"start_seconds": True, "title": "x"}]}, "whole number of seconds"),
        ("rss", {"title": "A", "chapters": [{"start_seconds": 99999999, "title": "x"}]}, "between 0 and 86400"),
        (
            "rss",
            {"title": "A", "chapters": [{"start_seconds": 60, "title": "b"}, {"start_seconds": 0, "title": "a"}]},
            "distinct and ascending",
        ),
        (
            "rss",
            {"title": "A", "chapters": [{"start_seconds": index, "title": "x"} for index in range(101)]},
            "cannot exceed 100",
        ),
        ("youtube", {"title": "A", "tags": "podcast"}, "tags must be a list"),
        ("youtube", {"title": "A", "tags": ["ok", "ok"]}, "declared twice"),
        ("youtube", {"title": "A", "tags": ["not a legal tag!"]}, "each tag must be"),
        ("youtube", {"title": "A", "tags": [str(index) for index in range(31)]}, "cannot exceed 30"),
        (
            "youtube",
            {"title": "A", "tags": [f"{'x' * 27}{index:03d}" for index in range(20)]},
            "exceed the 500-character platform budget",
        ),
        ("youtube", {"title": "A", "tags": ["x" * 30]}, None),
        ("youtube", {"title": "A", "playlist": "not/a/legal\\playlist"}, "playlist must be"),
        ("clip_queue", {**CLIP_PACKAGE, "end_seconds": 30}, "greater than start_seconds"),
        ("clip_queue", {**CLIP_PACKAGE, "end_seconds": 700}, "cannot exceed 600 seconds"),
        ("clip_queue", {**CLIP_PACKAGE, "target_platform": "mastodon"}, "target_platform must be one of"),
        ("shorts", {**CLIP_PACKAGE, "target_platform": "tiktok"}, "must match the short-form platform"),
        ("clip_queue", {**CLIP_PACKAGE, "hashtags": "podcast"}, "hashtags must be a list"),
        ("clip_queue", {**CLIP_PACKAGE, "hashtags": [7]}, "each hashtag must be text"),
        ("clip_queue", {**CLIP_PACKAGE, "hashtags": ["not a tag"]}, "1-60 alphanumeric"),
        ("clip_queue", {**CLIP_PACKAGE, "hashtags": ["dup", "#dup"]}, "declared twice"),
        ("clip_queue", {**CLIP_PACKAGE, "hashtags": [str(index) for index in range(31)]}, "cannot exceed 30"),
    ],
)
def test_package_intake_rejects_malformed_fields(platform_name, metadata, fragment) -> None:
    if fragment is None:
        assert distribution.DistributionPackage.from_mapping(platform_name, metadata).values["title"]
        return
    with pytest.raises(distribution.DistributionError, match=fragment):
        distribution.DistributionPackage.from_mapping(platform_name, metadata)


def test_references_accept_https_or_custody_and_refuse_contact_data() -> None:
    custody = distribution.DistributionPackage.from_mapping(
        "rss", {"title": "A", "audio_url": "vault://episode-12/master"}
    )
    assert custody.values["audio_url"] == "vault://episode-12/master"
    public = distribution.DistributionPackage.from_mapping(
        "rss", {"title": "A", "audio_url": "https://cdn.example.test/a.mp3"}
    )
    assert public.values["audio_url"].startswith("https://")

    for value, fragment in (
        (12, "public https URL or an opaque custody reference"),
        ("https://cdn.example.test/host@example.test.mp3", "must not carry contact data"),
        ("http://cdn.example.test/a.mp3", "must use https, not plain http"),
    ):
        with pytest.raises(platform.PlatformError, match=fragment):
            distribution.DistributionPackage.from_mapping("rss", {"title": "A", "audio_url": value})
    with pytest.raises(platform.PlatformError, match="caption_ref"):
        distribution.DistributionPackage.from_mapping(
            "clip_queue", {**CLIP_PACKAGE, "caption_ref": "producer@example.test"}
        )
    with pytest.raises(platform.PlatformError, match="guid"):
        distribution.DistributionPackage.from_mapping("rss", {"title": "A", "guid": "a b c"})


def test_package_completeness_reports_outstanding_fields_without_blocking_the_draft(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "completeness.sqlite3")
    # A half-prepared package still drafts: the audio is not cut yet, and
    # refusing the draft would push that state back into a spreadsheet.
    partial = _draft(conn, metadata={"title": "Episode 12"})
    assert partial["status"] == "draft"
    assert distribution.package_completeness("rss", partial["metadata"]) == {
        "family": "rss",
        "complete": False,
        "missing": ["description", "audio_url", "duration"],
    }
    assert distribution.package_completeness("rss", RSS_PACKAGE)["complete"] is True
    assert distribution.package_completeness("youtube", {"title": "A"})["missing"] == ["description", "tags"]
    assert distribution.package_completeness("clip_queue", CLIP_PACKAGE)["complete"] is True
    assert distribution.package_completeness("tiktok", {"caption_ref": "caption://x"})["missing"] == ["hashtags"]
    conn.close()


# ---------------------------------------------------------------------------
# Previews.
# ---------------------------------------------------------------------------


def test_previews_render_rss_video_and_clip_and_stay_pure() -> None:
    rss = distribution.preview("rss", RSS_PACKAGE)
    assert rss["format"] == "xml" and rss["family"] == "rss"
    item = ET.fromstring(rss["body"])
    assert item.findtext("title") == RSS_PACKAGE["title"]
    assert item.find("enclosure").attrib["url"] == RSS_PACKAGE["audio_url"]
    assert item.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration") == "01:04:12"
    chapters = item.find("{http://podlove.org/simple-chapters}chapters")
    assert [chapter.attrib["start"] for chapter in chapters] == ["00:00", "07:00"]
    assert rss["completeness"]["complete"] is True
    assert len(rss["package_checksum"]) == 64

    # A minimal item is still parseable, and carries no empty guid or chapters.
    minimal = ET.fromstring(distribution.rss_preview({"title": "Fixture", "duration": "01:00"}))
    assert minimal.find("guid") is None
    assert minimal.find("{http://podlove.org/simple-chapters}chapters") is None
    # Escaping is not optional: a title is operator-supplied copy.
    assert "&lt;script&gt;" in distribution.rss_preview({"title": "<script>"})

    video = distribution.preview("youtube", VIDEO_PACKAGE)
    assert video["format"] == "text"
    assert "Playlist: Season 2" in video["body"]
    assert "Thumbnail: asset://episode-12/thumbnail" in video["body"]
    assert "Tags: podcast, interview" in video["body"]
    assert "00:00 Cold open" in video["body"]
    assert distribution.youtube_preview({"title": "Bare"}).strip() == "Title: Bare"
    assert "01:00:00 Late" in distribution.youtube_preview(
        {"title": "A", "chapters": [{"start_seconds": 3600, "title": "Late"}]}
    )

    clip = distribution.preview("tiktok", {**CLIP_PACKAGE, "target_platform": "tiktok"})
    assert clip["family"] == "clip"
    assert "Window: 00:30 → 01:30 (60s)" in clip["body"]
    assert "Hashtags: #podcast #clips" in clip["body"]
    assert "Hashtags" not in distribution.clip_preview({k: v for k, v in CLIP_PACKAGE.items() if k != "hashtags"})


# ---------------------------------------------------------------------------
# Drafting, the clip queue, and the blockers a package inherits.
# ---------------------------------------------------------------------------


def test_drafting_is_idempotent_and_inherits_the_publication_blockers(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "draft.sqlite3")
    first = _draft(conn)
    assert first["status"] == "draft"
    assert first["recorded_by"] == "producer_fixture" and first["recorded_by_role"] == "producer"
    assert first["correlation_id"] == f"distribution://{first['id']}"
    assert first["package_checksum"] == distribution.DistributionPackage.from_mapping("rss", RSS_PACKAGE).checksum

    # The same idempotency key returns the same job, never a second one.
    again = _draft(conn, metadata={**RSS_PACKAGE, "title": "Different"})
    assert again["id"] == first["id"]
    assert again["metadata"]["title"] == RSS_PACKAGE["title"]

    # An uncleared rights item makes the next draft blocked, not draft.
    platform.add_clearance(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id="episode-13",
        clearance_type="music",
        rights_holder_ref="vault://rights/one",
        license_terms_ref="vault://terms/one",
        now=NOW,
    )
    blocked = _draft(conn, episode_id="episode-13")
    assert blocked["status"] == "blocked"

    receipts = distribution.distribution_receipts(
        conn, tenant_id=TENANT, show_id=SHOW, actor_role="host", distribution_id=first["id"]
    )
    assert [row["event_type"] for row in receipts["transitions"]] == ["distribution.drafted"]
    assert receipts["transitions"][0]["to_status"] == "draft"
    assert receipts["transitions"][0]["correlation_id"] == f"distribution://{first['id']}/1"
    assert receipts["deliveries"] == []
    conn.close()


def test_the_clip_queue_records_window_target_caption_and_hashtags(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "clips.sqlite3")
    for index, target in enumerate(distribution.SHORT_FORM_PLATFORMS):
        clip = distribution.queue_clip(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            episode_id=EPISODE,
            start_seconds=30 * index,
            end_seconds=30 * index + 45,
            platform_name=target,
            caption_ref=f"caption://{EPISODE}/{target}",
            idempotency_key=f"{EPISODE}:{target}",
            hashtags=["podcast"],
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
        assert clip["platform"] == "clip_queue"
        assert clip["target_platform"] == target
        assert clip["metadata"]["caption_ref"] == f"caption://{EPISODE}/{target}"

    # A clip is drafted as a queue entry and receipted as one.
    queued = distribution.list_distributions(
        conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer", platform_name="clip_queue"
    )
    assert len(queued) == 3
    receipts = distribution.distribution_receipts(
        conn, tenant_id=TENANT, show_id=SHOW, actor_role="host", distribution_id=queued[0]["id"]
    )
    assert receipts["transitions"][0]["event_type"] == "distribution.clip_queued"

    # A clip with no hashtags omits the field rather than storing an empty list.
    bare = distribution.queue_clip(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        start_seconds=600,
        end_seconds=660,
        platform_name="reels",
        caption_ref="caption://episode-12/bare",
        idempotency_key=f"{EPISODE}:reels:bare",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    assert "hashtags" not in bare["metadata"]
    conn.close()


# ---------------------------------------------------------------------------
# Authorization, scheduling, and the gates that re-run.
# ---------------------------------------------------------------------------


def test_authorization_precedes_scheduling_and_both_gates_re_run_at_publication(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "gates.sqlite3")
    job = _draft(conn)

    # Scheduling before authorization is refused: a scheduled outbound action
    # with no human behind it is the automation AGENTS.md forbids.
    with pytest.raises(distribution.DistributionError, match="requires a human authorization receipt") as unscheduled:
        distribution.schedule_publication(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=job["id"],
            scheduled_at=LATER.isoformat(),
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    assert unscheduled.value.status_code == 403

    # A draft-only show may not authorize a publication at all.
    with pytest.raises(distribution.DistributionError, match="outbound mode") as refused:
        distribution.authorize_publish(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=job["id"],
            outbound_mode="draft_only",
            authorized_by="owner_fixture",
            authorization_ref="receipt://owner/publish-12",
            idempotency_key=f"{job['id']}:publish",
            actor_role="editorial_owner",
            now=NOW,
        )
    assert refused.value.status_code == 403

    authorized = _authorize(conn, job)
    assert authorized["status"] == "authorized"
    assert authorized["authorization_receipt_ref"]

    scheduled = distribution.schedule_publication(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        distribution_id=job["id"],
        scheduled_at=LATER.isoformat(),
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    assert scheduled["status"] == "scheduled"
    assert scheduled["scheduled_at"] == LATER.isoformat()
    assert scheduled["scheduled_by"] == "producer_fixture"
    # Re-scheduling the same moment is a no-op, and re-authorizing a scheduled
    # job keeps the schedule rather than silently unscheduling it.
    assert (
        distribution.schedule_publication(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=job["id"],
            scheduled_at=LATER.isoformat(),
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )["updated_at"]
        == scheduled["updated_at"]
    )
    assert _authorize(conn, job)["status"] == "scheduled"

    for value, fragment in (
        ("not-a-timestamp", "ISO 8601 timestamp"),
        (LATER.replace(tzinfo=None).isoformat(), "must include a timezone"),
        ((NOW - timedelta(days=1)).isoformat(), "must be in the future"),
    ):
        with pytest.raises(distribution.DistributionError, match=fragment):
            distribution.schedule_publication(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=job["id"],
                scheduled_at=value,
                actor_id="producer_fixture",
                actor_role="producer",
                now=NOW,
            )

    # A clearance reopened after authorization still denies mark-published.
    clearance = platform.add_clearance(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        clearance_type="music",
        rights_holder_ref="vault://rights/one",
        license_terms_ref="vault://terms/one",
        now=NOW,
    )
    with pytest.raises(distribution.DistributionError, match="uncleared rights items"):
        distribution.mark_published(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=job["id"],
            external_id_ref="rss://feed/episode-12",
            actor_id="owner_fixture",
            actor_role="editorial_owner",
            now=NOW,
        )
    platform.update_clearance(
        conn, tenant_id=TENANT, show_id=SHOW, clearance_id=clearance["id"], status="cleared", now=NOW
    )

    # The sponsor gate composes with it through the same predicate.
    sponsors.declare_slots(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        slots=[{"slot_type": "pre", "rate_minor": 150_000, "committed": True}],
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    with pytest.raises(distribution.DistributionError, match="publication blocker"):
        distribution.mark_published(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=job["id"],
            external_id_ref="rss://feed/episode-12",
            actor_id="owner_fixture",
            actor_role="editorial_owner",
            now=NOW,
        )
    conn.close()


# ---------------------------------------------------------------------------
# Manual receipts, retry-safe transitions, immutable delivery evidence.
# ---------------------------------------------------------------------------


def test_every_attempt_appends_immutable_delivery_evidence_and_a_retry_is_numbered(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "delivery.sqlite3")
    job = _authorize(conn, _draft(conn))

    failed = distribution.record_failure(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        distribution_id=job["id"],
        error_ref="error://provider/timeout",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 1
    assert failed["last_error_ref"] == "error://provider/timeout"

    published = distribution.mark_published(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        distribution_id=job["id"],
        external_id_ref="rss://feed/episode-12",
        actor_id="owner_fixture",
        actor_role="editorial_owner",
        now=NOW,
    )
    assert published["status"] == "published"
    # The retry is attempt 2, not a rewrite of attempt 1.
    assert published["attempt_count"] == 2
    assert published["external_id_ref"] == "rss://feed/episode-12"

    receipts = distribution.distribution_receipts(
        conn, tenant_id=TENANT, show_id=SHOW, actor_role="host", distribution_id=job["id"]
    )
    assert [row["event_type"] for row in receipts["transitions"]] == [
        "distribution.drafted",
        "distribution.authorized",
        "distribution.failed",
        "distribution.published",
    ]
    deliveries = receipts["deliveries"]
    assert [(row["attempt"], row["result"]) for row in deliveries] == [(1, "failed"), (2, "delivered")]
    assert {row["delivery_mode"] for row in deliveries} == {"manual_receipt"}
    assert {row["authorization_action"] for row in deliveries} == {"publish"}
    assert {row["authorization_receipt_id"] for row in deliveries} == {job["authorization_receipt_ref"]}
    assert {row["payload_checksum"] for row in deliveries} == {job["package_checksum"]}
    assert deliveries[0]["external_reference"] == "error://provider/timeout"
    assert deliveries[1]["external_reference"] == "rss://feed/episode-12"

    # The delivery evidence is immutable: the same attempt cannot be re-filed.
    with pytest.raises(Exception):
        store.insert(conn, "delivery_receipts", {**deliveries[1], "id": "duplicate-attempt"})
    conn.rollback()

    # One provider receipt names the adapter that produced the delivery.
    provider_receipt = store.fetch_one(
        conn,
        "SELECT * FROM provider_receipts WHERE capability = 'distribution' AND receipt_ref = ?",
        ("rss://feed/episode-12",),
    )
    assert provider_receipt["details"]["authorized_by"] == "owner_fixture"
    assert provider_receipt["details"]["adapter"] == "rss"
    assert provider_receipt["details"]["attempt"] == 2
    conn.close()


def test_the_published_external_reference_is_immutable_and_transitions_fail_closed(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "immutable.sqlite3")
    job = _authorize(conn, _draft(conn))
    published = distribution.mark_published(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        distribution_id=job["id"],
        external_id_ref="rss://feed/episode-12",
        actor_id="owner_fixture",
        actor_role="editorial_owner",
        now=NOW,
    )

    # Re-recording the same reference is idempotent; a different one conflicts.
    assert (
        distribution.mark_published(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=job["id"],
            external_id_ref="rss://feed/episode-12",
            actor_id="owner_fixture",
            actor_role="editorial_owner",
            now=NOW,
        )["published_at"]
        == published["published_at"]
    )
    for call, fragment, code in (
        (
            lambda: distribution.mark_published(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=job["id"],
                external_id_ref="rss://feed/rewritten",
                actor_id="owner_fixture",
                actor_role="editorial_owner",
                now=NOW,
            ),
            "external reference is immutable",
            409,
        ),
        (
            lambda: distribution.record_failure(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=job["id"],
                error_ref="error://provider/late",
                actor_id="producer_fixture",
                actor_role="producer",
                now=NOW,
            ),
            "published distribution cannot be marked failed",
            409,
        ),
        (
            lambda: distribution.schedule_publication(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=job["id"],
                scheduled_at=LATER.isoformat(),
                actor_id="producer_fixture",
                actor_role="producer",
                now=NOW,
            ),
            "published distribution cannot be rescheduled",
            409,
        ),
    ):
        with pytest.raises(distribution.DistributionError, match=fragment) as excinfo:
            call()
        assert excinfo.value.status_code == code

    # Re-authorizing a published job returns it unchanged rather than reopening it.
    assert _authorize(conn, job)["status"] == "published"

    # A row whose status is outside the declared lifecycle fails closed rather
    # than being authorized on the assumption that it is safe.
    conn.execute("UPDATE distributions SET status = 'delivering' WHERE id = ?", (job["id"],))
    conn.commit()
    with pytest.raises(distribution.DistributionError, match="not eligible for authorization") as ineligible:
        _authorize(conn, job)
    assert ineligible.value.status_code == 409
    conn.execute("UPDATE distributions SET status = 'published' WHERE id = ?", (job["id"],))
    conn.commit()

    # An unauthorized job cannot be delivered at all, in either direction.
    unauthorized = _draft(conn, episode_id="episode-14", key="episode-14:rss")
    for call, fragment in (
        (
            lambda: distribution.mark_published(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=unauthorized["id"],
                external_id_ref="rss://feed/episode-14",
                actor_id="owner_fixture",
                actor_role="editorial_owner",
                now=NOW,
            ),
            "only an authorized distribution can be published",
        ),
        (
            lambda: distribution.record_failure(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=unauthorized["id"],
                error_ref="error://provider/timeout",
                actor_id="producer_fixture",
                actor_role="producer",
                now=NOW,
            ),
            "only an authorized distribution can record a delivery failure",
        ),
    ):
        with pytest.raises(distribution.DistributionError, match=fragment):
            call()

    # A tampered authorization reference fails closed instead of delivering.
    tampered = _authorize(conn, unauthorized, key="episode-14:publish")
    conn.execute(
        "UPDATE distributions SET authorization_receipt_ref = 'authorization_receipt-missing' WHERE id = ?",
        (tampered["id"],),
    )
    conn.commit()
    with pytest.raises(distribution.DistributionError, match="authorization receipt is invalid") as invalid:
        distribution.mark_published(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=tampered["id"],
            external_id_ref="rss://feed/episode-14",
            actor_id="owner_fixture",
            actor_role="editorial_owner",
            now=NOW,
        )
    assert invalid.value.status_code == 403
    conn.execute("UPDATE distributions SET authorization_receipt_ref = NULL WHERE id = ?", (tampered["id"],))
    conn.commit()
    with pytest.raises(distribution.DistributionError, match="requires a human authorization receipt"):
        distribution.record_failure(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=tampered["id"],
            error_ref="error://provider/timeout",
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    conn.close()


# ---------------------------------------------------------------------------
# Roles, scope, and bounded reads.
# ---------------------------------------------------------------------------


def test_roles_and_scope_fail_closed(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "roles.sqlite3")
    job = _authorize(conn, _draft(conn))

    assert distribution.READ_ROLES > distribution.WRITE_ROLES > distribution.PUBLISH_ROLES
    for call, fragment in (
        (lambda: _draft(conn, show_id=SHOW, episode_id="episode-99", key="k1"), None),
        (
            lambda: distribution.create_draft(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                episode_id="episode-99",
                platform_name="rss",
                metadata=RSS_PACKAGE,
                idempotency_key="episode-99:host",
                actor_id="host_fixture",
                actor_role="host",
                now=NOW,
            ),
            "cannot draft a publication",
        ),
        (
            lambda: distribution.authorize_publish(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=job["id"],
                outbound_mode="manual_receipt",
                authorized_by="producer_fixture",
                authorization_ref="receipt://owner/publish-12",
                idempotency_key="producer:publish",
                actor_role="producer",
                now=NOW,
            ),
            "cannot authorize a publication",
        ),
        (
            lambda: distribution.mark_published(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=job["id"],
                external_id_ref="rss://feed/episode-12",
                actor_id="producer_fixture",
                actor_role="producer",
                now=NOW,
            ),
            "cannot record a publication",
        ),
        (
            lambda: distribution.record_failure(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=job["id"],
                error_ref="error://provider/timeout",
                actor_id="host_fixture",
                actor_role="host",
                now=NOW,
            ),
            "cannot record a delivery failure",
        ),
        (
            lambda: distribution.schedule_publication(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                distribution_id=job["id"],
                scheduled_at=LATER.isoformat(),
                actor_id="host_fixture",
                actor_role="host",
                now=NOW,
            ),
            "cannot schedule a publication",
        ),
        (
            lambda: distribution.list_distributions(conn, tenant_id=TENANT, show_id=SHOW, actor_role="stranger"),
            "cannot read distribution jobs",
        ),
        (
            lambda: distribution.record_adapter_verification(
                conn, tenant_id=TENANT, show_id=SHOW, actor_role="host", now=NOW
            ),
            "cannot verify distribution adapters",
        ),
    ):
        if fragment is None:
            call()
            continue
        with pytest.raises(distribution.DistributionError, match=fragment) as excinfo:
            call()
        assert excinfo.value.status_code == 403

    # A job in another show is not found, never silently read across the boundary.
    with pytest.raises(distribution.DistributionError, match="not found") as missing:
        distribution.distribution_receipts(
            conn, tenant_id=TENANT, show_id=OTHER_SHOW, actor_role="host", distribution_id=job["id"]
        )
    assert missing.value.status_code == 404

    # Reads are bounded and validated.
    for kwargs, fragment in (
        ({"limit": 0}, "limit must be an integer"),
        ({"limit": True}, "limit must be an integer"),
        ({"status": "nope"}, "status must be one of"),
        ({"platform_name": "nope"}, "platform must be one of"),
    ):
        with pytest.raises(distribution.DistributionError, match=fragment):
            distribution.list_distributions(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer", **kwargs)
    scoped = distribution.list_distributions(
        conn, tenant_id=TENANT, show_id=SHOW, actor_role="host", episode_id=EPISODE, status="authorized", limit=5
    )
    assert [row["id"] for row in scoped] == [job["id"]]

    # An internal call with no operator identity is recorded as the system
    # actor rather than borrowing somebody's name.
    unattributed = distribution.create_draft(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id="episode-15",
        platform_name="rss",
        metadata={"title": "Internal"},
        idempotency_key="episode-15:rss",
        now=NOW,
    )
    assert unattributed["recorded_by"] == "system" and unattributed["recorded_by_role"] == "system"
    conn.close()


# ---------------------------------------------------------------------------
# Adapters, provider receipts, and webhook readiness.
# ---------------------------------------------------------------------------


def test_adapters_report_a_visible_fallback_and_the_webhook_stays_unarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = store.connect(tmp_path / "adapters.sqlite3")
    rss = distribution.delivery_adapter("rss")
    assert rss["provider"] == "rss" and rss["status"] == "ready"
    assert rss["delivery_mode"] == "manual_receipt"

    # youtube_metadata is configured live with a credential reference and no
    # authenticated smoke, so it verifies blocked and the platform falls back to
    # the manual adapter — visibly, never as a silent substitution.
    youtube = distribution.delivery_adapter("youtube")
    assert youtube["provider"] == "manual_external_receipt"
    assert youtube["status"] == "ready"
    for short_form in distribution.SHORT_FORM_PLATFORMS:
        assert distribution.delivery_adapter(short_form)["provider"] == "clip_queue"

    # With no configured distribution provider at all the state is unconfigured
    # with a reason, not an exception and not an invented credential.
    runtime = configuration.load_runtime()
    empty = providers.ProviderRegistry(
        dataclasses.replace(runtime, providers={**runtime.providers, "distribution": ()})
    )
    unconfigured = distribution.delivery_adapter("rss", registry=empty)
    assert unconfigured == {
        "platform": "rss",
        "provider": None,
        "mode": None,
        "status": "unconfigured",
        "delivery_mode": "manual_receipt",
        "reason": "no configured provider is available for capability distribution",
    }

    webhook = distribution.webhook_contract(SHOW)
    assert webhook["status"] == "unconfigured"
    assert webhook["outbound_mode"] == "manual_receipt"
    assert "provider_connected" in webhook["reason"]
    assert webhook["requires_authorization_receipt"] is True
    assert webhook["payload_schema"] == "spec/distribution.schema.json"
    assert webhook["events"] == [
        "distribution.scheduled",
        "distribution.published",
        "distribution.failed",
    ]
    assert webhook["connected_platforms"] == []
    # An unregistered show falls back to the safe draft_only default rather than
    # failing the whole board on a bookkeeping gap.
    assert distribution.webhook_contract("not-a-configured-show")["outbound_mode"] == "draft_only"
    assert distribution.webhook_contract()["outbound_mode"] == "draft_only"

    # Opting a show into provider_connected is not enough on its own: with no
    # live adapter the webhook stays unconfigured, and says which half is missing.
    connected_show = dataclasses.replace(configuration.load_show(SHOW), outbound_mode="provider_connected")
    monkeypatch.setattr(configuration, "load_show", lambda show_id: connected_show)
    armed = distribution.webhook_contract(SHOW)
    assert armed["outbound_mode"] == "provider_connected"
    assert armed["status"] == "unconfigured"
    assert armed["reason"] == "no distribution adapter has an authenticated live smoke receipt"
    monkeypatch.undo()

    written = distribution.record_adapter_verification(
        conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer", now=NOW
    )
    assert {row["capability"] for row in written} == {"distribution"}
    assert {row["provider"] for row in written} == {"rss", "youtube_metadata", "clip_queue", "manual_external_receipt"}
    blocked = [row for row in written if row["status"] == "blocked"]
    assert [row["provider"] for row in blocked] == ["youtube_metadata"]
    assert "no authenticated live smoke receipt" in blocked[0]["details"]["reason"]
    conn.close()


# ---------------------------------------------------------------------------
# The Publish board.
# ---------------------------------------------------------------------------


def test_the_publication_board_projects_cards_blockers_and_totals(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "board.sqlite3")
    rss_job = _authorize(conn, _draft(conn))
    distribution.mark_published(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        distribution_id=rss_job["id"],
        external_id_ref="rss://feed/episode-12",
        actor_id="owner_fixture",
        actor_role="editorial_owner",
        now=NOW,
    )
    _draft(conn, platform_name="youtube", metadata=VIDEO_PACKAGE, key=f"{EPISODE}:youtube")
    distribution.queue_clip(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        start_seconds=30,
        end_seconds=90,
        platform_name="tiktok",
        caption_ref="caption://episode-12/clip-1",
        idempotency_key=f"{EPISODE}:tiktok",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    platform.add_clearance(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id="episode-13",
        clearance_type="clip",
        rights_holder_ref="vault://rights/two",
        license_terms_ref="vault://terms/two",
        now=NOW,
    )
    _draft(conn, episode_id="episode-13", key="episode-13:rss")

    board = distribution.publication_board(conn, tenant_id=TENANT, show_id=SHOW, actor_role="host")
    assert [episode["episode_id"] for episode in board["episodes"]] == ["episode-12", "episode-13"]
    twelve, thirteen = board["episodes"]
    assert [card["platform"] for card in twelve["platforms"]] == ["rss", "youtube"]
    assert [card["target_platform"] for card in twelve["clips"]] == ["tiktok"]
    assert twelve["publishable"] is True and twelve["blockers"] == []
    assert thirteen["publishable"] is False and thirteen["blockers"][0]["status"] == "pending"

    published_card = twelve["platforms"][0]
    assert published_card["status"] == "published"
    assert published_card["authorized"] is True
    assert published_card["external_id_ref"] == "rss://feed/episode-12"
    assert published_card["attempt_count"] == 1
    assert published_card["title"] == RSS_PACKAGE["title"]
    assert published_card["metadata"]["audio_url"] == RSS_PACKAGE["audio_url"]
    assert published_card["completeness"]["complete"] is True
    assert twelve["clips"][0]["caption_ref"] == "caption://episode-12/clip-1"

    assert board["totals"] == {
        "episodes": 2,
        "jobs": 4,
        "published": 1,
        "scheduled": 0,
        "failed": 0,
        "blocked_episodes": 1,
        "clips": 1,
    }
    assert set(board["adapters"]) == distribution.PLATFORMS
    assert board["webhook"]["status"] == "unconfigured"

    filtered = distribution.publication_board(
        conn, tenant_id=TENANT, show_id=SHOW, actor_role="host", episode_id="episode-13"
    )
    assert [episode["episode_id"] for episode in filtered["episodes"]] == ["episode-13"]
    with pytest.raises(distribution.DistributionError, match="cannot read distribution jobs"):
        distribution.publication_board(conn, tenant_id=TENANT, show_id=SHOW, actor_role="stranger")
    conn.close()


# ---------------------------------------------------------------------------
# HTTP surface: scoped, private-response, role-constrained.
# ---------------------------------------------------------------------------


def test_the_http_surface_is_complete_show_scoped_and_private(tmp_path: Path) -> None:
    database = tmp_path / "http.sqlite3"
    _seed(database)
    client = _client(database)

    drafted = client.post(
        f"/v1/shows/{SHOW}/distributions",
        headers=_headers(),
        json={
            "episode_id": EPISODE,
            "platform": "rss",
            "metadata": RSS_PACKAGE,
            "idempotency_key": f"{EPISODE}:rss",
        },
    )
    assert drafted.status_code == 201, drafted.text
    assert drafted.headers["Cache-Control"] == "no-store, private"
    job_id = drafted.json()["id"]
    assert drafted.json()["recorded_by_role"] == "producer"

    preview = client.post(
        f"/v1/shows/{SHOW}/distributions/preview",
        headers=_headers(),
        json={"platform": "rss", "metadata": RSS_PACKAGE},
    )
    assert preview.status_code == 200
    ET.fromstring(preview.json()["body"])
    assert preview.headers["Cache-Control"] == "no-store, private"

    clip = client.post(
        f"/v1/shows/{SHOW}/clips",
        headers=_headers(),
        json={
            "episode_id": EPISODE,
            "target_platform": "reels",
            "start_seconds": 30,
            "end_seconds": 90,
            "caption_ref": "caption://episode-12/clip-1",
            "hashtags": ["podcast"],
            "idempotency_key": f"{EPISODE}:reels",
        },
    )
    assert clip.status_code == 201, clip.text
    assert clip.json()["target_platform"] == "reels"

    # A producer may draft but not authorize; an owner may.
    refused = client.post(
        f"/v1/shows/{SHOW}/distributions/{job_id}/authorize",
        headers=_headers(),
        json={"authorization_ref": "receipt://owner/publish-12", "idempotency_key": f"{job_id}:publish"},
    )
    assert refused.status_code == 403
    # An authorized role with a malformed payload is a 422, not a 403.
    malformed = client.post(
        f"/v1/shows/{SHOW}/distributions/{job_id}/authorize",
        headers=_headers(OWNER_TOKEN),
        json={},
    )
    assert malformed.status_code == 422
    authorized = client.post(
        f"/v1/shows/{SHOW}/distributions/{job_id}/authorize",
        headers=_headers(OWNER_TOKEN),
        json={"authorization_ref": "receipt://owner/publish-12", "idempotency_key": f"{job_id}:publish"},
    )
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["status"] == "authorized"

    scheduled = client.post(
        f"/v1/shows/{SHOW}/distributions/{job_id}/schedule",
        headers=_headers(),
        json={"scheduled_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()},
    )
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()["status"] == "scheduled"

    failure = client.post(
        f"/v1/shows/{SHOW}/distributions/{job_id}/failures",
        headers=_headers(),
        json={"error_ref": "error://provider/timeout"},
    )
    assert failure.status_code == 201
    assert failure.json()["status"] == "failed"

    published = client.post(
        f"/v1/shows/{SHOW}/distributions/{job_id}/publication-receipt",
        headers=_headers(OWNER_TOKEN),
        json={"external_id_ref": "rss://feed/episode-12"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["attempt_count"] == 2

    receipts = client.get(f"/v1/shows/{SHOW}/distributions/{job_id}/receipts", headers=_headers(HOST_TOKEN))
    assert receipts.status_code == 200
    assert [row["attempt"] for row in receipts.json()["deliveries"]] == [1, 2]
    assert receipts.headers["Cache-Control"] == "no-store, private"

    listed = client.get(f"/v1/shows/{SHOW}/distributions?status=published", headers=_headers(HOST_TOKEN))
    assert [row["id"] for row in listed.json()] == [job_id]

    board = client.get(f"/v1/shows/{SHOW}/distribution-board", headers=_headers(HOST_TOKEN))
    assert board.status_code == 200
    assert board.json()["totals"]["published"] == 1
    assert board.headers["Cache-Control"] == "no-store, private"

    verification = client.post(f"/v1/shows/{SHOW}/distribution-adapters/verification", headers=_headers(), json={})
    assert verification.status_code == 201
    assert {row["capability"] for row in verification.json()} == {"distribution"}

    # A draft-only show refuses the authorization, so nothing can be published.
    draft_only = client.post(
        f"/v1/shows/{DRAFT_ONLY_SHOW}/distributions",
        headers=_headers(session_show=DRAFT_ONLY_SHOW),
        json={
            "episode_id": EPISODE,
            "platform": "rss",
            "metadata": {"title": "Flagship"},
            "idempotency_key": f"{EPISODE}:flagship:rss",
        },
    )
    assert draft_only.status_code == 201
    assert (
        client.post(
            f"/v1/shows/{DRAFT_ONLY_SHOW}/distributions/{draft_only.json()['id']}/authorize",
            headers=_headers(OWNER_TOKEN, session_show=DRAFT_ONLY_SHOW),
            json={"authorization_ref": "receipt://owner/flagship", "idempotency_key": "flagship:publish"},
        ).status_code
        == 403
    )

    # An identity bound to one show cannot reach another one's board.
    bound = _client(database, bound_show=SHOW)
    assert bound.get(f"/v1/shows/{OTHER_SHOW}/distribution-board", headers=_headers()).status_code == 403
    assert (
        bound.post(
            f"/v1/shows/{OTHER_SHOW}/distributions",
            headers=_headers(),
            json={
                "episode_id": EPISODE,
                "platform": "rss",
                "metadata": {"title": "Cross show"},
                "idempotency_key": "cross:rss",
            },
        ).status_code
        == 403
    )

    # Every route named in the documented surface exists, and no route carries
    # an action verb HOSPES does not perform.
    paths = set(client.app.app.openapi()["paths"])
    for route in (
        f"/v1/shows/{{show_id}}/distributions",
        f"/v1/shows/{{show_id}}/distributions/preview",
        f"/v1/shows/{{show_id}}/distributions/{{distribution_id}}/authorize",
        f"/v1/shows/{{show_id}}/distributions/{{distribution_id}}/schedule",
        f"/v1/shows/{{show_id}}/distributions/{{distribution_id}}/publication-receipt",
        f"/v1/shows/{{show_id}}/distributions/{{distribution_id}}/failures",
        f"/v1/shows/{{show_id}}/distributions/{{distribution_id}}/receipts",
        f"/v1/shows/{{show_id}}/clips",
        f"/v1/shows/{{show_id}}/distribution-board",
        f"/v1/shows/{{show_id}}/distribution-adapters/verification",
    ):
        assert route in paths, route
    assert not any(
        action in path for path in paths for action in ("send", "deliver", "book", "sign", "publish", "distribute")
    )


# ---------------------------------------------------------------------------
# Migration, schema, contract, UI, and documentation surfaces.
# ---------------------------------------------------------------------------


def test_migration_schema_contract_dashboard_and_documentation_are_complete(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "schema.sqlite3")
    ledger = {row["version"]: row["name"] for row in migrations.applied_migrations(conn)}
    assert ledger[21] == "distribution_publishing_pipeline"
    assert migrations.LATEST_VERSION >= 21
    assert {"distribution_receipts", "delivery_receipts"} <= store.table_names(conn)
    distribution_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(distributions)")}
    assert {
        "target_platform",
        "package_checksum",
        "attempt_count",
        "last_attempt_at",
        "last_error_ref",
        "recorded_by",
        "recorded_by_role",
        "scheduled_by",
        "scheduled_by_role",
        "correlation_id",
    } <= distribution_columns
    delivery_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(delivery_receipts)")}
    assert "delivery_mode" in delivery_columns
    indexes = {row["name"] for row in store.fetch_all(conn, "PRAGMA index_list(distributions)")}
    assert {"ix_distribution_episode_platform", "ix_distribution_schedule"} <= indexes
    receipt_indexes = {row["name"] for row in store.fetch_all(conn, "PRAGMA index_list(distribution_receipts)")}
    assert "ux_distribution_receipt_correlation" in receipt_indexes
    conn.close()

    schema = json.loads((ROOT / "spec/distribution.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["platform"]["enum"] == sorted(
        distribution.PLATFORMS,
        key=[
            "rss",
            "youtube",
            "tiktok",
            "reels",
            "shorts",
            "clip_queue",
        ].index,
    )
    assert schema["properties"]["status"]["enum"] == list(distribution.STATUSES)
    assert schema["properties"]["family"]["enum"] == ["rss", "video", "clip"]
    assert [ref["$ref"].rsplit("/", 1)[1] for ref in schema["properties"]["metadata"]["oneOf"]] == [
        "rss_package",
        "video_package",
        "clip_package",
    ]
    assert set(schema["$defs"]["clip_package"]["required"]) == {
        "start_seconds",
        "end_seconds",
        "target_platform",
        "caption_ref",
    }
    assert schema["$defs"]["rss_package"]["required"] == ["title"]
    assert schema["$defs"]["video_package"]["properties"]["tags"]["maxItems"] == distribution.MAX_TAGS
    delivery_schema = json.loads((ROOT / "spec/delivery_receipt.schema.json").read_text(encoding="utf-8"))
    assert delivery_schema["properties"]["delivery_mode"]["enum"] == list(distribution.DELIVERY_MODES)
    assert delivery_schema["properties"]["result"]["enum"] == ["delivered", "failed"]
    assert delivery_schema["properties"]["authorization_action"]["const"] == "publish"

    for relative in (
        "spec/distribution.schema.json",
        "spec/delivery_receipt.schema.json",
        "config/domain_kernel.yaml",
        "dashboard/index.html",
        "dashboard/assets/api.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/guide.json",
        "dashboard/assets/partnership.js",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/styles.css",
    ):
        assert (ROOT / relative).read_bytes() == (ROOT / "hospes/resources" / relative).read_bytes(), relative

    domain = (ROOT / "config/domain_kernel.yaml").read_text(encoding="utf-8")
    assert "distribution_contract:" in domain
    for marker in (
        "package_families: [rss, video, clip]",
        "delivery_modes: [manual_receipt, provider_connected]",
        "webhook_payload_schema: spec/distribution.schema.json",
        "HOSPES never publishes",
        "immutable delivery receipt at attempt n+1",
        "A published external identifier is immutable",
        "stays visibly unconfigured until a show opts into provider_connected",
    ):
        assert marker in domain, marker
    for entity in ("DistributionPackage", "DistributionReceipt", "DeliveryReceipt"):
        assert f"- {entity}\n" in domain
    for event in (
        "distribution.drafted",
        "distribution.scheduled",
        "distribution.authorized",
        "distribution.published",
        "distribution.failed",
        "distribution.clip_queued",
    ):
        assert f"- {event}\n" in domain

    html = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in ("dashboard/index.html", "dashboard/assets/partnership-workspace.html")
    )
    javascript = (ROOT / "dashboard/assets/partnership-workspace.js").read_text(encoding="utf-8")
    api_javascript = (ROOT / "dashboard/assets/api.js").read_text(encoding="utf-8")
    shell_javascript = (ROOT / "dashboard/assets/partnership.js").read_text(encoding="utf-8")
    capabilities = (ROOT / "dashboard/assets/capabilities.mjs").read_text(encoding="utf-8")
    styles = (ROOT / "dashboard/assets/styles.css").read_text(encoding="utf-8")
    assert 'data-view="publish"' in html
    # The view id lives once in the shared registry (dashboard/assets/views.mjs);
    # the workspace and the shell both drive off it rather than each pinning
    # their own literal view list, so assert participation, not a literal string.
    views_registry = (ROOT / "dashboard/assets/views.mjs").read_text(encoding="utf-8")
    assert "'publish'" in views_registry
    assert "import { VIEWS, viewTitle } from './views.mjs';" in javascript
    assert "import { showView } from './views.mjs';" in shell_javascript
    # Every element the Publish code addresses must exist in the shipped markup:
    # a missing id is a silent runtime failure no Python test would otherwise see.
    for element_id in (
        "publish-view",
        "publish-cards",
        "publish-gate",
        "publish-adapters",
        "publish-episode",
        "publish-webhook",
        "publish-preview-note",
        "publish-preview-output",
        "pub-published",
        "pub-scheduled",
        "pub-failed",
        "pub-blocked",
        "btn-publish-refresh",
        "btn-verify-adapters",
        "rss-form",
        "youtube-form",
        "clip-form",
    ):
        assert f'id="{element_id}"' in html, element_id
        assert f"'#{element_id}'" in javascript, element_id
    for exported in (
        "loadPublishBoard",
        "previewDistribution",
        "saveDistributionDraft",
        "queueClip",
        "authorizeDistribution",
        "scheduleDistribution",
        "markDistributionPublished",
        "recordDistributionFailure",
        "loadDistributionReceipts",
        "verifyDistributionAdapters",
    ):
        assert f"export function {exported}(" in api_javascript, exported
    assert "distribution-board" in api_javascript
    assert "publication-receipt" in api_javascript
    assert "escapeHTML(card.title" in javascript
    assert "escapeHTML(adapter.provider" in javascript
    for capability in ("canViewPublishing", "canManagePublishing", "canAuthorizePublication"):
        assert capability in capabilities
        assert capability in javascript, capability
    # The dashboard-quality audit throws under a 120px textarea; the preview
    # pane declares its own floor rather than inheriting one.
    assert "#publish-preview-output { display: block; min-height: 200px; width: 100%; }" in styles
    assert "#rss-form textarea, #youtube-form textarea { min-height: 120px; }" in styles

    guide = json.loads((ROOT / "dashboard/assets/guide.json").read_text(encoding="utf-8"))
    assert {"canViewPublishing", "canManagePublishing", "canAuthorizePublication"} <= set(guide["capabilities"])
    assert "publish" in {beat["view"] for beat in guide["beats"]}
    assert {"publish-view", "publish-cards", "rss-form", "youtube-form", "clip-form"} <= set(guide["elements"])

    documentation = (ROOT / "docs/publishing-pipeline.md").read_text(encoding="utf-8")
    for phrase in (
        "Three package families",
        "package_completeness",
        "Immutable delivery evidence",
        "attempt *n+1*",
        "manual_receipt",
        "Cache-Control: no-store, private",
        "Webhook-ready, and visibly not yet armed",
        "distribution_publishing_pipeline",
    ):
        assert phrase in documentation, phrase
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/publishing-pipeline.md" in readme
