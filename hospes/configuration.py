"""Typed, additive runtime and show configuration.

Configuration is deliberately data-driven.  A provider may be enabled without
being configured; the capability report keeps that distinction visible so an
operator can repair credentials without changing code.  Secret values never
belong in these files: providers reference a credential-wall key by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .paths import CONFIG_DIR, DNA_DIR


RUNTIME_PROFILES = frozenset({"local", "tunnel", "hosted", "hybrid"})
CAPABILITIES = (
    "calendar",
    "tour_intelligence",
    "analytics",
    "research",
    "distribution",
    "asset_amplifier",
    "mail_drafting",
)
SECRET_KEYS = frozenset({"api_key", "token", "password", "secret", "private_key", "client_secret"})
PROVIDER_MODES = frozenset({"manual", "fixture", "local", "live"})
CREDENTIAL_REF = re.compile(r"^(?:credential|op)://[^\s]{3,296}$")
CONFIG_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62})$")
CUSTODY_SENSITIVE_CATEGORIES = frozenset(
    {
        "contact",
        "correspondence",
        "consent",
        "financial",
        "private_relationship",
        "research",
        "artifact_metadata",
    }
)


class ConfigurationError(ValueError):
    """Raised when a configuration file violates the typed contract."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    enabled: bool
    storage: str
    authentication: str
    custody: str
    description: str


@dataclass(frozen=True)
class ProviderConfig:
    capability: str
    name: str
    enabled: bool
    credential_ref: str | None
    mode: str
    settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.credential_ref is not None or self.mode in {"manual", "fixture", "local"}


@dataclass(frozen=True)
class ArtifactCustodyConfig:
    private_records: str
    public_artifacts: str
    master_key_credential_ref: str
    local_root: str
    object_encryption: str
    aad_scope: str
    sensitive_categories: frozenset[str]
    outbound_requires_authorization_receipt: bool


@dataclass(frozen=True)
class AuthenticationConfig:
    local_identity: str
    access_application_ref: str
    access_team_domain_ref: str
    origin_jwt_audience_ref: str
    csrf_signing_key_ref: str
    assertion_header: str
    cookie_fallback: str
    identity_mapping: str
    csrf: str
    deny_by_default: bool


@dataclass(frozen=True)
class JobsConfig:
    queue: str
    internal_trigger_credential_ref: str
    policy_version: str
    scheduled_operations: tuple[str, ...]
    scheduled_namespaces: tuple[str, ...]
    default_lease_seconds: int
    max_timeout_seconds: int
    max_attempts: int
    max_cost_limit_minor: int
    scheduled_outbound: bool


@dataclass(frozen=True)
class RuntimeConfig:
    version: int
    active_profile: str
    profiles: Mapping[str, RuntimeProfile]
    providers: Mapping[str, tuple[ProviderConfig, ...]]
    failover_order: Mapping[str, tuple[str, ...]]
    feature_defaults: Mapping[str, Any]
    artifact_custody: ArtifactCustodyConfig
    authentication: AuthenticationConfig
    jobs: JobsConfig
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ShowConfig:
    tenant_id: str
    show_id: str
    label: str
    enabled: bool
    guest_interaction_mode: str
    outbound_mode: str
    provider_precedence: Mapping[str, tuple[str, ...]]
    roles: tuple[str, ...]
    dna_ref: str | None
    voice_ref: str | None
    template_ref: str | None
    brand_ref: str | None
    pilot_policy_ref: str | None
    display_order: int
    default: bool
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class SponsorInventoryPolicy:
    """The configurable sponsor/ad-inventory contract for one show.

    ``block_publication_on_unfilled_committed_slots`` is the acceptance
    criterion "Episode blocks PUBLISHED if committed slots unfilled
    (configurable)": the default protects a promised advertiser, and a show that
    sells no committed inventory can turn it off in its own configuration.
    """

    currency: str
    slot_types: tuple[str, ...]
    block_publication_on_unfilled_committed_slots: bool
    require_approved_claims_before_publication: bool
    source: str


@dataclass(frozen=True)
class Capability:
    capability: str
    provider: str
    status: str
    reason: str
    credential_ref: str | None = None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path} must contain a YAML object")
    return value


