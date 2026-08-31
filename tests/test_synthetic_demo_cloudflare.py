"""Exact readback, pagination, and redacted receipt tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "configure_synthetic_demo_cloudflare.py"
)
SPEC = importlib.util.spec_from_file_location("synthetic_cloudflare", SCRIPT)
assert SPEC and SPEC.loader
cloudflare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloudflare)


class ExactClient:
    def __init__(self) -> None:
        self.values = {
            "/user/tokens/verify": {"status": "active"},
            "/accounts/account_fixture": {"id": "account_fixture"},
            "/zones/zone_fixture": {
                "id": "zone_fixture",
                "name": "example.test",
                "account": {"id": "account_fixture"},
            },
            "/accounts/account_fixture/cfd_tunnel/11111111-1111-4111-8111-111111111111": {
                "id": "11111111-1111-4111-8111-111111111111",
                "name": "hospes-ari-synthetic-demo",
                "account_tag": "account_fixture",
                "deleted_at": None,
            },
        }
        self.lists = {
            "/accounts/account_fixture/access/identity_providers": [
                {"id": "otp_fixture", "type": "onetimepin"}
            ],
        }

    def call(self, method: str, path: str, payload=None):
        assert method == "GET"
        return self.values[path]

    def list(self, path: str, params=None):
        return self.lists[path]


def test_bounded_pagination_collects_all_pages_and_rejects_overrun() -> None:
    client = cloudflare.Cloudflare("fixture-token")
    documents = [
        {
            "success": True,
            "result": [{"id": "one"}],
            "result_info": {"total_pages": 2},
        },
        {
            "success": True,
            "result": [{"id": "two"}],
            "result_info": {"total_pages": 2},
        },
    ]
    client._document = lambda *_args, **_kwargs: documents.pop(0)
    assert client.list("/fixture") == [{"id": "one"}, {"id": "two"}]

    client._document = lambda *_args, **_kwargs: {
        "success": True,
        "result": [],
        "result_info": {"total_pages": cloudflare.MAX_PAGES + 1},
    }
    with pytest.raises(cloudflare.ConfigurationError, match="bounded page limit"):
        client.list("/fixture")


def test_foundation_requires_exact_account_tunnel_and_otp_readback() -> None:
    client = ExactClient()
    result = cloudflare.verify_foundation(
        client,
        account_id="account_fixture",
        zone_id="zone_fixture",
        zone_name="example.test",
        tunnel_id="11111111-1111-4111-8111-111111111111",
        tunnel_name="hospes-ari-synthetic-demo",
    )
    assert result["otp"]["type"] == "onetimepin"
    assert set(result) == {"account", "zone", "tunnel", "otp"}

    tunnel_path = (
        "/accounts/account_fixture/cfd_tunnel/"
        "11111111-1111-4111-8111-111111111111"
    )
    client.values[tunnel_path]["name"] = "drifted-tunnel"
    with pytest.raises(cloudflare.ConfigurationError, match="tunnel/account"):
        cloudflare.verify_foundation(
            client,
            account_id="account_fixture",
            zone_id="zone_fixture",
            zone_name="example.test",
            tunnel_id="11111111-1111-4111-8111-111111111111",
            tunnel_name="hospes-ari-synthetic-demo",
        )
    client.values[tunnel_path]["name"] = "hospes-ari-synthetic-demo"
    client.lists["/accounts/account_fixture/access/identity_providers"] = []
    with pytest.raises(cloudflare.ConfigurationError, match="One-time PIN"):
        cloudflare.verify_foundation(
            client,
            account_id="account_fixture",
            zone_id="zone_fixture",
            zone_name="example.test",
            tunnel_id="11111111-1111-4111-8111-111111111111",
            tunnel_name="hospes-ari-synthetic-demo",
        )


def test_application_and_policy_readback_rejects_drift_and_unknown_rules() -> None:
    app_path = "/accounts/account_fixture/access/apps/app_fixture"
    policy_path = f"{app_path}/policies"

    class ApplicationClient:
        def __init__(self) -> None:
            self.application = {
                "id": "app_fixture",
                "name": "HOSPES synthetic demo - review",
                "type": "self_hosted",
                "domain": "review.example.test",
                "session_duration": "8h",
                "app_launcher_visible": False,
                "allow_authenticate_via_warp": False,
            }
            self.policies = [
                {
                    "id": "policy_fixture",
                    "name": "HOSPES synthetic demo - review exact identities",
                    "precedence": 1,
                    "decision": "allow",
                    "include": [
                        {"email": {"email": "ari@example.test"}},
                        {"email": {"email": "owner@example.test"}},
                    ],
                    "exclude": [],
                    "require": [],
                }
            ]

        def call(self, method, path, payload=None):
            assert method == "GET" and path == app_path and payload is None
            return self.application

        def list(self, path, params=None):
            if path == "/accounts/account_fixture/access/apps":
                return [self.application]
            assert path == policy_path and params is None
            return self.policies

    client = ApplicationClient()
    result = cloudflare.ensure_application(
        client,
        account_id="account_fixture",
        name="HOSPES synthetic demo - review",
        hostname="review.example.test",
        allowed_emails={"ari@example.test", "owner@example.test"},
        verify_only=True,
    )
    assert result[0]["domain"] == "review.example.test"

    client.policies[0]["include"].append(
        {"ip": {"ip": "192.0.2.0/24"}}
    )
    with pytest.raises(cloudflare.ConfigurationError, match="policy readback drifted"):
        cloudflare.ensure_application(
            client,
            account_id="account_fixture",
            name="HOSPES synthetic demo - review",
            hostname="review.example.test",
            allowed_emails={"ari@example.test", "owner@example.test"},
            verify_only=True,
        )

    client.policies[0]["include"].pop()
    client.application["session_duration"] = "24h"
    with pytest.raises(cloudflare.ConfigurationError, match="policy readback drifted"):
        cloudflare.ensure_application(
            client,
            account_id="account_fixture",
            name="HOSPES synthetic demo - review",
            hostname="review.example.test",
            allowed_emails={"ari@example.test", "owner@example.test"},
            verify_only=True,
        )


def test_dns_readback_rejects_content_proxy_and_uniqueness_drift() -> None:
    class DnsClient:
        def __init__(self, records):
            self.records = records
            self.calls = []

        def list(self, path, params=None):
            self.calls.append(("list", path, params))
            return self.records

        def call(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            raise AssertionError("verify-only DNS must never mutate")

    exact_record = {
        "id": "dns_fixture",
        "type": "CNAME",
        "name": "review.example.test",
        "content": "11111111-1111-4111-8111-111111111111.cfargotunnel.com",
        "ttl": 1,
        "proxied": True,
    }
    exact = DnsClient([exact_record])
    assert cloudflare.ensure_dns(
        exact,
        zone_id="zone_fixture",
        hostname="review.example.test",
        tunnel_id="11111111-1111-4111-8111-111111111111",
        verify_only=True,
    ) == exact_record

    for records in (
        [{**exact_record, "content": "wrong.cfargotunnel.com"}],
        [{**exact_record, "ttl": 300}],
        [{**exact_record, "proxied": False}],
        [exact_record, {**exact_record, "id": "dns_duplicate"}],
    ):
        with pytest.raises(cloudflare.ConfigurationError, match="DNS readback"):
            cloudflare.ensure_dns(
                DnsClient(records),
                zone_id="zone_fixture",
                hostname="review.example.test",
                tunnel_id="11111111-1111-4111-8111-111111111111",
                verify_only=True,
            )


def test_access_receipt_is_redacted_schema_bound_and_rejects_staleness(
    tmp_path: Path,
) -> None:
    state = {
        "account_id": "private-account",
        "zone_id": "private-zone",
        "tunnel_id": "private-tunnel",
        "otp_id": "private-otp",
        "applications": [
            {"id": "app-one", "domain": "one.private.test"},
            {"id": "app-two", "domain": "two.private.test"},
        ],
        "dns": [],
        "ingress": {},
    }
    receipt = cloudflare.redacted_receipt(state)
    serialized = json.dumps(receipt)
    assert receipt["schema"] == cloudflare.ACCESS_RECEIPT_SCHEMA
    assert receipt["gates"] == {
        "account_tunnel_otp": True,
        "applications_policies_dns_ingress": True,
    }
    for private_value in (
        "private-account",
        "private-zone",
        "private-tunnel",
        "private-otp",
        "one.private.test",
        "two.private.test",
    ):
        assert private_value not in serialized

    path = tmp_path / "access-ready.json"
    cloudflare._atomic_private_json(path, receipt)
    cloudflare.verify_receipt(path, cloudflare.redacted_receipt(state))
    drifted = cloudflare.redacted_receipt({**state, "ingress": {"drift": True}})
    with pytest.raises(cloudflare.ConfigurationError, match="stale"):
        cloudflare.verify_receipt(path, drifted)


def test_verify_only_ingress_never_mutates_and_detects_drift() -> None:
    class IngressClient:
        def __init__(self, current):
            self.current = current
            self.methods = []

        def call(self, method, _path, payload=None):
            self.methods.append(method)
            if method == "PUT":
                self.current = payload
            return self.current

    expected = cloudflare.desired_ingress(
        "review.example.test", "complete.example.test"
    )
    exact = IngressClient(expected)
    assert cloudflare.ensure_remote_ingress(
        exact,
        account_id="account_fixture",
        tunnel_id="11111111-1111-4111-8111-111111111111",
        expected=expected,
        verify_only=True,
    ) == expected
    assert exact.methods == ["GET"]

    drifted = IngressClient({"config": {"ingress": []}})
    with pytest.raises(cloudflare.ConfigurationError, match="drifted"):
        cloudflare.ensure_remote_ingress(
            drifted,
            account_id="account_fixture",
            tunnel_id="11111111-1111-4111-8111-111111111111",
            expected=expected,
            verify_only=True,
        )
    assert drifted.methods == ["GET"]
