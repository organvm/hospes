"""Validated, privacy-safe inputs for the universal Pilot execution kernel."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import privacy


_OPAQUE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,119}$")
_POLICY_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{1,79}$")
_ALLOWED_ROLES = {"producer", "editorial_owner", "relationship_owner"}
DECISION_KINDS = frozenset({
    "wait",
    "follow_up",
    "promote",
    "activate_set",
    "fallback_rehearsal",
    "pause",
})


class PilotError(ValueError):
    """A bounded Pilot request violated the domain contract."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise PilotError(422, f"{label} contains unsupported fields: {sorted(unknown)}")


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PilotError(422, f"{label} must be an object")
    return value


def _positive_int(value: Any, label: str, *, minimum: int = 1, maximum: int = 10_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PilotError(422, f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _bounded_string(value: Any, label: str, *, minimum: int = 2, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise PilotError(422, f"{label} must be a string")
    normalized = value.strip()
    if not minimum <= len(normalized) <= maximum:
        raise PilotError(422, f"{label} must be {minimum}-{maximum} characters")
    if privacy.private_text_kind(normalized):
        raise PilotError(422, f"{label} contains private data that must remain external")
    return normalized


def opaque_id(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise PilotError(422, f"{label} must be a string")
    normalized = value.strip()
    if (
        not 2 <= len(normalized) <= 120
        or not _OPAQUE_ID.fullmatch(normalized)
        or not re.search(r"[a-z_]", normalized)
    ):
        raise PilotError(422, f"{label} must be an opaque lowercase identifier")
    return normalized


def aware_datetime(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif not isinstance(value, str):
        raise PilotError(422, f"{label} must be an ISO timestamp")
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PilotError(422, f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PilotError(422, f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _privacy_scan(value: Any, label: str = "policy") -> None:
    if isinstance(value, str):
        if privacy.private_text_kind(value):
            raise PilotError(422, f"{label} contains private data that must remain external")
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if key.endswith("_id") and isinstance(nested, str):
                opaque_id(nested, f"{label}.{key}")
                continue
            _privacy_scan(nested, f"{label}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _privacy_scan(nested, f"{label}[{index}]")


@dataclass(frozen=True)
class PilotPolicyInput:
    policy_key: str
    policy_version: int
    deadline_at: datetime
    timezone_name: str
    candidate_count: int
    candidate_network_id: str
    candidate_city: str
    allowed_relationship_classes: tuple[str, ...]
    max_social_cost: int
    follow_up_limit: int
    initial_response_hours: int
    follow_up_response_hours: int
    production_gate_durations: dict[str, int]
    safety_reserve_hours: int
    human_authority_rules: dict[str, Any]
    relationship_exposure_budget: int
    rehearsal_lead_hours: int

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any]) -> "PilotPolicyInput":
        _reject_unknown(source, {"version", "show_id", "policy"}, "policy document")
        if source.get("version") != 1:
            raise PilotError(422, "policy document must declare version 1")
        policy = _required_mapping(source.get("policy"), "policy")
        _reject_unknown(
            policy,
            {
                "key",
                "version",
                "deadline_at",
                "timezone",
                "candidate_eligibility",
                "follow_up_limit",
                "response_windows",
                "production_gate_durations",
                "safety_reserve_hours",
                "human_authority_rules",
                "relationship_exposure_budget",
                "rehearsal_lead_hours",
            },
            "policy",
        )
        _privacy_scan(policy)
        policy_key = _bounded_string(policy.get("key"), "policy.key", maximum=80)
        if not _POLICY_KEY.fullmatch(policy_key):
            raise PilotError(422, "policy.key must be a stable lowercase key")
        policy_version = _positive_int(policy.get("version"), "policy.version")
        deadline_at = aware_datetime(policy.get("deadline_at"), "policy.deadline_at")
        timezone_name = _bounded_string(policy.get("timezone"), "policy.timezone", maximum=80)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise PilotError(422, "policy.timezone must name an IANA timezone") from exc

        eligibility = _required_mapping(
            policy.get("candidate_eligibility"), "policy.candidate_eligibility"
        )
        _reject_unknown(
            eligibility,
            {"count", "network_id", "city", "relationship_classes", "max_social_cost"},
            "policy.candidate_eligibility",
        )
        relationship_classes = eligibility.get("relationship_classes")
        if (
            not isinstance(relationship_classes, list)
            or not relationship_classes
            or any(item not in {"C0", "C1", "C2", "C3"} for item in relationship_classes)
        ):
            raise PilotError(422, "candidate relationship_classes must be a non-empty C0-C3 list")
        classes = tuple(dict.fromkeys(relationship_classes))

        response_windows = _required_mapping(
            policy.get("response_windows"), "policy.response_windows"
        )
        _reject_unknown(
            response_windows,
            {"initial_hours", "follow_up_hours"},
            "policy.response_windows",
        )
        durations_raw = _required_mapping(
            policy.get("production_gate_durations"), "policy.production_gate_durations"
        )
        required_durations = {"booking", "consent", "brief", "asset_preflight", "rehearsal"}
        _reject_unknown(durations_raw, required_durations, "policy.production_gate_durations")
        if set(durations_raw) != required_durations:
            raise PilotError(
                422,
                "production_gate_durations requires booking, consent, brief, asset_preflight, and rehearsal",
            )
        durations = {
            key: _positive_int(value, f"production_gate_durations.{key}")
            for key, value in durations_raw.items()
        }

        authority = _required_mapping(
            policy.get("human_authority_rules"), "policy.human_authority_rules"
        )
        _reject_unknown(
            authority,
            {
                "no_autonomous_sending",
                "multi_activation_requires_owner_approval",
                "decision_roles",
            },
            "policy.human_authority_rules",
        )
        roles = authority.get("decision_roles")
        if (
            authority.get("no_autonomous_sending") is not True
            or authority.get("multi_activation_requires_owner_approval") is not True
            or not isinstance(roles, list)
            or not roles
            or any(role not in _ALLOWED_ROLES for role in roles)
        ):
            raise PilotError(422, "human authority rules must preserve human-only sending and roles")
        authority_normalized = {
            "no_autonomous_sending": True,
            "multi_activation_requires_owner_approval": True,
            "decision_roles": list(dict.fromkeys(roles)),
        }

        max_social_cost = _positive_int(
            eligibility.get("max_social_cost"),
            "candidate_eligibility.max_social_cost",
            maximum=5,
        )
        exposure_budget = _positive_int(
            policy.get("relationship_exposure_budget"),
            "policy.relationship_exposure_budget",
            maximum=5,
        )
        return cls(
            policy_key=policy_key,
            policy_version=policy_version,
            deadline_at=deadline_at,
            timezone_name=timezone_name,
            candidate_count=_positive_int(
                eligibility.get("count"), "candidate_eligibility.count", maximum=20
            ),
            candidate_network_id=opaque_id(
                eligibility.get("network_id"), "candidate_eligibility.network_id"
            ),
            candidate_city=_bounded_string(
                eligibility.get("city"), "candidate_eligibility.city", maximum=80
            ),
            allowed_relationship_classes=classes,
            max_social_cost=max_social_cost,
            follow_up_limit=_positive_int(
                policy.get("follow_up_limit"),
                "policy.follow_up_limit",
                minimum=0,
                maximum=3,
            ),
            initial_response_hours=_positive_int(
                response_windows.get("initial_hours"), "response_windows.initial_hours"
            ),
            follow_up_response_hours=_positive_int(
                response_windows.get("follow_up_hours"), "response_windows.follow_up_hours"
            ),
            production_gate_durations=durations,
            safety_reserve_hours=_positive_int(
                policy.get("safety_reserve_hours"),
                "policy.safety_reserve_hours",
                minimum=0,
            ),
            human_authority_rules=authority_normalized,
            relationship_exposure_budget=exposure_budget,
            rehearsal_lead_hours=_positive_int(
                policy.get("rehearsal_lead_hours"), "policy.rehearsal_lead_hours"
            ),
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "policy_key": self.policy_key,
            "policy_version": self.policy_version,
            "deadline_at": self.deadline_at.isoformat(),
            "timezone": self.timezone_name,
            "candidate_count": self.candidate_count,
            "candidate_network_id": self.candidate_network_id,
            "candidate_city": self.candidate_city,
            "allowed_relationship_classes": list(self.allowed_relationship_classes),
            "max_social_cost": self.max_social_cost,
            "follow_up_limit": self.follow_up_limit,
            "initial_response_hours": self.initial_response_hours,
            "follow_up_response_hours": self.follow_up_response_hours,
            "production_gate_durations": self.production_gate_durations,
            "safety_reserve_hours": self.safety_reserve_hours,
            "human_authority_rules": self.human_authority_rules,
            "relationship_exposure_budget": self.relationship_exposure_budget,
            "rehearsal_lead_hours": self.rehearsal_lead_hours,
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PilotDecisionInput:
    decision_kind: str
    expected_revision: int
    payload: dict[str, Any]

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any]) -> "PilotDecisionInput":
        if not isinstance(source, Mapping):
            raise PilotError(422, "decision must be an object")
        kind = source.get("decision_kind")
        if kind not in DECISION_KINDS:
            raise PilotError(422, f"decision_kind must be one of {sorted(DECISION_KINDS)}")
        revision = _positive_int(source.get("expected_revision"), "expected_revision")
        common = {"decision_kind", "expected_revision"}
        payload: dict[str, Any] = {}
        if kind == "activate_set":
            _reject_unknown(source, common | {"assignments"}, "activate_set decision")
            assignments = source.get("assignments")
            if not isinstance(assignments, list) or not assignments:
                raise PilotError(422, "activate_set requires one or more assignments")
            normalized = []
            seen = set()
            for index, item in enumerate(assignments):
                assignment = _required_mapping(item, f"assignments[{index}]")
                _reject_unknown(
                    assignment, {"assignment_id", "not_before"}, f"assignments[{index}]"
                )
                assignment_id = opaque_id(
                    assignment.get("assignment_id"), f"assignments[{index}].assignment_id"
                )
                if assignment_id in seen:
                    raise PilotError(422, "activate_set assignment ids must be unique")
                seen.add(assignment_id)
                normalized.append({
                    "assignment_id": assignment_id,
                    "not_before": aware_datetime(
                        assignment.get("not_before"), f"assignments[{index}].not_before"
                    ).isoformat(),
                })
            payload["assignments"] = normalized
        elif kind == "follow_up":
            _reject_unknown(source, common | {"assignment_id"}, "follow_up decision")
            payload["assignment_id"] = opaque_id(source.get("assignment_id"), "assignment_id")
        elif kind == "promote":
            _reject_unknown(
                source,
                common | {"exhausted_assignment_id", "promoted_assignment_id", "not_before"},
                "promote decision",
            )
            payload = {
                "exhausted_assignment_id": opaque_id(
                    source.get("exhausted_assignment_id"), "exhausted_assignment_id"
                ),
                "promoted_assignment_id": opaque_id(
                    source.get("promoted_assignment_id"), "promoted_assignment_id"
                ),
                "not_before": aware_datetime(source.get("not_before"), "not_before").isoformat(),
            }
            if payload["exhausted_assignment_id"] == payload["promoted_assignment_id"]:
                raise PilotError(422, "promotion requires distinct assignments")
        else:
            _reject_unknown(source, common, f"{kind} decision")
        _privacy_scan(payload, "decision")
        return cls(decision_kind=str(kind), expected_revision=revision, payload=payload)


__all__ = [
    "DECISION_KINDS",
    "PilotDecisionInput",
    "PilotError",
    "PilotPolicyInput",
    "aware_datetime",
    "opaque_id",
]