def _show_config_path(show_id: str) -> Path:
    """Return one direct child of the configured shows directory.

    ``show_id`` is used by API routes, so it must never become an arbitrary
    filesystem path. Resolving both the root and candidate also rejects a
    configured filename that is a symlink outside the resource tree.
    """
    if not isinstance(show_id, str) or not re.fullmatch(r"^[a-z0-9](?:[a-z0-9_-]{0,62})$", show_id):
        raise ConfigurationError("show_id must be a safe configuration identifier")
    root = (CONFIG_DIR / "shows").resolve()
    for configured_path in root.glob("*.yaml"):
        if configured_path.stem != show_id:
            continue
        candidate = configured_path.resolve()
        if candidate.parent != root:
            raise ConfigurationError("show configuration escapes the configured resource directory")
        return candidate
    raise ConfigurationError(f"show configuration {show_id!r} is not registered")


_SHOW_RESOURCE_ROOTS = {
    "dna": DNA_DIR,
    "voice": CONFIG_DIR / "voices",
    "template": CONFIG_DIR / "templates",
    "brand": CONFIG_DIR / "brands",
    "pilot_policy": CONFIG_DIR / "pilot_policies",
}


def show_resource_path(reference: str, kind: str) -> Path:
    """Resolve one declared show resource without allowing path traversal."""
    root = _SHOW_RESOURCE_ROOTS.get(kind)
    if root is None:
        raise ConfigurationError(f"unsupported show resource kind {kind!r}")
    prefix = "dna/" if kind == "dna" else f"config/{root.name}/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise ConfigurationError(f"{kind}_ref must be a repository-relative {prefix} YAML path")
    relative = reference[len(prefix) :]
    if not relative or Path(relative).suffix not in {".yaml", ".yml"}:
        raise ConfigurationError(f"{kind}_ref must name a YAML resource")
    resolved_root = root.resolve()
    unresolved = root / relative
    if unresolved.is_symlink():
        raise ConfigurationError(f"{kind}_ref cannot use a symbolic link")
    candidate = unresolved.resolve()
    if candidate.parent != resolved_root or not candidate.is_file():
        raise ConfigurationError(f"{kind}_ref is not a registered show resource")
    return candidate


def load_show_resource(show: ShowConfig, kind: str) -> dict[str, Any]:
    """Load one show-owned resource after validating its declared custody."""
    reference = getattr(show, f"{kind}_ref", None)
    if not isinstance(reference, str):
        raise ConfigurationError(f"show {show.show_id!r} has no {kind} resource")
    value = _read_yaml(show_resource_path(reference, kind))
    if value.get("version") != 1 or str(value.get("show_id")) != show.show_id:
        raise ConfigurationError(f"{kind} resource must declare version 1 and the owning show_id")
    return value


