"""Provider-neutral adapter contracts and visible capability state.

Adapters are intentionally small.  Integrations can be added behind the same
contract without changing domain code, and missing credentials remain a
visible ``unconfigured`` status instead of silently disappearing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from . import configuration, generation, store


class ProviderError(RuntimeError):
    """Raised when a provider cannot satisfy a bounded adapter call."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


class ProviderAdapter(Protocol):
    capability: str
    name: str

    def verify(self) -> dict[str, Any]: ...

    def execute(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProviderResult:
    capability: str
    provider: str
    status: str
    receipt_ref: str
    details: Mapping[str, Any]


class ConfiguredAdapter:
    def __init__(self, item: configuration.ProviderConfig):
        self.capability = item.capability
        self.name = item.name
        self.mode = item.mode
        self.settings = dict(item.settings)

    def verify(self) -> dict[str, Any]:
        if self.mode not in {"manual", "fixture", "local"}:
            raise ProviderError(
                f"{self.name} has a credential reference but no authenticated live smoke receipt"
            )
        return {"status": "ready", "mode": self.mode, "provider": self.name}

    def execute(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.mode not in {"manual", "fixture", "local"}:
            raise ProviderError(
                f"{self.name} is configured but live execution is disabled until its credential wall is provisioned"
            )
        return {"status": "prepared", "operation": operation, "provider": self.name, "payload": dict(payload)}


class ProviderRegistry:
    def __init__(self, runtime: configuration.RuntimeConfig | None = None):
        self.runtime = runtime or configuration.load_runtime()

    def statuses(self) -> list[configuration.Capability]:
        return configuration.capability_report(self.runtime)

    def adapters(self, capability: str) -> list[ProviderAdapter]:
        return [ConfiguredAdapter(item) for item in self.runtime.providers.get(capability, ()) if item.enabled and item.configured]

    def choose(self, capability: str, preferred: list[str] | tuple[str, ...] | None = None) -> ProviderAdapter:
        order = tuple(preferred or self.runtime.failover_order.get(capability, ()))
        candidates = self.adapters(capability)
        if order:
            candidates.sort(key=lambda adapter: order.index(adapter.name) if adapter.name in order else len(order))
        if not candidates:
            raise ProviderError(f"no configured provider is available for capability {capability}")
        blocked: list[str] = []
        for candidate in candidates:
            try:
                result = candidate.verify()
            except ProviderError:
                blocked.append(candidate.name)
                continue
            if result.get("status") == "ready":
                return candidate
            blocked.append(candidate.name)
        detail = ", ".join(blocked) if blocked else "none"
        raise ProviderError(
            f"no ready provider is available for capability {capability}; blocked={detail}",
            503,
        )

    def verify(self, capability: str | None = None) -> list[ProviderResult]:
        selected = [capability] if capability else list(configuration.CAPABILITIES)
        results: list[ProviderResult] = []
        for name in selected:
            configured = self.adapters(name)
            if not configured:
                results.append(ProviderResult(name, "-", "unconfigured", "none", {"reason": "no configured provider"}))
                continue
            for adapter in configured:
                try:
                    details = adapter.verify()
                    status = "ready"
                except ProviderError as exc:
                    details = {"reason": str(exc)}
                    status = "blocked"
                results.append(ProviderResult(name, adapter.name, status, f"provider:{name}:{adapter.name}", details))
        return results

    def record_verification(
        self,
        conn: Any,
        *,
        tenant_id: str,
        show_id: str,
        capability: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        timestamp = (now or generation.now()).astimezone(timezone.utc).isoformat()
        written: list[dict[str, Any]] = []
        for result in self.verify(capability):
            row = {
                "id": generation.new_id("provider_receipt"),
                "tenant_id": tenant_id,
                "show_id": show_id,
                "capability": result.capability,
                "provider": result.provider,
                "status": result.status,
                "receipt_ref": result.receipt_ref,
                "details": dict(result.details),
                "created_at": timestamp,
            }
            store.insert(conn, "provider_receipts", row)
            written.append(row)
        conn.commit()
        return written


def require_human_authorization(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    action: str,
    subject_ref: str,
    idempotency_key: str,
    authorized_by: str,
    authorization_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the receipt required before any external publish/send action."""
    if not all(isinstance(value, str) and value.strip() for value in (action, subject_ref, idempotency_key, authorized_by, authorization_ref)):
        raise ProviderError("human authorization requires action, subject, actor, and opaque authorization reference")
    existing = store.fetch_one(
        conn,
        "SELECT * FROM authorization_receipts WHERE tenant_id = ? AND show_id = ? AND action = ? AND idempotency_key = ?",
        (tenant_id, show_id, action, idempotency_key),
    )
    if existing:
        if existing.get("subject_ref") != subject_ref:
            raise ProviderError(
                "authorization idempotency key is already bound to another subject",
                409,
            )
        return existing
    timestamp = (now or generation.now()).astimezone(timezone.utc).isoformat()
    row = {
        "id": generation.new_id("authorization_receipt"),
        "tenant_id": tenant_id,
        "show_id": show_id,
        "action": action,
        "subject_ref": subject_ref,
        "idempotency_key": idempotency_key,
        "authorized_by": authorized_by,
        "authorization_ref": authorization_ref,
        "created_at": timestamp,
    }
    store.insert(conn, "authorization_receipts", row)
    conn.commit()
    return row


__all__ = [
    "ConfiguredAdapter", "ProviderAdapter", "ProviderError", "ProviderRegistry",
    "ProviderResult", "require_human_authorization",
]
