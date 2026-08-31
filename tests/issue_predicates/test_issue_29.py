"""Executable completion predicate for HOSPES issue #29.

Close condition: validated provider imports and pulls, read-only scheduling,
metrics and trends UI, CSV export, and provider receipts pass.

Required surfaces: cli, api, schema, service, ui, security, documentation,
provider_receipts.
"""

from __future__ import annotations

import csv
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import synthetic_bearer_authenticator
from hospes import analytics, configuration, jobs, platform, providers, store
from hospes.__main__ import local_job_handlers, main
from hospes.api import create_app


try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - exercised only without the api extra
    TestClient = None  # type: ignore[assignment]


UTC = timezone.utc
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
TENANT = "hospes"
SHOW = "flagship"
OTHER_SHOW = "field"
PRODUCER_TOKEN = "issue29-producer-token-0123456789abcdef"  # allow-secret: fixture
HEADERS = {"Authorization": f"Bearer {PRODUCER_TOKEN}"}

SPOTIFY_EXPORT = (
    "episode_id,period_start,period_end,downloads,retention_%,top_clips,top_clip_ref\n"
    "episode-11,2026-07-01,2026-07-07,9000,61,31000,clip://synthetic/11\n"
    "episode-12,2026-07-08,2026-07-14,12450,68,45000,clip://synthetic/12\n"
)
MEGAPHONE_EXPORT = (
    "episode_id,period_start,period_end,downloads,retention_pct\nepisode-12,2026-07-08,2026-07-14,1550,72\n"
)


def _headers(session_show: str | None = None) -> dict[str, str]:
    headers = dict(HEADERS)
    if session_show is not None:
        headers["X-Session-Show"] = session_show
    return headers