def _assert_no_secrets(value: Any, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text.endswith("credential_ref") and item not in (None, ""):
                if not isinstance(item, str) or not CREDENTIAL_REF.fullmatch(item):
                    raise ConfigurationError(f"{path}.{key} must be an opaque credential-wall reference")
            if key_text in SECRET_KEYS:
                if item not in (None, "", False) and not str(key).endswith("_ref"):
                    raise ConfigurationError(f"{path}.{key} stores a secret value; use credential_ref")
            _assert_no_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_secrets(item, f"{path}[{index}]")


def assert_no_secrets(value: Any, path: str = "config") -> None:
    """Public entry point for the shared configuration secret-shape guard.

    Callers that validate configuration outside the engine's own resource tree
    (the onboarding wizard validates a workspace at an arbitrary root) need the
    same rule the loaders apply, not a second copy of it.
    """
    _assert_no_secrets(value, path)


def load_runtime(path: str | Path | None = None) -> RuntimeConfig:
    selected = Path(path) if path else CONFIG_DIR / "runtime.yaml"
    raw = _read_yaml(selected)
    _assert_no_secrets(raw)
    version = raw.get("version")
    runtime = raw.get("runtime")
    if not isinstance(version, int) or version < 1 or not isinstance(runtime, Mapping):
        raise ConfigurationError("runtime.yaml requires integer version and runtime object")
    active = str(runtime.get("active_profile", "local"))
    profiles_raw = runtime.get("profiles")
    if not isinstance(profiles_raw, Mapping):
        raise ConfigurationError("runtime.profiles must be an object")
    profiles: dict[str, RuntimeProfile] = {}
    for name in RUNTIME_PROFILES:
        item = profiles_raw.get(name)
        if not isinstance(item, Mapping):
            raise ConfigurationError(f"runtime profile {name!r} is missing")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"runtime profile {name!r} enabled must be a boolean")
        profiles[name] = RuntimeProfile(
            name=name,
            enabled=enabled,
            storage=str(item.get("storage", "sqlite")),
            authentication=str(item.get("authentication", "local")),
            custody=str(item.get("custody", "local")),
            description=str(item.get("description", "")),
        )
    if active not in RUNTIME_PROFILES or not profiles[active].enabled:
        raise ConfigurationError(f"active runtime profile {active!r} is not enabled")
    providers_raw = runtime.get("providers", {})
    if not isinstance(providers_raw, Mapping):
        raise ConfigurationError("runtime.providers must be an object")
    providers: dict[str, tuple[ProviderConfig, ...]] = {}
    for capability in CAPABILITIES:
        items = providers_raw.get(capability, [])
        if not isinstance(items, list):
            raise ConfigurationError(f"provider registry {capability!r} must be a list")
        parsed: list[ProviderConfig] = []
        for item in items:
            if not isinstance(item, Mapping) or not item.get("name"):
                raise ConfigurationError(f"{capability} providers require name")
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ConfigurationError(f"{capability} provider {item.get('name')!r} enabled must be a boolean")
            mode = str(item.get("mode", "live"))
            if mode not in PROVIDER_MODES:
                raise ConfigurationError(f"{capability} provider {item.get('name')!r} has unsupported mode {mode!r}")
            parsed.append(
                ProviderConfig(
                    capability=capability,
                    name=str(item["name"]),
                    enabled=enabled,
                    credential_ref=str(item["credential_ref"]) if item.get("credential_ref") else None,
                    mode=mode,
                    settings=dict(item.get("settings", {})) if isinstance(item.get("settings", {}), Mapping) else {},
                )
            )
        providers[capability] = tuple(parsed)
    failover_raw = runtime.get("failover_order", {})
    failover = (
        {str(key): tuple(str(item) for item in value) for key, value in failover_raw.items() if isinstance(value, list)}
        if isinstance(failover_raw, Mapping)
        else {}
    )
    defaults = runtime.get("feature_defaults", {})
    custody_raw = runtime.get("artifact_custody")
    if not isinstance(custody_raw, Mapping):
        raise ConfigurationError("runtime.artifact_custody must be an object")
    credential_ref = custody_raw.get("master_key_credential_ref")
    if not isinstance(credential_ref, str) or not CREDENTIAL_REF.fullmatch(credential_ref):
        raise ConfigurationError(
            "runtime.artifact_custody.master_key_credential_ref must be an opaque credential-wall reference"
        )
    local_root = custody_raw.get("local_root")
    if not isinstance(local_root, str):
        raise ConfigurationError("runtime.artifact_custody.local_root must be a relative path")
    local_path = Path(local_root)
    if not local_root.strip() or local_path.is_absolute() or ".." in local_path.parts:
        raise ConfigurationError("runtime.artifact_custody.local_root must be a safe relative path")
    categories_raw = custody_raw.get("sensitive_categories")
    if not isinstance(categories_raw, list) or not all(isinstance(item, str) for item in categories_raw):
        raise ConfigurationError("runtime.artifact_custody.sensitive_categories must be a list of names")
    categories = frozenset(categories_raw)
    if len(categories) != len(categories_raw) or categories != CUSTODY_SENSITIVE_CATEGORIES:
        raise ConfigurationError(
            "runtime.artifact_custody.sensitive_categories must contain every required private category exactly once"
        )
    outbound_gate = custody_raw.get("outbound_requires_authorization_receipt")
    if not isinstance(outbound_gate, bool) or not outbound_gate:
        raise ConfigurationError("runtime.artifact_custody outbound authorization gate must remain enabled")
    exact_custody_values = {
        "private_records": "aes_256_gcm_tenant_envelope",
        "public_artifacts": "opaque_receipt_references_only",
        "object_encryption": "client_side_before_upload",
        "aad_scope": "tenant_show_table_record_field_key_version",
    }
    for key, expected in exact_custody_values.items():
        if custody_raw.get(key) != expected:
            raise ConfigurationError(f"runtime.artifact_custody.{key} must be {expected!r}")
    artifact_custody = ArtifactCustodyConfig(
        private_records=exact_custody_values["private_records"],
        public_artifacts=exact_custody_values["public_artifacts"],
        master_key_credential_ref=credential_ref,
        local_root=local_root,
        object_encryption=exact_custody_values["object_encryption"],
        aad_scope=exact_custody_values["aad_scope"],
        sensitive_categories=categories,
        outbound_requires_authorization_receipt=outbound_gate,
    )
    authentication_raw = runtime.get("authentication")
    if not isinstance(authentication_raw, Mapping):
        raise ConfigurationError("runtime.authentication must be an object")
    authentication_values = {
        "local_identity": "process_bound",
        "assertion_header": "Cf-Access-Jwt-Assertion",
        "cookie_fallback": "CF_Authorization",
        "identity_mapping": "database_subject_hash",
        "csrf": "double_submit_hmac",
    }
    for key, expected in authentication_values.items():
        if authentication_raw.get(key) != expected:
            raise ConfigurationError(f"runtime.authentication.{key} must be {expected!r}")
    authentication_refs: dict[str, str] = {}
    for key in (
        "access_application_ref",
        "access_team_domain_ref",
        "origin_jwt_audience_ref",
        "csrf_signing_key_ref",
    ):
        value = authentication_raw.get(key)
        if not isinstance(value, str) or not CREDENTIAL_REF.fullmatch(value):
            raise ConfigurationError(f"runtime.authentication.{key} must be an opaque credential-wall reference")
        authentication_refs[key] = value
    deny_by_default = authentication_raw.get("deny_by_default")
    if deny_by_default is not True:
        raise ConfigurationError("runtime.authentication.deny_by_default must remain enabled")
    authentication_config = AuthenticationConfig(
        **authentication_values,
        **authentication_refs,
        deny_by_default=True,
    )
    jobs_raw = runtime.get("jobs")
    if not isinstance(jobs_raw, Mapping):
        raise ConfigurationError("runtime.jobs must be an object")
    trigger_ref = jobs_raw.get("internal_trigger_credential_ref")
    if not isinstance(trigger_ref, str) or not CREDENTIAL_REF.fullmatch(trigger_ref):
        raise ConfigurationError(
            "runtime.jobs.internal_trigger_credential_ref must be an opaque credential-wall reference"
        )
    exact_job_values = {
        "queue": "postgresql_leased_with_sqlite_local",
        "policy_version": "jobs-v1",
        "scheduled_operations": ["read"],
        "scheduled_namespaces": ["analytics", "research"],
        "default_lease_seconds": 60,
        "max_timeout_seconds": 3600,
        "max_attempts": 10,
        "max_cost_limit_minor": 2500,
        "scheduled_outbound": False,
    }
    for key, expected in exact_job_values.items():
        if jobs_raw.get(key) != expected:
            raise ConfigurationError(f"runtime.jobs.{key} must be {expected!r}")
    jobs_config = JobsConfig(
        queue=exact_job_values["queue"],
        internal_trigger_credential_ref=trigger_ref,
        policy_version=exact_job_values["policy_version"],
        scheduled_operations=tuple(exact_job_values["scheduled_operations"]),
        scheduled_namespaces=tuple(exact_job_values["scheduled_namespaces"]),
        default_lease_seconds=exact_job_values["default_lease_seconds"],
        max_timeout_seconds=exact_job_values["max_timeout_seconds"],
        max_attempts=exact_job_values["max_attempts"],
        max_cost_limit_minor=exact_job_values["max_cost_limit_minor"],
        scheduled_outbound=exact_job_values["scheduled_outbound"],
    )
    return RuntimeConfig(
        version=version,
        active_profile=active,
        profiles=profiles,
        providers=providers,
        failover_order=failover,
        feature_defaults=dict(defaults) if isinstance(defaults, Mapping) else {},
        artifact_custody=artifact_custody,
        authentication=authentication_config,
        jobs=jobs_config,
        raw=raw,
    )


