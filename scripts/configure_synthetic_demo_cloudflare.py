#!/usr/bin/env python3
"""Provision or verify the exact private Cloudflare boundary for the demo.

Inputs remain environment-only. The local receipt is schema-bound and redacted:
it proves two exact gates without storing identities, hostnames, URLs, tokens,
or Cloudflare resource identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.cloudflare.com/client/v4"
ACCESS_RECEIPT_SCHEMA = "hospes.cloudflare-access-receipt.v1"
MAX_PAGES = 20
PAGE_SIZE = 50
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HOST = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
TUNNEL_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ConfigurationError(RuntimeError):
    pass


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def canonical_runtime_dir() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "HOSPES"
        / "private_pilot"
        / "demo"
        / "cloudflare"
    )


def enforce_runtime_dir(raw: str, *, create: bool) -> Path:
    runtime = Path(raw).expanduser()
    canonical = canonical_runtime_dir()
    if runtime.resolve(strict=False) != canonical.resolve(strict=False):
        raise ConfigurationError(
            "HOSPES_DEMO_CLOUDFLARE_DIR must be the canonical private runtime"
        )
    if runtime.is_symlink() or any(
        parent.is_symlink()
        for parent in (runtime.parent, runtime.parent.parent)
        if parent.exists()
    ):
        raise ConfigurationError("Cloudflare private runtime cannot use symlinks")
    if create:
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not runtime.is_dir():
        raise ConfigurationError("Cloudflare private runtime is missing")
    runtime.chmod(0o700)
    return runtime


def _require_private_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label} must be a regular private file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigurationError(f"{label} permissions must be 0600 or stricter")


def _atomic_private_json(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = (
            json.dumps(document, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    path.chmod(0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Cloudflare:
    def __init__(self, token: str) -> None:
        self.token = token

    def _document(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{API_ROOT}{path}", data=body, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=30) as response:
                document = json.load(response)
        except HTTPError as exc:
            try:
                rejected = json.load(exc)
                errors = rejected.get("errors", [])
                detail = "; ".join(
                    str(item.get("message", "request rejected"))
                    for item in errors
                    if isinstance(item, dict)
                )
            except Exception:
                detail = f"HTTP {exc.code}"
            raise ConfigurationError(
                f"Cloudflare API request failed: {detail or f'HTTP {exc.code}'}"
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise ConfigurationError("Cloudflare API is unreachable") from exc
        if not isinstance(document, dict) or document.get("success") is not True:
            raise ConfigurationError("Cloudflare API did not confirm success")
        return document

    def call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._document(method, path, payload).get("result")

    def list(self, path: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        records: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            query = dict(params or {})
            query.update({"page": page, "per_page": PAGE_SIZE})
            separator = "&" if "?" in path else "?"
            document = self._document(
                "GET", f"{path}{separator}{urlencode(query)}"
            )
            result = document.get("result")
            if not isinstance(result, list):
                raise ConfigurationError("Cloudflare paginated result is malformed")
            records.extend(item for item in result if isinstance(item, dict))
            info = document.get("result_info") or {}
            total_pages = info.get("total_pages")
            if isinstance(total_pages, int):
                if total_pages > MAX_PAGES:
                    raise ConfigurationError(
                        "Cloudflare pagination exceeded the bounded page limit"
                    )
                if page >= total_pages:
                    return records
            elif len(result) < PAGE_SIZE:
                return records
        raise ConfigurationError("Cloudflare pagination did not terminate")


def _exact_policy_emails(policy: Mapping[str, Any]) -> set[str] | None:
    if (
        policy.get("decision") != "allow"
        or policy.get("precedence") != 1
        or policy.get("exclude") != []
        or policy.get("require") != []
    ):
        return None
    include = policy.get("include")
    if not isinstance(include, list) or not include:
        return None
    values: set[str] = set()
    for rule in include:
        if not isinstance(rule, dict) or set(rule) != {"email"}:
            return None
        email = rule["email"]
        if (
            not isinstance(email, dict)
            or set(email) != {"email"}
            or not isinstance(email["email"], str)
            or not EMAIL.fullmatch(email["email"])
        ):
            return None
        values.add(email["email"].lower())
    if len(values) != len(include):
        return None
    return values


def _application(
    client: Cloudflare,
    account_id: str,
    application_id: str,
) -> tuple[dict, list[dict]]:
    app = client.call(
        "GET", f"/accounts/{account_id}/access/apps/{application_id}"
    )
    if not isinstance(app, dict):
        raise ConfigurationError("Access application readback is malformed")
    policies = client.list(
        f"/accounts/{account_id}/access/apps/{application_id}/policies"
    )
    return app, policies


def ensure_application(
    client: Cloudflare,
    *,
    account_id: str,
    name: str,
    hostname: str,
    allowed_emails: set[str],
    verify_only: bool,
) -> tuple[dict, list[dict]]:
    applications = client.list(f"/accounts/{account_id}/access/apps")
    matches = [
        item
        for item in applications
        if item.get("name") == name or item.get("domain") == hostname
    ]
    if len(matches) > 1:
        raise ConfigurationError("multiple Access applications collide")
    if not matches:
        if verify_only:
            raise ConfigurationError("required Access application is missing")
        policy = {
            "name": f"{name} exact identities",
            "precedence": 1,
            "decision": "allow",
            "include": [
                {"email": {"email": email}} for email in sorted(allowed_emails)
            ],
            "exclude": [],
            "require": [],
        }
        created = client.call(
            "POST",
            f"/accounts/{account_id}/access/apps",
            {
                "name": name,
                "domain": hostname,
                "type": "self_hosted",
                "session_duration": "8h",
                "app_launcher_visible": False,
                "allow_authenticate_via_warp": False,
                "policies": [policy],
            },
        )
        if not isinstance(created, dict) or not created.get("id"):
            raise ConfigurationError(
                "Cloudflare did not return an Access application id"
            )
        matches = [created]
    application_id = str(matches[0].get("id", ""))
    if not OPAQUE_ID.fullmatch(application_id):
        raise ConfigurationError("Access application id is malformed")
    app, policies = _application(client, account_id, application_id)
    if (
        str(app.get("id", "")) != application_id
        or app.get("name") != name
        or app.get("type") != "self_hosted"
        or app.get("domain") != hostname
        or app.get("session_duration") != "8h"
        or app.get("app_launcher_visible") is not False
        or app.get("allow_authenticate_via_warp") is not False
        or len(policies) != 1
        or OPAQUE_ID.fullmatch(str(policies[0].get("id", ""))) is None
        or policies[0].get("name") != f"{name} exact identities"
        or _exact_policy_emails(policies[0]) != allowed_emails
    ):
        raise ConfigurationError("Access application or policy readback drifted")
    return app, policies


def ensure_dns(
    client: Cloudflare,
    *,
    zone_id: str,
    hostname: str,
    tunnel_id: str,
    verify_only: bool,
) -> dict:
    records = client.list(
        f"/zones/{zone_id}/dns_records",
        {"type": "CNAME", "name": hostname},
    )
    target = f"{tunnel_id}.cfargotunnel.com"
    if not records:
        if verify_only:
            raise ConfigurationError("required tunnel DNS record is missing")
        created = client.call(
            "POST",
            f"/zones/{zone_id}/dns_records",
            {
                "type": "CNAME",
                "name": hostname,
                "content": target,
                "ttl": 1,
                "proxied": True,
                "comment": "HOSPES synthetic demo; local operator only",
            },
        )
        if not isinstance(created, dict):
            raise ConfigurationError("Cloudflare did not return a DNS record")
        records = [created]
    if len(records) != 1:
        raise ConfigurationError("tunnel DNS readback is not unique")
    record = records[0]
    if (
        OPAQUE_ID.fullmatch(str(record.get("id", ""))) is None
        or record.get("type") != "CNAME"
        or record.get("name") != hostname
        or str(record.get("content", "")).rstrip(".") != target
        or record.get("ttl") != 1
        or record.get("proxied") is not True
    ):
        raise ConfigurationError("tunnel DNS readback drifted")
    return record


def desired_ingress(review_hostname: str, complete_hostname: str) -> dict:
    return {
        "config": {
            "ingress": [
                {
                    "hostname": review_hostname,
                    "service": "http://127.0.0.1:8765",
                },
                {
                    "hostname": complete_hostname,
                    "service": "http://127.0.0.1:8766",
                },
                {"service": "http_status:404"},
            ],
            "warp-routing": {"enabled": False},
        }
    }


def ensure_remote_ingress(
    client: Cloudflare,
    *,
    account_id: str,
    tunnel_id: str,
    expected: dict,
    verify_only: bool,
) -> dict:
    path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
    current = client.call("GET", path)
    if not isinstance(current, dict) or current.get("config") != expected["config"]:
        if verify_only:
            raise ConfigurationError("remote tunnel ingress readback drifted")
        client.call("PUT", path, expected)
        current = client.call("GET", path)
    if not isinstance(current, dict) or current.get("config") != expected["config"]:
        raise ConfigurationError("remote tunnel ingress readback drifted")
    return current


def verify_foundation(
    client: Cloudflare,
    *,
    account_id: str,
    zone_id: str,
    zone_name: str,
    tunnel_id: str,
    tunnel_name: str,
) -> dict[str, Any]:
    token = client.call("GET", "/user/tokens/verify")
    account = client.call("GET", f"/accounts/{account_id}")
    zone = client.call("GET", f"/zones/{zone_id}")
    tunnel = client.call(
        "GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}"
    )
    providers = client.list(
        f"/accounts/{account_id}/access/identity_providers"
    )
    otp = [item for item in providers if item.get("type") == "onetimepin"]
    if not isinstance(token, dict) or token.get("status") != "active":
        raise ConfigurationError("Cloudflare API token is not active")
    if not isinstance(account, dict) or str(account.get("id")) != account_id:
        raise ConfigurationError("Cloudflare account readback drifted")
    if (
        not isinstance(zone, dict)
        or str(zone.get("id")) != zone_id
        or zone.get("name") != zone_name
        or str(zone.get("account", {}).get("id", account_id)) != account_id
    ):
        raise ConfigurationError("Cloudflare zone/account readback drifted")
    if (
        not isinstance(tunnel, dict)
        or str(tunnel.get("id")) != tunnel_id
        or tunnel.get("name") != tunnel_name
        or tunnel.get("deleted_at") not in (None, "")
        or str(tunnel.get("account_tag", account_id)) != account_id
    ):
        raise ConfigurationError("Cloudflare tunnel/account readback drifted")
    if (
        len(otp) != 1
        or OPAQUE_ID.fullmatch(str(otp[0].get("id", ""))) is None
    ):
        raise ConfigurationError(
            "Cloudflare Access requires exactly one One-time PIN provider"
        )
    return {
        "account": account,
        "zone": zone,
        "tunnel": tunnel,
        "otp": otp[0],
    }


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_local_config(
    runtime_dir: Path,
    *,
    tunnel_id: str,
    review_hostname: str,
    complete_hostname: str,
) -> None:
    credentials = runtime_dir / "tunnel-credentials.json"
    _require_private_file(credentials, "private tunnel credentials")
    try:
        value = json.loads(credentials.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("private tunnel credentials are malformed") from exc
    if not isinstance(value, dict) or str(value.get("TunnelID")) != tunnel_id:
        raise ConfigurationError("private tunnel credentials do not match the tunnel")
    config = runtime_dir / "config.yml"
    if config.is_symlink():
        raise ConfigurationError("private tunnel config cannot be a symlink")
    temporary = config.with_name(".config.yml.tmp")
    temporary.write_text(
        "\n".join(
            (
                f"tunnel: {tunnel_id}",
                f"credentials-file: {credentials}",
                "no-autoupdate: true",
                "metrics: 127.0.0.1:8767",
                "ingress:",
                f"  - hostname: {review_hostname}",
                "    service: http://127.0.0.1:8765",
                f"  - hostname: {complete_hostname}",
                "    service: http://127.0.0.1:8766",
                "  - service: http_status:404",
                "",
            )
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, config)
    config.chmod(0o600)


def exact_state(
    *,
    foundation: Mapping[str, Any],
    applications: list[tuple[dict, list[dict]]],
    dns_records: list[dict],
    ingress: dict,
) -> dict[str, Any]:
    return {
        "account_id": foundation["account"]["id"],
        "zone_id": foundation["zone"]["id"],
        "tunnel_id": foundation["tunnel"]["id"],
        "tunnel_name": foundation["tunnel"]["name"],
        "otp_id": foundation["otp"]["id"],
        "applications": [
            {
                "id": application["id"],
                "name": application["name"],
                "type": application["type"],
                "domain": application["domain"],
                "session_duration": application["session_duration"],
                "app_launcher_visible": application["app_launcher_visible"],
                "allow_authenticate_via_warp": application[
                    "allow_authenticate_via_warp"
                ],
                "policy": policies,
            }
            for application, policies in applications
        ],
        "dns": [
            {
                "id": record["id"],
                "type": record["type"],
                "name": record["name"],
                "content": record["content"],
                "ttl": record["ttl"],
                "proxied": record["proxied"],
            }
            for record in dns_records
        ],
        "ingress": ingress["config"],
    }


def redacted_receipt(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": ACCESS_RECEIPT_SCHEMA,
        "verified_at": datetime.now(UTC).isoformat(),
        "generation_fingerprint": _fingerprint(state),
        "gates": {
            "account_tunnel_otp": True,
            "applications_policies_dns_ingress": True,
        },
        "resource_counts": {
            "applications": 2,
            "policies": 2,
            "dns_records": 2,
            "ingress_rules": 3,
        },
        "default_deny": True,
        "exact_identity_count": 2,
    }


def verify_receipt(path: Path, expected: Mapping[str, Any]) -> None:
    _require_private_file(path, "Access receipt")
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Access receipt is malformed") from exc
    stable_actual = dict(actual) if isinstance(actual, dict) else {}
    stable_expected = dict(expected)
    stable_actual.pop("verified_at", None)
    stable_expected.pop("verified_at", None)
    if stable_actual != stable_expected:
        raise ConfigurationError("Access receipt is stale or schema-invalid")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="perform exact readback and reject drift without mutating Cloudflare",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        token = required("CLOUDFLARE_API_TOKEN")
        account_id = required("CLOUDFLARE_ACCOUNT_ID")
        zone_id = required("CLOUDFLARE_ZONE_ID")
        zone = required("HOSPES_DEMO_ZONE").lower().rstrip(".")
        owner_email = required("HOSPES_DEMO_OWNER_EMAIL").lower()
        ari_email = required("HOSPES_DEMO_ARI_EMAIL").lower()
        tunnel_id = required("HOSPES_DEMO_TUNNEL_ID").lower()
        tunnel_name = required("HOSPES_DEMO_TUNNEL_NAME")
        runtime_dir = enforce_runtime_dir(
            required("HOSPES_DEMO_CLOUDFLARE_DIR"),
            create=not args.verify_only,
        )
        if not HOST.fullmatch(zone):
            raise ConfigurationError("HOSPES_DEMO_ZONE must be an existing DNS zone")
        if not EMAIL.fullmatch(owner_email) or not EMAIL.fullmatch(ari_email):
            raise ConfigurationError("both demo identities must be exact email addresses")
        if owner_email == ari_email:
            raise ConfigurationError("owner and Ari identities must be distinct")
        if not TUNNEL_ID.fullmatch(tunnel_id):
            raise ConfigurationError("HOSPES_DEMO_TUNNEL_ID must be a tunnel UUID")
        if not OPAQUE_ID.fullmatch(account_id) or not OPAQUE_ID.fullmatch(zone_id):
            raise ConfigurationError("Cloudflare account and zone ids are malformed")

        client = Cloudflare(token)
        foundation = verify_foundation(
            client,
            account_id=account_id,
            zone_id=zone_id,
            zone_name=zone,
            tunnel_id=tunnel_id,
            tunnel_name=tunnel_name,
        )
        allowed_emails = {owner_email, ari_email}
        hostnames = (f"ari-review.{zone}", f"ari-complete.{zone}")
        applications = [
            ensure_application(
                client,
                account_id=account_id,
                name=f"HOSPES synthetic demo - {scenario}",
                hostname=hostname,
                allowed_emails=allowed_emails,
                verify_only=args.verify_only,
            )
            for scenario, hostname in zip(("review", "complete"), hostnames)
        ]
        dns_records = [
            ensure_dns(
                client,
                zone_id=zone_id,
                hostname=hostname,
                tunnel_id=tunnel_id,
                verify_only=args.verify_only,
            )
            for hostname in hostnames
        ]
        expected_ingress = desired_ingress(*hostnames)
        ingress = ensure_remote_ingress(
            client,
            account_id=account_id,
            tunnel_id=tunnel_id,
            expected=expected_ingress,
            verify_only=args.verify_only,
        )
        state = exact_state(
            foundation=foundation,
            applications=applications,
            dns_records=dns_records,
            ingress=ingress,
        )
        receipt = redacted_receipt(state)
        receipt_path = runtime_dir / "access-ready.json"
        if args.verify_only:
            verify_receipt(receipt_path, receipt)
        else:
            write_local_config(
                runtime_dir,
                tunnel_id=tunnel_id,
                review_hostname=hostnames[0],
                complete_hostname=hostnames[1],
            )
            _atomic_private_json(receipt_path, receipt)
        print(
            json.dumps(
                {
                    "verified": True,
                    "verify_only": bool(args.verify_only),
                    "receipt_schema": ACCESS_RECEIPT_SCHEMA,
                },
                sort_keys=True,
            )
        )
        return 0
    except ConfigurationError as exc:
        print(f"[configure-cloudflare] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