class _SessionShowASGI:
    """Project the operator's active show into scope, as the session layer does."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"x-session-show":
                    scope["hospes.operator_show"] = value.decode("utf-8")
        await self.app(scope, receive, send)


class _ShowBoundAuthenticator:
    def __init__(self, inner, show_id: str) -> None:
        self.inner = inner
        self.show_id = show_id

    def authenticate(self, presented_bearer):
        identity = self.inner.authenticate(presented_bearer)
        return dataclasses.replace(identity, show_id=self.show_id)


def _seed(path: Path) -> store.DatabaseConnection:
    conn = store.connect(path)
    for show_id, label in ((SHOW, "Flagship Show"), (OTHER_SHOW, "Field Show")):
        platform.register_show(
            conn,
            tenant_id=TENANT,
            show_id=show_id,
            label=label,
            config_ref=f"config/shows/{show_id}.yaml",
            now=NOW,
        )
    conn.commit()
    return conn


def _exports(tmp_path: Path) -> tuple[Path, Path]:
    spotify = tmp_path / "spotify-export.csv"
    spotify.write_text(SPOTIFY_EXPORT, encoding="utf-8")
    megaphone = tmp_path / "megaphone-export.csv"
    megaphone.write_text(MEGAPHONE_EXPORT, encoding="utf-8")
    return spotify, megaphone


def _policy(**import_paths: str) -> dict:
    return {
        "version": 1,
        "providers": {name: {"enabled": True, "import_path": path} for name, path in import_paths.items()},
    }


def _client(path: Path, *, bound_show: str | None = None) -> TestClient:
    authenticator = synthetic_bearer_authenticator({PRODUCER_TOKEN: ("producer_fixture", "producer", TENANT)})
    if bound_show is not None:
        authenticator = _ShowBoundAuthenticator(authenticator, bound_show)
    app = create_app(
        str(path),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=authenticator,
        csrf_required=False,
    )
    return TestClient(_SessionShowASGI(app))


# --- schema + service: validated provider imports --------------------------


def test_provider_imports_are_schema_validated_normalized_and_idempotent(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue29-import.sqlite3")
    spotify, _ = _exports(tmp_path)

    first = analytics.import_csv(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        provider="spotify_creator_csv",
        path=spotify,
        source_receipt_ref="receipt://analytics/spotify_creator_csv/fixture",
        now=NOW,
    )
    assert len(first) == 2

    # Provider column spellings normalize onto the canonical metric names while
    # the values keep their provider spelling verbatim.
    latest = next(row for row in first if row["episode_id"] == "episode-12")
    assert latest["metrics"]["retention_pct"] == "68"
    assert latest["metrics"]["top_clip_views"] == "45000"
    assert latest["metrics"]["top_clip_ref"] == "clip://synthetic/12"
    assert latest["source_receipt_ref"] == "receipt://analytics/spotify_creator_csv/fixture"

    # Re-reading the same export writes nothing new.
    again = analytics.import_csv(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        provider="spotify_creator_csv",
        path=spotify,
        source_receipt_ref="receipt://analytics/spotify_creator_csv/fixture",
        now=NOW,
    )
    assert [row["id"] for row in again] == [row["id"] for row in first]
    assert len(analytics.list_metrics(conn, tenant_id=TENANT, show_id=SHOW)) == 2

    # The shipped schema is the enforcement authority, not decoration.
    contract = analytics.schema()
    assert "source_receipt_ref" in contract["required"]
    assert contract["additionalProperties"] is False
    valid = {
        "episode_id": "episode-13",
        "period_start": "2026-07-15",
        "period_end": "2026-07-21",
        "downloads": 10,
    }
    for provider, receipt, message in (
        ("not_a_provider", "receipt://analytics/x", "unsupported analytics provider"),
        ("spotify_creator_csv", "not-an-opaque-reference", "schema pattern"),
        ("spotify_creator_csv", "", "missing source_receipt_ref"),
    ):
        with pytest.raises(analytics.AnalyticsError, match=message):
            analytics.normalize_row(valid, provider=provider, source_receipt_ref=receipt, now=NOW)
    for row, message in (
        ({**valid, "period_start": "not-a-date"}, "ISO 8601"),
        ({**valid, "period_start": "2026-07-22"}, "cannot be after"),
        ({**valid, "episode_id": " "}, "require episode_id"),
    ):
        with pytest.raises(analytics.AnalyticsError, match=message):
            analytics.normalize_row(
                row,
                provider="spotify_creator_csv",
                source_receipt_ref="receipt://analytics/spotify/1",
                now=NOW,
            )
    with pytest.raises(analytics.AnalyticsError, match="undeclared fields"):
        analytics.validate_record(
            {
                **analytics.normalize_row(
                    valid,
                    provider="spotify_creator_csv",
                    source_receipt_ref="receipt://analytics/spotify/1",
                    now=NOW,
                ),
                "id": "smuggled",
            }
        )
    conn.close()


# --- provider_receipts: every pull is attributable -------------------------


def test_every_provider_pull_writes_a_receipt_and_keeps_unready_state_visible(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue29-fetch.sqlite3")
    spotify, megaphone = _exports(tmp_path)

    result = analytics.fetch(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        policy=_policy(
            spotify_creator_csv=str(spotify),
            megaphone_chartable_import=str(tmp_path / "absent-export.csv"),
        ),
        now=NOW,
    )
    by_provider = {row["provider"]: row for row in result["providers"]}

    # The shipped runtime declares four analytics providers; each one ends in an
    # attributable state and each one leaves a receipt.
    assert set(by_provider) == analytics.SUPPORTED_PROVIDERS
    assert by_provider["spotify_creator_csv"]["status"] == "imported"
    assert by_provider["spotify_creator_csv"]["imported"] == 2
    assert by_provider["megaphone_chartable_import"]["status"] == "unavailable"
    assert by_provider["youtube_analytics"]["status"] == "blocked"
    assert by_provider["custom_csv_http"]["status"] == "blocked"
    assert result["imported"] == 2

    receipts = analytics.list_receipts(conn, tenant_id=TENANT, show_id=SHOW)
    assert {row["provider"] for row in receipts} == analytics.SUPPORTED_PROVIDERS
    assert all(row["capability"] == "analytics" for row in receipts)

    # Every stored metric points back at the receipt that authorized its read.
    stored = analytics.list_metrics(conn, tenant_id=TENANT, show_id=SHOW)
    receipt_ids = {row["id"] for row in receipts}
    assert stored
    for row in stored:
        assert row["source_receipt_ref"].startswith("receipt://analytics/spotify_creator_csv/")
        assert row["source_receipt_ref"].rsplit("/", 1)[-1] in receipt_ids

    # A blocked provider is never substituted with another provider's numbers.
    assert {row["provider"] for row in stored} == {"spotify_creator_csv"}

    # Nothing that reaches an operator names a credential.
    assert "credential://" not in json.dumps(result)
    assert "credential://" not in json.dumps(receipts)

    # An unconfigured provider is a visible state, not a silent skip.
    unconfigured = analytics.fetch(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        provider="megaphone_chartable_import",
        policy=_policy(spotify_creator_csv=str(spotify)),
        now=NOW,
    )
    assert [row["status"] for row in unconfigured["providers"]] == ["unconfigured"]
    assert analytics.provider_import_path(_policy(), "megaphone_chartable_import") is None

    # A second provider's export lands beside the first without collision.
    both = analytics.fetch(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        provider="megaphone_chartable_import",
        source_path=megaphone,
        now=NOW,
    )
    assert both["providers"][0]["status"] == "imported"
    assert len(analytics.list_metrics(conn, tenant_id=TENANT, show_id=SHOW)) == 3

    with pytest.raises(analytics.AnalyticsError, match="unsupported analytics provider"):
        analytics.fetch(conn, tenant_id=TENANT, show_id=SHOW, provider="not_a_provider", now=NOW)
    with pytest.raises(analytics.AnalyticsError, match="requires an explicit provider"):
        analytics.fetch(conn, tenant_id=TENANT, show_id=SHOW, source_path=megaphone, now=NOW)
    conn.close()


def test_a_capability_with_no_ready_provider_reports_blocked_and_imports_nothing(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue29-blocked.sqlite3")
    spotify, _ = _exports(tmp_path)
    runtime = configuration.load_runtime()
    live_only = tuple(item for item in runtime.providers["analytics"] if item.mode == "live")
    assert live_only, "the shipped runtime must declare a credential-gated analytics provider"
    registry = providers.ProviderRegistry(
        dataclasses.replace(runtime, providers={**runtime.providers, "analytics": live_only})
    )

    result = analytics.fetch(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        registry=registry,
        policy=_policy(youtube_analytics=str(spotify), custom_csv_http=str(spotify)),
        now=NOW,
    )
    assert result["imported"] == 0
    assert {row["status"] for row in result["providers"]} == {"blocked"}
    assert analytics.list_metrics(conn, tenant_id=TENANT, show_id=SHOW) == []
    # The blocked read is still receipted.
    assert len(analytics.list_receipts(conn, tenant_id=TENANT, show_id=SHOW)) == len(live_only)
    conn.close()


# --- security: read-only scheduling ---------------------------------------


def test_scheduled_analytics_work_is_read_only_and_runs_through_the_worker(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue29-schedule.sqlite3")
    spotify, _ = _exports(tmp_path)

    job = analytics.schedule_fetch(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        created_by="operator_fixture",
        period="2026-07-14",
        now=NOW,
    )
    assert job["job_type"] == analytics.FETCH_JOB_TYPE == "analytics.fetch"
    assert job["execution_kind"] == "scheduled"
    assert job["operation"] == "read"
    assert job["status"] == "queued"

    # Scheduling is idempotent within its window.
    assert (
        analytics.schedule_fetch(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            created_by="operator_fixture",
            period="2026-07-14",
            now=NOW,
        )["id"]
        == job["id"]
    )

    # The queue itself refuses a scheduled job that is not an analytics/research
    # read, so a scheduled analytics job cannot acquire send or publish authority.
    base = dataclasses.replace(
        jobs.JobSpec(
            tenant_id=TENANT,
            show_id=SHOW,
            job_type=analytics.FETCH_JOB_TYPE,
            payload_ref=job["payload_ref"],
            payload_checksum=job["payload_checksum"],
            idempotency_key="analytics:flagship:all:2026-07-21",
            created_by="operator_fixture",
            execution_kind="scheduled",
            operation="read",
        ),
    )
    with pytest.raises(jobs.JobValidationError, match="may only read external sources"):
        jobs.enqueue(conn, dataclasses.replace(base, operation="prepare"), now=NOW)
    with pytest.raises(jobs.JobValidationError, match="restricted to analytics and research"):
        jobs.enqueue(
            conn,
            dataclasses.replace(base, job_type="distribution.publish", idempotency_key="dist:flagship:2026-07-21"),
            now=NOW,
        )
    with pytest.raises(analytics.AnalyticsError, match="unsupported analytics provider"):
        analytics.schedule_fetch(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            created_by="operator_fixture",
            provider="not_a_provider",
            now=NOW,
        )

    # The local worker's authority is its handler map, and it holds only reads.
    handlers = local_job_handlers(conn)
    assert set(handlers) == {analytics.FETCH_JOB_TYPE}

    completed = jobs.run_available(
        conn,
        worker_id="issue29_worker",
        handlers={
            analytics.FETCH_JOB_TYPE: analytics.job_handler(
                conn,
                policy=_policy(spotify_creator_csv=str(spotify)),
                now=NOW,
            )
        },
        max_jobs=2,
    )
    assert [row["status"] for row in completed] == ["succeeded"]
    assert completed[0]["result_ref"].startswith("analytics://")
    assert len(analytics.list_metrics(conn, tenant_id=TENANT, show_id=SHOW)) == 2

    receipt = store.fetch_one(
        conn,
        "SELECT * FROM job_attempt_receipts WHERE job_id = ? AND attempt = 1",
        (job["id"],),
    )
    assert receipt is not None and receipt["outcome"] == "succeeded"
    conn.close()


# --- service: trends and export -------------------------------------------


def test_trends_sum_counters_average_ratios_and_bound_the_window(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue29-trends.sqlite3")
    spotify, megaphone = _exports(tmp_path)
    for provider, export in (("spotify_creator_csv", spotify), ("megaphone_chartable_import", megaphone)):
        analytics.import_csv(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            provider=provider,
            path=export,
            source_receipt_ref=f"receipt://analytics/{provider}/fixture",
            now=NOW,
        )

    report = analytics.trends(conn, tenant_id=TENANT, show_id=SHOW, limit=12)
    assert [point["episode_id"] for point in report["episodes"]] == ["episode-11", "episode-12"]
    latest = report["episodes"][-1]
    # Counters sum across the providers that reported the episode...
    assert latest["downloads"] == 12450 + 1550
    assert latest["top_clip_views"] == 45000
    # ...ratios average over them.
    assert latest["retention_pct"] == 70
    assert latest["providers"] == ["megaphone_chartable_import", "spotify_creator_csv"]
    assert report["series"]["downloads"] == [9000, 14000]
    assert report["totals"]["downloads"] == 23000
    assert report["episodes_available"] == 2

    # The window bounds the projection without hiding the true denominator.
    windowed = analytics.trends(conn, tenant_id=TENANT, show_id=SHOW, limit=1)
    assert [point["episode_id"] for point in windowed["episodes"]] == ["episode-12"]
    assert windowed["episodes_available"] == 2
    for bad_limit in (0, 101, True):
        with pytest.raises(analytics.AnalyticsError, match="between 1 and 100"):
            analytics.trends(conn, tenant_id=TENANT, show_id=SHOW, limit=bad_limit)

    # Non-numeric provider values are skipped by the projection, never coerced.
    analytics.import_rows(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        provider="custom_csv_http",
        rows=[
            {
                "episode_id": "episode-13",
                "period_start": "2026-07-15",
                "period_end": "2026-07-21",
                "downloads": "unavailable",
            }
        ],
        source_receipt_ref="receipt://analytics/custom_csv_http/fixture",
        now=NOW,
    )
    partial = analytics.trends(conn, tenant_id=TENANT, show_id=SHOW)
    assert partial["episodes"][-1] == {
        "episode_id": "episode-13",
        "period_start": "2026-07-15",
        "period_end": "2026-07-21",
        "providers": ["custom_csv_http"],
    }
    conn.close()


def test_export_csv_carries_canonical_columns_and_neutralizes_formulas(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue29-export.sqlite3")
    analytics.import_rows(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        provider="spotify_creator_csv",
        rows=[
            {
                "episode_id": "episode-12",
                "period_start": "2026-07-08",
                "period_end": "2026-07-14",
                "downloads": "12450",
                "retention_%": "68",
                "top_clips": "-45000",
                "top_clip_ref": '=HYPERLINK("http://evil.test")',
            }
        ],
        source_receipt_ref="receipt://analytics/spotify_creator_csv/fixture",
        now=NOW,
    )
    body = analytics.export_csv(conn, tenant_id=TENANT, show_id=SHOW)
    header, row = body.strip().splitlines()
    columns = header.split(",")
    for name in ("provider", "episode_id", "downloads", "retention_pct", "top_clip_views", "source_receipt_ref"):
        assert name in columns

    values = dict(zip(columns, next(csv.reader([row]))))
    assert values["episode_id"] == "episode-12"
    assert values["downloads"] == "12450"
    assert values["retention_pct"] == "68"
    # A real negative number stays arithmetically usable...
    assert values["top_clip_views"] == "-45000"
    # ...while a provider-supplied formula cannot be evaluated by a spreadsheet.
    assert values["top_clip_ref"].startswith("'=")

    scoped = analytics.export_csv(conn, tenant_id=TENANT, show_id=SHOW, episode_id="episode-99")
    assert scoped.strip().splitlines() == [header]
    conn.close()


# --- api + security --------------------------------------------------------


@pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")
def test_api_reads_are_show_scoped_private_and_never_echo_credentials(tmp_path: Path) -> None:
    database = tmp_path / "issue29-api.sqlite3"
    conn = _seed(database)
    conn.close()
    spotify, _ = _exports(tmp_path)

    with _client(database) as client:
        created = client.post(
            f"/v1/shows/{SHOW}/analytics/imports",
            json={
                "provider": "spotify_creator_csv",
                "source_receipt_ref": "receipt://analytics/spotify_creator_csv/api",
                "rows": [
                    {
                        "episode_id": "episode-12",
                        "period_start": "2026-07-08",
                        "period_end": "2026-07-14",
                        "downloads": 12450,
                        "retention_%": 68,
                        "top_clips": 45000,
                    }
                ],
            },
            headers=_headers(SHOW),
        )
        assert created.status_code == 201, created.text
        assert created.json()["imported"] == 1
        assert created.headers["Cache-Control"] == "no-store, private"

        # An invalid import is rejected by the schema, not persisted.
        rejected = client.post(
            f"/v1/shows/{SHOW}/analytics/imports",
            json={"provider": "spotify_creator_csv", "source_receipt_ref": "nope", "rows": [{"episode_id": "x"}]},
            headers=_headers(SHOW),
        )
        assert rejected.status_code == 422
        empty = client.post(
            f"/v1/shows/{SHOW}/analytics/imports",
            json={"provider": "spotify_creator_csv", "source_receipt_ref": "receipt://a/b", "rows": []},
            headers=_headers(SHOW),
        )
        assert empty.status_code == 422

        listed = client.get(f"/v1/shows/{SHOW}/analytics", headers=_headers(SHOW))
        assert listed.status_code == 200
        assert [row["episode_id"] for row in listed.json()] == ["episode-12"]
        assert listed.json()[0]["metrics"]["retention_pct"] == 68

        trends = client.get(f"/v1/shows/{SHOW}/analytics/trends?limit=12", headers=_headers(SHOW)).json()
        assert trends["episodes"][-1]["downloads"] == 12450
        assert trends["episodes"][-1]["top_clip_views"] == 45000

        export = client.get(f"/v1/shows/{SHOW}/analytics/export.csv", headers=_headers(SHOW))
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        assert export.headers["Cache-Control"] == "no-store, private"
        assert 'filename="analytics-flagship.csv"' in export.headers["Content-Disposition"]
        assert "episode-12" in export.text

        fetched = client.post(f"/v1/shows/{SHOW}/analytics/fetches", json={}, headers=_headers(SHOW))
        assert fetched.status_code == 201, fetched.text
        assert set(row["provider"] for row in fetched.json()["providers"]) == analytics.SUPPORTED_PROVIDERS
        assert "credential://" not in fetched.text

        receipts = client.get(f"/v1/shows/{SHOW}/analytics/receipts", headers=_headers(SHOW))
        assert receipts.status_code == 200
        assert receipts.json()
        assert "credential://" not in receipts.text

        scheduled = client.post(
            f"/v1/shows/{SHOW}/analytics/schedules",
            json={"period": "2026-07-14"},
            headers=_headers(SHOW),
        )
        assert scheduled.status_code == 201, scheduled.text
        assert scheduled.json()["execution_kind"] == "scheduled"
        assert scheduled.json()["operation"] == "read"

        # There is no analytics route that publishes or sends.
        assert client.post(f"/v1/shows/{SHOW}/analytics/publish", json={}, headers=_headers(SHOW)).status_code == 404

        # A session scoped to one show cannot read or write another show's analytics.
        for method, path in (
            ("get", f"/v1/shows/{OTHER_SHOW}/analytics"),
            ("get", f"/v1/shows/{OTHER_SHOW}/analytics/trends"),
            ("get", f"/v1/shows/{OTHER_SHOW}/analytics/receipts"),
            ("get", f"/v1/shows/{OTHER_SHOW}/analytics/export.csv"),
        ):
            assert getattr(client, method)(path, headers=_headers(SHOW)).status_code == 403
        for path in (
            f"/v1/shows/{OTHER_SHOW}/analytics/imports",
            f"/v1/shows/{OTHER_SHOW}/analytics/fetches",
            f"/v1/shows/{OTHER_SHOW}/analytics/schedules",
        ):
            assert client.post(path, json={}, headers=_headers(SHOW)).status_code == 403

        # The other show's store stays empty after every rejected attempt.
        assert client.get(f"/v1/shows/{OTHER_SHOW}/analytics", headers=_headers(OTHER_SHOW)).json() == []

    with _client(database, bound_show=SHOW) as bound:
        assert bound.get(f"/v1/shows/{OTHER_SHOW}/analytics", headers=_headers()).status_code == 403
        assert bound.get(f"/v1/shows/{SHOW}/analytics", headers=_headers()).status_code == 200


# --- cli -------------------------------------------------------------------


def test_cli_fetches_lists_trends_exports_and_schedules(tmp_path: Path, capsys, monkeypatch) -> None:
    database = tmp_path / "issue29-cli.sqlite3"
    _seed(database).close()
    spotify, _ = _exports(tmp_path)

    assert (
        main(
            [
                "fetch-analytics",
                "--db",
                str(database),
                "--tenant",
                TENANT,
                "--show",
                SHOW,
                "--provider",
                "spotify_creator_csv",
                "--source",
                str(spotify),
            ]
        )
        == 0
    )
    fetched = json.loads(capsys.readouterr().out.strip())
    assert fetched["imported"] == 2
    assert fetched["providers"][0]["status"] == "imported"

    assert main(["analytics", "list", "--db", str(database), "--tenant", TENANT, "--show", SHOW]) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert {row["episode_id"] for row in listed} == {"episode-11", "episode-12"}

    assert (
        main(["analytics", "trends", "--db", str(database), "--tenant", TENANT, "--show", SHOW, "--limit", "12"]) == 0
    )
    report = json.loads(capsys.readouterr().out.strip())
    assert report["totals"]["downloads"] == 21450

    destination = tmp_path / "report.csv"
    assert (
        main(
            [
                "analytics",
                "export",
                "--db",
                str(database),
                "--tenant",
                TENANT,
                "--show",
                SHOW,
                "--out",
                str(destination),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert "episode-12" in destination.read_text(encoding="utf-8")

    assert (
        main(
            [
                "analytics",
                "schedule",
                "--db",
                str(database),
                "--tenant",
                TENANT,
                "--show",
                SHOW,
                "--actor",
                "operator_fixture",
                "--period",
                "2026-07-14",
            ]
        )
        == 0
    )
    scheduled = json.loads(capsys.readouterr().out.strip())
    assert scheduled["job_type"] == "analytics.fetch"
    assert scheduled["execution_kind"] == "scheduled"
    assert scheduled["operation"] == "read"

    assert main(["jobs", "run", "--db", str(database), "--worker", "issue29_cli_worker"]) == 0
    ran = json.loads(capsys.readouterr().out.strip())
    assert ran["registered_handlers"] == 1

    # A scoped failure exits non-zero and names the boundary rather than crashing.
    assert main(["analytics", "trends", "--db", str(database), "--tenant", TENANT, "--show", SHOW, "--limit", "0"]) == 2
    assert "between 1 and 100" in capsys.readouterr().err


# --- ui + documentation + packaged parity ----------------------------------


def test_dashboard_domain_kernel_docs_and_packaged_resources_are_complete() -> None:
    for relative in (
        "dashboard/index.html",
        "dashboard/assets/api.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/views.mjs",
        "dashboard/assets/partnership.js",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/styles.css",
        "dashboard/assets/guide.json",
        "config/analytics.yaml",
        "config/domain_kernel.yaml",
        "spec/analytics.schema.json",
    ):
        assert (ROOT / relative).read_bytes() == (ROOT / "hospes" / "resources" / relative).read_bytes(), relative

    index = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    workspace = (ROOT / "dashboard" / "assets" / "partnership-workspace.html").read_text(encoding="utf-8")
    javascript = (ROOT / "dashboard" / "assets" / "partnership-workspace.js").read_text(encoding="utf-8")
    api_javascript = (ROOT / "dashboard" / "assets" / "api.js").read_text(encoding="utf-8")
    capabilities = (ROOT / "dashboard" / "assets" / "capabilities.mjs").read_text(encoding="utf-8")
    views = (ROOT / "dashboard" / "assets" / "views.mjs").read_text(encoding="utf-8")

    # The Analytics view is reachable, and it renders metrics, trends, receipts,
    # and an export control.
    assert 'data-view="analytics"' in index
    assert 'id="analytics-view"' in workspace
    assert 'id="analytics-table"' in workspace
    assert 'id="analytics-trend-chart"' in workspace
    assert 'id="analytics-receipt-list"' in workspace
    assert 'id="btn-analytics-export"' in workspace
    assert 'id="analytics-window"' in workspace
    for column in ("Episode", "Downloads", "Retention", "Top clip views", "Providers"):
        assert f">{column}</th>" in workspace
    assert "'analytics'" in views
    assert "loadAnalyticsTrends" in api_javascript
    assert "loadAnalyticsReceipts" in api_javascript
    assert "/analytics/export.csv" in api_javascript
    assert "renderTrendChart" in javascript
    assert "escapeHTML(point.episode_id)" in javascript
    assert "canViewAnalytics" in capabilities
    assert "canExportAnalytics" in capabilities

    guide = json.loads((ROOT / "dashboard" / "assets" / "guide.json").read_text(encoding="utf-8"))
    assert {"canViewAnalytics", "canExportAnalytics"} <= set(guide["capabilities"])
    assert {"analytics-tab", "analytics-view", "analytics-trends", "analytics-export", "analytics-receipts"} <= set(
        guide["elements"]
    )
    assert any(beat["view"] == "analytics" for beat in guide["beats"])

    domain = (ROOT / "config" / "domain_kernel.yaml").read_text(encoding="utf-8")
    assert "analytics_contract:" in domain
    assert "scheduled_job_type: analytics.fetch" in domain
    assert "source_receipt_ref of the provider receipt that authorized the read" in domain
    assert "Analytics is a read organ" in domain

    documentation = (ROOT / "docs" / "analytics.md").read_text(encoding="utf-8")
    for phrase in (
        "spec/analytics.schema.json",
        "source_receipt_ref",
        "hospes fetch-analytics",
        "analytics.fetch",
        "operation=read",
        "unconfigured",
        "blocked",
        "Cache-Control: no-store",
        "counters are summed",
        "spreadsheet-formula prefixes",
    ):
        assert phrase in documentation, phrase
    assert "docs/analytics.md" in (ROOT / "README.md").read_text(encoding="utf-8")

    # The tracked policy declares an export path per provider and no secret.
    policy = analytics.load_policy(ROOT / "config" / "analytics.yaml")
    assert set(policy["providers"]) == analytics.SUPPORTED_PROVIDERS
    assert all("import_path" in entry for entry in policy["providers"].values())
    assert "op://" not in (ROOT / "config" / "analytics.yaml").read_text(encoding="utf-8")