def load_show(show_id: str) -> ShowConfig:
    raw = _read_yaml(_show_config_path(show_id))
    _assert_no_secrets(raw)
    tenant_id = str(raw.get("tenant_id", ""))
    configured_show = str(raw.get("show_id", show_id))
    if not tenant_id or configured_show != show_id:
        raise ConfigurationError("show config requires matching tenant_id and show_id")
    modes = raw.get("modes", {})
    if not isinstance(modes, Mapping):
        raise ConfigurationError("show modes must be an object")
    guest_mode = str(modes.get("guest_interaction", "operator_packet"))
    if guest_mode not in {"operator_packet", "portal"}:
        raise ConfigurationError("guest_interaction must be operator_packet or portal")
    outbound_mode = str(modes.get("outbound", "draft_only"))
    if outbound_mode not in {"draft_only", "manual_receipt", "provider_connected"}:
        raise ConfigurationError("outbound must be draft_only, manual_receipt, or provider_connected")
    providers = raw.get("provider_precedence", {})
    precedence = (
        {str(key): tuple(str(item) for item in value) for key, value in providers.items() if isinstance(value, list)}
        if isinstance(providers, Mapping)
        else {}
    )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigurationError("show enabled must be a boolean")
    default = raw.get("default", False)
    if not isinstance(default, bool):
        raise ConfigurationError("show default must be a boolean")
    display_order = raw.get("display_order", 100)
    if isinstance(display_order, bool) or not isinstance(display_order, int) or not 0 <= display_order <= 10_000:
        raise ConfigurationError("show display_order must be an integer from 0 to 10000")
    dna_ref = raw.get("dna_ref")
    if dna_ref is None and not enabled:
        pass
    elif not isinstance(dna_ref, str):
        raise ConfigurationError("show dna_ref is required")
    else:
        show_resource_path(dna_ref, "dna")
    optional_refs: dict[str, str | None] = {}
    for field_name, kind in (
        ("voice_ref", "voice"),
        ("template_ref", "template"),
        ("brand_ref", "brand"),
        ("pilot_policy_ref", "pilot_policy"),
    ):
        value = raw.get(field_name)
        if value is not None and not isinstance(value, str):
            raise ConfigurationError(f"show {field_name} must be a string")
        if isinstance(value, str):
            show_resource_path(value, kind)
        optional_refs[field_name] = value
    return ShowConfig(
        tenant_id=tenant_id,
        show_id=show_id,
        label=str(raw.get("label", show_id)),
        enabled=enabled,
        guest_interaction_mode=guest_mode,
        outbound_mode=outbound_mode,
        provider_precedence=precedence,
        roles=tuple(str(item) for item in raw.get("roles", []) if isinstance(item, str)),
        dna_ref=dna_ref,
        voice_ref=optional_refs["voice_ref"],
        template_ref=optional_refs["template_ref"],
        brand_ref=optional_refs["brand_ref"],
        pilot_policy_ref=optional_refs["pilot_policy_ref"],
        display_order=display_order,
        default=default,
        raw=raw,
    )


def list_show_configs(tenant_id: str) -> tuple[ShowConfig, ...]:
    """Return enabled configured shows for one tenant in operator order."""
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ConfigurationError("tenant_id is required to list shows")
    shows = tuple(
        show
        for path in sorted((CONFIG_DIR / "shows").glob("*.yaml"))
        for show in (load_show(path.stem),)
        if show.enabled and show.tenant_id == tenant_id
    )
    defaults = [show.show_id for show in shows if show.default]
    if len(defaults) > 1:
        raise ConfigurationError(f"tenant {tenant_id!r} declares multiple default shows: {defaults}")
    return tuple(sorted(shows, key=lambda show: (show.display_order, show.label)))


def show_profile(show: ShowConfig) -> dict[str, Any]:
    """Expose the bounded per-show resource profile used by the operator UI."""
    if show.dna_ref is None:
        raise ConfigurationError("show dna_ref is required")
    dna = _read_yaml(show_resource_path(show.dna_ref, "dna"))
    show_dna = dna.get("show")
    if not isinstance(show_dna, Mapping) or str(show_dna.get("id")) != show.show_id:
        raise ConfigurationError("show DNA id must match its show configuration")
    resources: dict[str, Mapping[str, Any] | None] = {}
    for field_name, kind in (
        ("voice_ref", "voice"),
        ("template_ref", "template"),
        ("brand_ref", "brand"),
        ("pilot_policy_ref", "pilot_policy"),
    ):
        reference = getattr(show, field_name)
        if reference is None:
            resources[kind] = None
            continue
        value = _read_yaml(show_resource_path(reference, kind))
        if value.get("version") != 1 or str(value.get("show_id")) != show.show_id:
            raise ConfigurationError(f"{kind} resource must declare version 1 and the owning show_id")
        resources[kind] = value
    voice = resources["voice"]
    if voice is not None and not all(
        isinstance(voice.get(key), str) and str(voice[key]).strip() for key in ("principle", "persona")
    ):
        raise ConfigurationError("voice resource requires principle and persona")
    template = resources["template"]
    if template is not None and not all(
        isinstance(template.get(key), Mapping) for key in ("class_to_template", "templates")
    ):
        raise ConfigurationError("template resource requires class_to_template and templates")
    if template is not None:
        routing = template["class_to_template"]
        bodies = template["templates"]
        if set(routing) != {"C0", "C1", "C2", "C3"} or any(
            not isinstance(key, str) or key not in bodies for key in routing.values()
        ):
            raise ConfigurationError("template resource has invalid relationship routing")
    brand = resources["brand"]
    if brand is not None:
        from . import branding

        if not all(
            isinstance(brand.get(key), str) and str(brand[key]).strip() for key in branding.REQUIRED_BRAND_FIELDS
        ):
            raise ConfigurationError("brand resource is missing its required presentation fields")
        try:
            branding.validate_brand(dict(brand))
        except ValueError as exc:
            raise ConfigurationError(f"brand resource is invalid: {exc}") from exc
    pilot_policy = resources["pilot_policy"]
    if pilot_policy is not None and not isinstance(pilot_policy.get("policy"), Mapping):
        raise ConfigurationError("pilot policy resource requires a policy object")
    return {
        "tenant_id": show.tenant_id,
        "show_id": show.show_id,
        "label": show.label,
        "default": show.default,
        "display_order": show.display_order,
        "guest_interaction_mode": show.guest_interaction_mode,
        "outbound_mode": show.outbound_mode,
        "dna_ref": show.dna_ref,
        "dna_title": str(show_dna.get("title", show.label)),
        "primary_format": str(show_dna.get("primary_format", "unconfigured")),
        "recording_cities": [str(value) for value in show_dna.get("recording_cities", [])],
        "voice_ref": show.voice_ref,
        "template_ref": show.template_ref,
        "brand_ref": show.brand_ref,
        "pilot_policy_ref": show.pilot_policy_ref,
    }


DEFAULT_SPONSOR_SLOT_TYPES = ("pre", "mid", "post")
_SPONSOR_POLICY_KEYS = frozenset(
    {
        "currency",
        "slot_types",
        "block_publication_on_unfilled_committed_slots",
        "require_approved_claims_before_publication",
    }
)
CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


def _sponsor_policy_block(raw: Any, origin: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigurationError(f"{origin} sponsor_inventory must be an object")
    unknown = sorted(set(raw) - _SPONSOR_POLICY_KEYS)
    if unknown:
        raise ConfigurationError(f"{origin} sponsor_inventory has unsupported keys: {unknown}")
    block: dict[str, Any] = {}
    if "currency" in raw:
        currency = raw["currency"]
        if not isinstance(currency, str) or not CURRENCY_CODE.fullmatch(currency):
            raise ConfigurationError(f"{origin} sponsor_inventory currency must be an ISO 4217 code")
        block["currency"] = currency
    if "slot_types" in raw:
        values = raw["slot_types"]
        if not isinstance(values, list) or not values:
            raise ConfigurationError(f"{origin} sponsor_inventory slot_types must be a non-empty list")
        normalized = tuple(str(item) for item in values)
        if len(set(normalized)) != len(normalized) or not set(normalized) <= set(DEFAULT_SPONSOR_SLOT_TYPES):
            raise ConfigurationError(
                f"{origin} sponsor_inventory slot_types must be distinct values from {list(DEFAULT_SPONSOR_SLOT_TYPES)}"
            )
        block["slot_types"] = normalized
    for flag in (
        "block_publication_on_unfilled_committed_slots",
        "require_approved_claims_before_publication",
    ):
        if flag in raw:
            value = raw[flag]
            if not isinstance(value, bool):
                raise ConfigurationError(f"{origin} sponsor_inventory {flag} must be a boolean")
            block[flag] = value
    return block


def sponsor_inventory_policy(
    show_id: str | None = None,
    *,
    runtime: RuntimeConfig | None = None,
) -> SponsorInventoryPolicy:
    """Resolve the sponsor-inventory policy: runtime default, show override.

    An unregistered ``show_id`` is not an error here.  Tenants create shows in
    the store before anyone writes a tracked show configuration file, and the
    revenue surface must still run under the safe runtime default rather than
    fail closed on a bookkeeping gap.
    """
    current = runtime or load_runtime()
    resolved: dict[str, Any] = {
        "currency": "USD",
        "slot_types": DEFAULT_SPONSOR_SLOT_TYPES,
        "block_publication_on_unfilled_committed_slots": True,
        "require_approved_claims_before_publication": True,
    }
    runtime_block = current.raw.get("runtime", {}) if isinstance(current.raw, Mapping) else {}
    resolved.update(
        _sponsor_policy_block(
            runtime_block.get("sponsor_inventory") if isinstance(runtime_block, Mapping) else None,
            "runtime.yaml",
        )
    )
    source = "runtime"
    if show_id:
        try:
            show = load_show(show_id)
        except ConfigurationError:
            show = None
        if show is not None:
            override = _sponsor_policy_block(show.raw.get("sponsor_inventory"), f"show {show_id!r}")
            if override:
                resolved.update(override)
                source = f"show:{show_id}"
    return SponsorInventoryPolicy(
        currency=str(resolved["currency"]),
        slot_types=tuple(resolved["slot_types"]),
        block_publication_on_unfilled_committed_slots=bool(resolved["block_publication_on_unfilled_committed_slots"]),
        require_approved_claims_before_publication=bool(resolved["require_approved_claims_before_publication"]),
        source=source,
    )


def capability_report(runtime: RuntimeConfig | None = None) -> list[Capability]:
    current = runtime or load_runtime()
    report: list[Capability] = []
    for capability in CAPABILITIES:
        providers = current.providers.get(capability, ())
        if not providers:
            report.append(Capability(capability, "-", "unavailable", "no provider is registered"))
            continue
        for provider in providers:
            if not provider.enabled:
                status, reason = "disabled", "disabled by configuration"
            elif provider.mode in {"manual", "fixture", "local"}:
                status, reason = "ready", f"mode={provider.mode}"
            elif provider.credential_ref:
                status, reason = "blocked", "authenticated live smoke receipt is not present"
            else:
                status, reason = "unconfigured", "credential_ref is not present"
            report.append(Capability(capability, provider.name, status, reason, provider.credential_ref))
    return report


def validate_configuration() -> list[str]:
    runtime = load_runtime()
    errors: list[str] = []
    shows_dir = CONFIG_DIR / "shows"
    if shows_dir.exists():
        for path in sorted(shows_dir.glob("*.yaml")):
            try:
                load_show(path.stem)
            except ConfigurationError as exc:
                errors.append(str(exc))
    tenant_ids = {str(_read_yaml(path).get("tenant_id", "")) for path in shows_dir.glob("*.yaml") if path.is_file()}
    for tenant_id in sorted(tenant_id for tenant_id in tenant_ids if tenant_id):
        try:
            shows = list_show_configs(tenant_id)
            for show in shows:
                show_profile(show)
            for field_name in (
                "dna_ref",
                "voice_ref",
                "template_ref",
                "brand_ref",
                "pilot_policy_ref",
            ):
                values = [getattr(show, field_name) for show in shows if getattr(show, field_name) is not None]
                if len(values) != len(set(values)):
                    errors.append(f"tenant {tenant_id!r} must use a distinct {field_name} per show")
        except ConfigurationError as exc:
            errors.append(str(exc))
    try:
        sponsor_inventory_policy(runtime=runtime)
    except ConfigurationError as exc:
        errors.append(str(exc))
    for path in sorted(shows_dir.glob("*.yaml")) if shows_dir.exists() else ():
        try:
            sponsor_inventory_policy(path.stem, runtime=runtime)
        except ConfigurationError as exc:
            errors.append(str(exc))
    for capability, names in runtime.failover_order.items():
        known = {provider.name for provider in runtime.providers.get(capability, ())}
        unknown = [name for name in names if name not in known]
        if unknown:
            errors.append(f"failover order for {capability} names unknown providers: {unknown}")
    return errors


__all__ = [
    "CAPABILITIES",
    "Capability",
    "ConfigurationError",
    "ProviderConfig",
    "RUNTIME_PROFILES",
    "RuntimeConfig",
    "RuntimeProfile",
    "ShowConfig",
    "SponsorInventoryPolicy",
    "assert_no_secrets",
    "capability_report",
    "list_show_configs",
    "load_runtime",
    "load_show",
    "show_profile",
    "show_resource_path",
    "sponsor_inventory_policy",
    "load_show_resource",
    "validate_configuration",
]
