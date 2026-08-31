"""Scoped, provenance-backed relationship maps with safe export projections.

The relationship map composes three bounded sources: rows already stored in
the tenant/show database, manually curated configuration, and an optional
public episode archive.  Private aliases stay opaque in every projection and
all renderer inputs are revalidated before they enter Mermaid, Graphviz, JSON,
or CSV output.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml  # type: ignore[import-untyped]

from . import configuration, generation, platform, privacy, store


RELATIONSHIP_CLASSES = frozenset({"C0", "C1", "C2", "C3", "C4", "C5"})
RELATIONSHIP_STRENGTH = {
    "C0": "no_connection",
    "C1": "public_adjacency",
    "C2": "prior_professional_interaction",
    "C3": "recurring_colleague",
    "C4": "friend",
    "C5": "close_friend_mentor_or_sensitive",
}
EDGE_TYPES = platform.RELATIONSHIP_EDGE_TYPES
LEGACY_EDGE_TYPE = "legacy_unspecified"
PROJECTED_EDGE_TYPES = EDGE_TYPES | {LEGACY_EDGE_TYPE}
EXPORT_FORMATS = frozenset({"mermaid", "graphviz", "dot", "json", "csv"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EDGE_ID = re.compile(r"^edge-[0-9a-f]{20}$")
_PRIVATE_ALIAS = re.compile(r"^private-[a-z0-9][a-z0-9_-]{1,71}$")
_PRIVATE_FIELD_REF = re.compile(
    r"^private-field://[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MAX_CONFIG_BYTES = 1_000_000
_MAX_ARCHIVE_BYTES = 5_000_000
_MAX_ARCHIVE_RECORDS = 10_000


@dataclass(frozen=True)
class NetworkConfig:
    """Validated records for exactly one tenant/show scope."""

    edges: tuple[dict[str, Any], ...]
    aliases: Mapping[str, dict[str, Any]]
    targets: frozenset[str]
    archive_sources: tuple[dict[str, Any], ...]


def _strict_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise platform.PlatformError(f"{label} contains unsupported fields")


def _mapping_items(raw: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise platform.PlatformError(f"network {key} must be a list")
    if len(value) > _MAX_ARCHIVE_RECORDS:
        raise platform.PlatformError(f"network {key} exceeds the record limit")
    if not all(isinstance(item, Mapping) for item in value):
        raise platform.PlatformError(f"network {key} entries must be objects")
    return value


def _label(value: Any, field: str = "label") -> str:
    if not isinstance(value, str):
        raise platform.PlatformError(f"{field} must be text")
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > 120 or _CONTROL.search(normalized):
        raise platform.PlatformError(f"{field} must be bounded printable text")
    if privacy.private_text_kind(normalized) is not None:
        raise platform.PlatformError(f"{field} must not contain private contact data")
    return normalized


def _validated_edge_id(value: Any) -> str:
    if not isinstance(value, str) or _EDGE_ID.fullmatch(value) is None:
        raise platform.PlatformError("edge id must be a generated opaque identifier")
    return value


def _archive_provenance_ref(provenance_ref: str, guest_id: str) -> str:
    base = provenance_ref.rstrip("/")
    candidate = f"{base}/{guest_id}"
    if len(candidate) <= 200:
        return platform._ref(candidate, "provenance_ref")
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:20]
    suffix = f"/edge-{digest}"
    return platform._ref(f"{base[: 200 - len(suffix)]}{suffix}", "provenance_ref")


def _csv_label(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def _edge_type(value: Any, *, allow_legacy: bool = False) -> str:
    if not isinstance(value, str):
        raise platform.PlatformError("edge_type must be text")
    normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
    if normalized not in EDGE_TYPES:
        if allow_legacy and platform.OPAQUE.fullmatch(normalized):
            return LEGACY_EDGE_TYPE
        raise platform.PlatformError(
            "edge_type must be worked with, knows, represented by, introduced by, "
            "or appeared with"
        )
    return normalized


def _projected_edge_type(value: Any) -> str:
    if value == LEGACY_EDGE_TYPE:
        return LEGACY_EDGE_TYPE
    return _edge_type(value)


def _relationship_class(value: Any) -> str:
    if not isinstance(value, str) or value.upper() not in RELATIONSHIP_CLASSES:
        raise platform.PlatformError("relationship_class must be C0 through C5")
    return value.upper()


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise platform.PlatformError(f"{field} must be a boolean")
    return value


def _private_label_ref(value: Any) -> str:
    if not isinstance(value, str) or _PRIVATE_FIELD_REF.fullmatch(value) is None:
        raise platform.PlatformError(
            "label_ref must be an opaque private-field custody reference"
        )
    return value


def _guest_slug(value: Any) -> str:
    if not isinstance(value, str):
        raise platform.PlatformError("guest must be a name or opaque alias")
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > 160 or _CONTROL.search(normalized):
        raise platform.PlatformError("guest must be bounded printable text")
    try:
        return platform._guest(normalized)
    except platform.PlatformError:
        ascii_name = (
            unicodedata.normalize("NFKD", normalized)
            .encode("ascii", "ignore")
            .decode("ascii")
            .lower()
        )
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
        if not slug:
            digest = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()
            slug = f"guest-{digest[:20]}"
        elif len(slug) < 2:
            slug = f"guest-{slug}"
        if len(slug) > 80:
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
            slug = f"{slug[:71].rstrip('-')}-{digest}"
        return platform._guest(slug)


def _scope_matches(
    item: Mapping[str, Any], *, tenant_id: str, show_id: str
) -> tuple[str, str, bool]:
    tenant, show = platform._scope(
        str(item.get("tenant_id", "")), str(item.get("show_id", ""))
    )
    return tenant, show, tenant == tenant_id and show == show_id


def _normalize_alias(
    item: Mapping[str, Any], *, tenant_id: str, show_id: str
) -> dict[str, Any] | None:
    _strict_keys(
        item,
        frozenset(
            {
                "tenant_id",
                "show_id",
                "guest_id",
                "visibility",
                "label",
                "label_ref",
            }
        ),
        "network alias",
    )
    tenant, show, selected = _scope_matches(item, tenant_id=tenant_id, show_id=show_id)
    guest_id = platform._guest(item.get("guest_id"))
    visibility = item.get("visibility")
    if visibility not in {"public", "private"}:
        raise platform.PlatformError("alias visibility must be public or private")
    if visibility == "private":
        if _PRIVATE_ALIAS.fullmatch(guest_id) is None:
            raise platform.PlatformError(
                "private aliases must use an opaque private-* id"
            )
        if item.get("label") is not None:
            raise platform.PlatformError(
                "private aliases cannot contain display labels"
            )
        label_ref = _private_label_ref(item.get("label_ref"))
        public_label = "Private relationship"
    else:
        if guest_id.startswith("private-"):
            raise platform.PlatformError(
                "the private-* guest id prefix is reserved for private aliases"
            )
        if item.get("label_ref") is not None:
            raise platform.PlatformError(
                "public aliases cannot contain private label references"
            )
        label_ref = None
        public_label = _label(item.get("label"), "alias label")
    if not selected:
        return None
    return {
        "tenant_id": tenant,
        "show_id": show,
        "guest_id": guest_id,
        "visibility": visibility,
        "label": public_label,
        "label_ref": label_ref,
    }


def _normalize_edge(
    item: Mapping[str, Any],
    *,
    tenant_id: str,
    show_id: str,
    source_kind: str,
    strict: bool = False,
    allow_legacy: bool = False,
) -> dict[str, Any] | None:
    if strict:
        _strict_keys(
            item,
            frozenset(
                {
                    "tenant_id",
                    "show_id",
                    "source_guest_id",
                    "target_guest_id",
                    "edge_type",
                    "relationship_class",
                    "provenance_ref",
                }
            ),
            "network edge",
        )
    tenant, show, selected = _scope_matches(item, tenant_id=tenant_id, show_id=show_id)
    normalized = {
        "tenant_id": tenant,
        "show_id": show,
        "source_guest_id": platform._guest(item.get("source_guest_id")),
        "target_guest_id": platform._guest(item.get("target_guest_id")),
        "edge_type": _edge_type(
            item.get("edge_type"),
            allow_legacy=allow_legacy or source_kind == "database",
        ),
        "relationship_class": _relationship_class(item.get("relationship_class")),
        "provenance_ref": platform._ref(item.get("provenance_ref"), "provenance_ref"),
        "source_kind": source_kind,
    }
    if normalized["source_guest_id"] == normalized["target_guest_id"]:
        if source_kind == "database":
            # Older writers admitted self-edges. They carry no traversable
            # relationship evidence, so omit them without making the scoped
            # map unavailable.
            return None
        raise platform.PlatformError("relationship edges cannot point to themselves")
    return normalized if selected else None


def _resolve_resource_path(path_ref: str) -> Path:
    prefix = "resource://"
    if not path_ref.startswith(prefix):
        raise platform.PlatformError("archive path_ref must use resource://data/")
    relative = path_ref[len(prefix) :]
    if not relative.startswith("data/"):
        raise platform.PlatformError(
            "archive path_ref must stay under resource://data/"
        )
    resource_root = configuration.CONFIG_DIR.parent.resolve()
    candidate = (resource_root / relative).resolve()
    try:
        candidate.relative_to(resource_root)
    except ValueError as exc:
        raise platform.PlatformError(
            "archive path_ref escapes the resource root"
        ) from exc
    return candidate


def _normalize_archive_source(
    item: Mapping[str, Any], *, tenant_id: str, show_id: str
) -> dict[str, Any] | None:
    _strict_keys(
        item,
        frozenset(
            {
                "tenant_id",
                "show_id",
                "host_guest_id",
                "path_ref",
                "provenance_ref",
                "required",
            }
        ),
        "archive source",
    )
    tenant, show, selected = _scope_matches(item, tenant_id=tenant_id, show_id=show_id)
    required = item.get("required", False)
    if not isinstance(required, bool):
        raise platform.PlatformError("archive source required must be a boolean")
    path_ref = platform._ref(item.get("path_ref"), "path_ref")
    path = _resolve_resource_path(path_ref)
    normalized = {
        "tenant_id": tenant,
        "show_id": show,
        "host_guest_id": platform._guest(item.get("host_guest_id")),
        "path_ref": path_ref,
        "path": path,
        "provenance_ref": platform._ref(item.get("provenance_ref"), "provenance_ref"),
        "required": required,
    }
    return normalized if selected else None


def load_network_config(
    path: str | Path | None = None,
    *,
    tenant_id: str,
    show_id: str,
) -> NetworkConfig:
    """Load and validate all config entries, returning only the requested scope."""
    tenant, show = platform._scope(tenant_id, show_id)
    selected = Path(path) if path else configuration.CONFIG_DIR / "network_edges.yaml"
    if not selected.exists():
        return NetworkConfig((), {}, frozenset(), ())
    if selected.stat().st_size > _MAX_CONFIG_BYTES:
        raise platform.PlatformError("network configuration exceeds the size limit")
    try:
        raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise platform.PlatformError(
            "network configuration could not be parsed"
        ) from exc
    if not isinstance(raw, Mapping):
        raise platform.PlatformError("network configuration must be an object")
    _strict_keys(
        raw,
        frozenset({"version", "aliases", "targets", "archive_sources", "edges"}),
        "network configuration",
    )
    version = raw.get("version", 1)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in {1, 2}
    ):
        raise platform.PlatformError("network configuration version must be 1 or 2")

    aliases: dict[str, dict[str, Any]] = {}
    for item in _mapping_items(raw, "aliases"):
        alias = _normalize_alias(item, tenant_id=tenant, show_id=show)
        if alias is None:
            continue
        guest_id = alias["guest_id"]
        if guest_id in aliases and aliases[guest_id] != alias:
            raise platform.PlatformError("network alias has conflicting definitions")
        aliases[guest_id] = alias

    edges: list[dict[str, Any]] = []
    for item in _mapping_items(raw, "edges"):
        edge = _normalize_edge(
            item,
            tenant_id=tenant,
            show_id=show,
            source_kind="manual",
            strict=True,
            allow_legacy=version == 1,
        )
        if edge is not None:
            for guest_id in (edge["source_guest_id"], edge["target_guest_id"]):
                if guest_id.startswith("private-") and guest_id not in aliases:
                    raise platform.PlatformError(
                        "private edge ids require a validated scoped alias"
                    )
            edges.append(edge)

    targets: set[str] = set()
    for item in _mapping_items(raw, "targets"):
        _strict_keys(
            item,
            frozenset({"tenant_id", "show_id", "guest_id"}),
            "network target",
        )
        _, _, is_selected = _scope_matches(item, tenant_id=tenant, show_id=show)
        guest_id = platform._guest(item.get("guest_id"))
        if is_selected:
            targets.add(guest_id)

    sources: list[dict[str, Any]] = []
    for item in _mapping_items(raw, "archive_sources"):
        source = _normalize_archive_source(item, tenant_id=tenant, show_id=show)
        if source is not None:
            sources.append(source)
    return NetworkConfig(tuple(edges), aliases, frozenset(targets), tuple(sources))


def load_config_edges(
    path: str | Path | None = None,
    *,
    tenant_id: str | None = None,
    show_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compatibility projection for callers that only need manual edges."""
    if tenant_id is None or show_id is None:
        raise platform.PlatformError(
            "tenant_id and show_id are required for config edges"
        )
    return list(load_network_config(path, tenant_id=tenant_id, show_id=show_id).edges)


def _load_archive(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists() or not path.is_file():
        raise platform.PlatformError(
            "configured relationship archive is unavailable", 404
        )
    if path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise platform.PlatformError("relationship archive exceeds the size limit")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise platform.PlatformError(
            "relationship archive could not be parsed"
        ) from exc
    if not isinstance(raw, list) or len(raw) > _MAX_ARCHIVE_RECORDS:
        raise platform.PlatformError("relationship archive must be a bounded list")
    if not all(isinstance(item, Mapping) for item in raw):
        raise platform.PlatformError("relationship archive entries must be objects")
    return raw


def load_archive_edges(
    path: str | Path,
    *,
    tenant_id: str,
    show_id: str,
    host_guest_id: str,
    provenance_ref: str = "public-rss://operator-supplied/archive",
    effective_date: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], set[str]]:
    """Aggregate public appearances into scoped, minimal relationship edges."""
    tenant, show = platform._scope(tenant_id, show_id)
    host = platform._guest(host_guest_id)
    provenance = platform._ref(provenance_ref, "provenance_ref")
    cutoff = effective_date or generation.now().date()
    if type(cutoff) is not date:
        raise platform.PlatformError("effective_date must be a date")
    records = _load_archive(Path(path))
    appearances: Counter[str] = Counter()
    labels: dict[str, str] = {}
    confidences: dict[str, set[str]] = {}
    appearance_ids: set[tuple[str, str, str]] = set()
    for item in records:
        if item.get("type", "guest") != "guest":
            continue
        guest_label = _label(item.get("guest"), "archive guest")
        guest_id = _guest_slug(guest_label)
        prior_label = labels.get(guest_id)
        if prior_label is not None and prior_label.casefold() != guest_label.casefold():
            raise platform.PlatformError(
                "archive guest names produce an ambiguous alias"
            )
        confidence = item.get("confidence", "low")
        if confidence not in {"high", "medium", "low"}:
            raise platform.PlatformError(
                "archive confidence must be high, medium, or low"
            )
        recorded_date = item.get("date")
        if not isinstance(recorded_date, str):
            raise platform.PlatformError("archive date must be an ISO date")
        try:
            appearance_date = date.fromisoformat(recorded_date)
        except ValueError as exc:
            raise platform.PlatformError("archive date must be an ISO date") from exc
        if appearance_date > cutoff:
            raise platform.PlatformError("archive date cannot be in the future")
        episode_no = item.get("episode_no")
        if (
            isinstance(episode_no, (str, int))
            and not isinstance(episode_no, bool)
            and str(episode_no).strip()
        ):
            appearance_id = (guest_id, "episode", str(episode_no).strip().casefold())
        else:
            raw_title = item.get("title")
            title = (
                " ".join(raw_title.strip().split()).casefold()
                if isinstance(raw_title, str)
                else ""
            )
            appearance_id = (guest_id, recorded_date, title)
        labels[guest_id] = guest_label
        if appearance_id in appearance_ids:
            continue
        appearance_ids.add(appearance_id)
        appearances[guest_id] += 1
        confidences.setdefault(guest_id, set()).add(confidence)

    edges: list[dict[str, Any]] = []
    targets: set[str] = set()
    for guest_id in sorted(appearances):
        if guest_id == host:
            continue
        if appearances[guest_id] >= 2:
            relationship_class = "C3"
        elif "high" in confidences[guest_id]:
            relationship_class = "C2"
        else:
            relationship_class = "C1"
        if relationship_class in {"C2", "C3"}:
            targets.add(guest_id)
        edges.append(
            {
                "tenant_id": tenant,
                "show_id": show,
                "source_guest_id": host,
                "target_guest_id": guest_id,
                "edge_type": "worked_with",
                "relationship_class": relationship_class,
                "provenance_ref": _archive_provenance_ref(provenance, guest_id),
                "source_kind": "archive",
            }
        )
    return edges, labels, targets


def _edge_id(edge: Mapping[str, Any]) -> str:
    identity = "|".join(
        str(edge[key])
        for key in (
            "tenant_id",
            "show_id",
            "source_guest_id",
            "target_guest_id",
            "edge_type",
            "relationship_class",
            "provenance_ref",
            "source_kind",
        )
    )
    return f"edge-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def _merge_alias_label(
    labels: dict[str, dict[str, Any]], alias: Mapping[str, Any]
) -> None:
    guest_id = str(alias["guest_id"])
    current = labels.get(guest_id)
    if (
        current
        and current["visibility"] == "private"
        and alias["visibility"] != "private"
    ):
        raise platform.PlatformError("private relationship aliases cannot be relabeled")
    labels[guest_id] = {
        "label": alias["label"],
        "visibility": alias["visibility"],
    }


def _display_label(guest_id: str, aliases: Mapping[str, Mapping[str, Any]]) -> str:
    alias = aliases.get(guest_id)
    if alias is not None:
        return str(alias["label"])
    if guest_id.startswith("private-"):
        return "Private relationship"
    synthesized = " ".join(
        part.capitalize() for part in guest_id.replace("_", "-").split("-")
    )
    return _label(synthesized, "unaliased guest label")


def _resolve_guest(
    value: Any,
    *,
    aliases: Mapping[str, Mapping[str, Any]],
    known_ids: set[str],
) -> str:
    if not isinstance(value, str):
        raise platform.PlatformError("guest must be a name or opaque alias")
    raw = " ".join(value.strip().split())
    if not raw:
        raise platform.PlatformError("guest must be a name or opaque alias")
    by_label = [
        guest_id
        for guest_id, alias in aliases.items()
        if alias.get("visibility") == "public"
        and str(alias.get("label", "")).casefold() == raw.casefold()
    ]
    if len(by_label) > 1:
        raise platform.PlatformError("guest label is ambiguous in this tenant/show")
    if by_label:
        return by_label[0]
    slug = _guest_slug(raw)
    return slug if slug in known_ids else slug


def graph(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    root_guest_id: str,
    depth: int = 2,
    config_path: str | Path | None = None,
    archive_path: str | Path | None = None,
    archive_host_guest_id: str = "ari",
    target_guest_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the shortest-path projection for one tenant/show."""
    if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 5:
        raise platform.PlatformError("depth must be between 0 and 5")
    tenant, show = platform._scope(tenant_id, show_id)
    config = load_network_config(config_path, tenant_id=tenant, show_id=show)
    aliases: dict[str, dict[str, Any]] = {}
    for alias in config.aliases.values():
        _merge_alias_label(aliases, alias)

    edges: list[dict[str, Any]] = []
    database_edges = store.fetch_all(
        conn,
        "SELECT tenant_id, show_id, source_guest_id, target_guest_id, edge_type, "
        "relationship_class, provenance_ref FROM relationship_edges "
        "WHERE tenant_id = ? AND show_id = ?",
        (tenant, show),
    )
    for item in database_edges:
        edge = _normalize_edge(
            item,
            tenant_id=tenant,
            show_id=show,
            source_kind="database",
        )
        if edge is not None:
            edges.append(edge)
    edges.extend(config.edges)
    targets = set(config.targets)

    archive_sources: list[dict[str, Any]]
    if archive_path is not None:
        archive_sources = [
            {
                "path": Path(archive_path),
                "host_guest_id": platform._guest(archive_host_guest_id),
                "provenance_ref": "public-rss://operator-supplied/archive",
                "required": True,
            }
        ]
    else:
        archive_sources = list(config.archive_sources)
    for archive_source in archive_sources:
        source_path = Path(archive_source["path"])
        if not source_path.exists() and not archive_source["required"]:
            continue
        archive_edges, archive_labels, archive_targets = load_archive_edges(
            source_path,
            tenant_id=tenant,
            show_id=show,
            host_guest_id=str(archive_source["host_guest_id"]),
            provenance_ref=str(archive_source["provenance_ref"]),
        )
        edges.extend(archive_edges)
        targets.update(archive_targets)
        for guest_id, public_label in archive_labels.items():
            current_alias = aliases.get(guest_id)
            if current_alias is not None and (
                current_alias.get("visibility") != "public"
                or str(current_alias.get("label", "")).casefold()
                != public_label.casefold()
            ):
                raise platform.PlatformError(
                    "archive guest conflicts with a configured alias"
                )
            if current_alias is None:
                aliases[guest_id] = {
                    "label": public_label,
                    "visibility": "public",
                }

    deduplicated: dict[str, dict[str, Any]] = {}
    for edge in edges:
        edge_id = _edge_id(edge)
        deduplicated[edge_id] = {**edge, "id": edge_id}
    edges = sorted(
        deduplicated.values(),
        key=lambda item: (
            item["source_guest_id"],
            item["target_guest_id"],
            item["relationship_class"],
            item["source_kind"],
            item["id"],
        ),
    )
    known_ids = {
        guest_id
        for edge in edges
        for guest_id in (edge["source_guest_id"], edge["target_guest_id"])
    } | set(aliases)
    root = _resolve_guest(root_guest_id, aliases=aliases, known_ids=known_ids)
    known_ids.add(root)
    for value in target_guest_ids or ():
        targets.add(_resolve_guest(value, aliases=aliases, known_ids=known_ids))
    targets.discard(root)

    adjacency: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for edge in edges:
        source_id = str(edge["source_guest_id"])
        target_id = str(edge["target_guest_id"])
        adjacency.setdefault(source_id, []).append((edge, target_id))
        adjacency.setdefault(target_id, []).append((edge, source_id))
    found: dict[str, int] = {root: 0}
    parent_edge: dict[str, str] = {}
    parent_node: dict[str, str] = {}
    selected: dict[str, dict[str, Any]] = {}
    queue = deque([root])
    while queue:
        source_id = queue.popleft()
        distance = found[source_id]
        if distance >= depth:
            continue
        for edge, neighbor_id in adjacency.get(source_id, []):
            selected.setdefault(str(edge["id"]), {**edge, "distance": distance + 1})
            if neighbor_id not in found:
                found[neighbor_id] = distance + 1
                parent_edge[neighbor_id] = str(edge["id"])
                parent_node[neighbor_id] = source_id
                queue.append(neighbor_id)

    target_paths: list[dict[str, Any]] = []
    path_node_ids: set[str] = {root}
    path_edge_ids: set[str] = set()
    edge_by_id = {str(edge["id"]): edge for edge in edges}
    for target in sorted(targets & set(found), key=lambda item: (found[item], item)):
        node_ids = [target]
        edge_ids: list[str] = []
        current = target
        while current != root:
            parent_edge_id = parent_edge.get(current)
            if parent_edge_id is None:
                break
            edge_ids.append(parent_edge_id)
            current = parent_node[current]
            node_ids.append(current)
        if current != root:
            continue
        node_ids.reverse()
        edge_ids.reverse()
        path_node_ids.update(node_ids)
        path_edge_ids.update(edge_ids)
        target_paths.append(
            {
                "target_id": target,
                "target_label": _display_label(target, aliases),
                "node_ids": node_ids,
                "edge_ids": edge_ids,
            }
        )

    nodes: list[dict[str, Any]] = []
    for guest_id, distance in sorted(
        found.items(), key=lambda item: (item[1], item[0])
    ):
        alias = aliases.get(guest_id, {})
        incoming = edge_by_id.get(parent_edge.get(guest_id, ""))
        nodes.append(
            {
                "id": guest_id,
                "label": _display_label(guest_id, aliases),
                "visibility": alias.get(
                    "visibility",
                    "private" if guest_id.startswith("private-") else "public",
                ),
                "distance": distance,
                "relationship_class": incoming.get("relationship_class")
                if incoming
                else None,
                "target_candidate": guest_id in targets,
                "on_target_path": guest_id in path_node_ids and bool(target_paths),
            }
        )

    projected_edges: list[dict[str, Any]] = []
    for edge in selected.values():
        relationship_class = str(edge["relationship_class"])
        projected_edges.append(
            {
                **edge,
                "source_label": _display_label(str(edge["source_guest_id"]), aliases),
                "target_label": _display_label(str(edge["target_guest_id"]), aliases),
                "relationship_strength": RELATIONSHIP_STRENGTH[relationship_class],
                "on_target_path": edge["id"] in path_edge_ids,
            }
        )
    projected_edges.sort(key=lambda item: (item["distance"], item["id"]))
    source_counts = Counter(str(edge["source_kind"]) for edge in projected_edges)
    return {
        "schema_version": 1,
        "tenant_id": tenant,
        "show_id": show,
        "root": root,
        "depth": depth,
        "nodes": nodes,
        "edges": projected_edges,
        "target_paths": target_paths,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(projected_edges),
            "reachable_target_count": len(target_paths),
            "source_counts": dict(sorted(source_counts.items())),
        },
    }


def _safe_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise platform.PlatformError("network graph must be an object")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise platform.PlatformError("network graph schema_version must be 1")
    tenant, show = platform._scope(
        str(value.get("tenant_id", "")), str(value.get("show_id", ""))
    )
    root = platform._guest(value.get("root"))
    depth = value.get("depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 5:
        raise platform.PlatformError("depth must be between 0 and 5")
    raw_nodes = value.get("nodes")
    raw_edges = value.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise platform.PlatformError("network graph nodes and edges must be lists")
    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, Mapping):
            raise platform.PlatformError("network graph nodes must be objects")
        guest_id = platform._guest(item.get("id"))
        visibility = item.get("visibility")
        if visibility not in {"public", "private"}:
            raise platform.PlatformError("node visibility must be public or private")
        distance = item.get("distance")
        if (
            isinstance(distance, bool)
            or not isinstance(distance, int)
            or not 0 <= distance <= depth
        ):
            raise platform.PlatformError("node distance is invalid")
        relationship_class = item.get("relationship_class")
        if relationship_class is not None:
            relationship_class = _relationship_class(relationship_class)
        node = {
            "id": guest_id,
            "label": "Private relationship"
            if visibility == "private"
            else _label(item.get("label"), "node label"),
            "visibility": visibility,
            "distance": distance,
            "relationship_class": relationship_class,
            "target_candidate": _boolean(
                item.get("target_candidate"), "target_candidate"
            ),
            "on_target_path": _boolean(item.get("on_target_path"), "on_target_path"),
        }
        if guest_id in node_ids:
            raise platform.PlatformError("network graph contains duplicate nodes")
        node_ids.add(guest_id)
        nodes.append(node)
    if root not in node_ids:
        raise platform.PlatformError("network graph root is missing from nodes")
    node_labels = {node["id"]: node["label"] for node in nodes}

    edges: list[dict[str, Any]] = []
    edge_ids: set[str] = set()
    edge_endpoints: dict[str, frozenset[str]] = {}
    for item in raw_edges:
        if not isinstance(item, Mapping):
            raise platform.PlatformError("network graph edges must be objects")
        source = platform._guest(item.get("source_guest_id"))
        target = platform._guest(item.get("target_guest_id"))
        if source not in node_ids or target not in node_ids:
            raise platform.PlatformError(
                "network graph edge references an unknown node"
            )
        edge_id = _validated_edge_id(item.get("id"))
        if edge_id in edge_ids:
            raise platform.PlatformError("network graph contains duplicate edges")
        edge_ids.add(edge_id)
        edge_endpoints[edge_id] = frozenset({source, target})
        relationship_class = _relationship_class(item.get("relationship_class"))
        source_kind = item.get("source_kind")
        if source_kind not in {"database", "manual", "archive"}:
            raise platform.PlatformError("edge source_kind is invalid")
        distance = item.get("distance")
        if (
            isinstance(distance, bool)
            or not isinstance(distance, int)
            or not 1 <= distance <= depth
        ):
            raise platform.PlatformError("edge distance is invalid")
        edges.append(
            {
                "id": edge_id,
                "tenant_id": tenant,
                "show_id": show,
                "source_guest_id": source,
                "target_guest_id": target,
                "source_label": node_labels[source],
                "target_label": node_labels[target],
                "edge_type": _projected_edge_type(item.get("edge_type")),
                "relationship_class": relationship_class,
                "relationship_strength": RELATIONSHIP_STRENGTH[relationship_class],
                "provenance_ref": platform._ref(
                    item.get("provenance_ref"), "provenance_ref"
                ),
                "source_kind": source_kind,
                "distance": distance,
                "on_target_path": _boolean(
                    item.get("on_target_path"), "on_target_path"
                ),
            }
        )
    paths: list[dict[str, Any]] = []
    raw_paths = value.get("target_paths", [])
    if not isinstance(raw_paths, list):
        raise platform.PlatformError("target_paths must be a list")
    for item in raw_paths:
        if not isinstance(item, Mapping):
            raise platform.PlatformError("target paths must be objects")
        target_id = platform._guest(item.get("target_id"))
        path_nodes = item.get("node_ids")
        path_edges = item.get("edge_ids")
        if (
            target_id not in node_ids
            or not isinstance(path_nodes, list)
            or not isinstance(path_edges, list)
        ):
            raise platform.PlatformError("target path is invalid")
        normalized_nodes = [platform._guest(node_id) for node_id in path_nodes]
        normalized_edges = [_validated_edge_id(edge_id) for edge_id in path_edges]
        if (
            not normalized_nodes
            or normalized_nodes[0] != root
            or normalized_nodes[-1] != target_id
            or not set(normalized_nodes) <= node_ids
            or not set(normalized_edges) <= edge_ids
            or len(normalized_edges) != len(normalized_nodes) - 1
        ):
            raise platform.PlatformError("target path is invalid")
        if any(
            edge_endpoints[edge_id] != frozenset({source, target})
            for source, target, edge_id in zip(
                normalized_nodes[:-1],
                normalized_nodes[1:],
                normalized_edges,
                strict=True,
            )
        ):
            raise platform.PlatformError("target path edges do not connect its nodes")
        paths.append(
            {
                "target_id": target_id,
                "target_label": node_labels[target_id],
                "node_ids": normalized_nodes,
                "edge_ids": normalized_edges,
            }
        )
    source_counts = Counter(str(edge["source_kind"]) for edge in edges)
    return {
        "schema_version": 1,
        "tenant_id": tenant,
        "show_id": show,
        "root": root,
        "depth": depth,
        "nodes": nodes,
        "edges": edges,
        "target_paths": paths,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "reachable_target_count": len(paths),
            "source_counts": dict(sorted(source_counts.items())),
        },
    }


def _mermaid_label(value: str) -> str:
    return json.dumps(html.escape(value, quote=True), ensure_ascii=False)


def render_graph(value: Mapping[str, Any], format: str = "json") -> str:
    """Render one redacted, revalidated network-map projection."""
    normalized = str(format).lower()
    if normalized not in EXPORT_FORMATS:
        raise platform.PlatformError("format must be mermaid, graphviz, json, or csv")
    graph_value = _safe_projection(value)
    if normalized == "json":
        return json.dumps(graph_value, sort_keys=True, indent=2, ensure_ascii=False)

    node_ids = {
        node["id"]: f"n{index}" for index, node in enumerate(graph_value["nodes"])
    }
    if normalized == "mermaid":
        lines = ["graph LR"]
        root = graph_value["root"]
        node_classes: list[str] = []
        for node in graph_value["nodes"]:
            classes = []
            if node["id"] == root:
                classes.append("root")
            if node["target_candidate"]:
                classes.append("target")
            if node["visibility"] == "private":
                classes.append("private")
            lines.append(f"  {node_ids[node['id']]}[{_mermaid_label(node['label'])}]")
            if classes:
                node_classes.append(
                    f"  class {node_ids[node['id']]} {','.join(classes)};"
                )
        highlighted_links: list[int] = []
        for index, edge in enumerate(graph_value["edges"]):
            edge_label = html.escape(
                f"{edge['edge_type'].replace('_', ' ')} / {edge['relationship_class']}",
                quote=True,
            )
            lines.append(
                f"  {node_ids[edge['source_guest_id']]} -->|{edge_label}| "
                f"{node_ids[edge['target_guest_id']]}"
            )
            if edge["on_target_path"]:
                highlighted_links.append(index)
        lines.extend(
            [
                "  classDef root stroke:#7aa300,stroke-width:3px;",
                "  classDef target stroke:#b4f000,stroke-width:3px;",
                "  classDef private stroke:#777,stroke-dasharray:4 3;",
            ]
        )
        lines.extend(node_classes)
        if highlighted_links:
            lines.append(
                "  linkStyle "
                + ",".join(str(index) for index in highlighted_links)
                + " stroke:#b4f000,stroke-width:3px;"
            )
        return "\n".join(lines)

    if normalized in {"graphviz", "dot"}:
        lines = ["digraph hospes {", "  rankdir=LR;"]
        root = graph_value["root"]
        for node in graph_value["nodes"]:
            attributes = [f"label={json.dumps(node['label'], ensure_ascii=False)}"]
            if node["id"] == root:
                attributes.extend(["shape=doublecircle", 'color="#7aa300"'])
            elif node["target_candidate"]:
                attributes.extend(["shape=box", 'color="#7aa300"'])
            if node["visibility"] == "private":
                attributes.append("style=dashed")
            lines.append(f"  {node_ids[node['id']]} [{', '.join(attributes)}];")
        for edge in graph_value["edges"]:
            attributes = [
                "label="
                + json.dumps(
                    f"{edge['edge_type'].replace('_', ' ')} / "
                    f"{edge['relationship_class']}",
                    ensure_ascii=False,
                )
            ]
            if edge["on_target_path"]:
                attributes.extend(['color="#7aa300"', "penwidth=3"])
            lines.append(
                f"  {node_ids[edge['source_guest_id']]} -> "
                f"{node_ids[edge['target_guest_id']]} "
                f"[{', '.join(attributes)}];"
            )
        lines.append("}")
        return "\n".join(lines)

    output = io.StringIO(newline="")
    fields = (
        "tenant_id",
        "show_id",
        "root_id",
        "source_id",
        "source_label",
        "target_id",
        "target_label",
        "edge_type",
        "relationship_class",
        "relationship_strength",
        "distance",
        "target_candidate",
        "on_target_path",
        "source_kind",
        "provenance_ref",
    )
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    target_ids = {
        node["id"] for node in graph_value["nodes"] if node["target_candidate"]
    }
    for edge in graph_value["edges"]:
        writer.writerow(
            {
                "tenant_id": graph_value["tenant_id"],
                "show_id": graph_value["show_id"],
                "root_id": graph_value["root"],
                "source_id": edge["source_guest_id"],
                "source_label": _csv_label(edge["source_label"]),
                "target_id": edge["target_guest_id"],
                "target_label": _csv_label(edge["target_label"]),
                "edge_type": edge["edge_type"],
                "relationship_class": edge["relationship_class"],
                "relationship_strength": edge["relationship_strength"],
                "distance": edge["distance"],
                "target_candidate": bool(
                    {edge["source_guest_id"], edge["target_guest_id"]} & target_ids
                ),
                "on_target_path": edge["on_target_path"],
                "source_kind": edge["source_kind"],
                "provenance_ref": edge["provenance_ref"],
            }
        )
    return output.getvalue()


def network_map(conn: store.DatabaseConnection, **kwargs: Any) -> str:
    selected_format = kwargs.pop("format", "json")
    return render_graph(graph(conn, **kwargs), selected_format)


__all__ = [
    "EDGE_TYPES",
    "EXPORT_FORMATS",
    "LEGACY_EDGE_TYPE",
    "NetworkConfig",
    "PROJECTED_EDGE_TYPES",
    "RELATIONSHIP_CLASSES",
    "RELATIONSHIP_STRENGTH",
    "graph",
    "load_archive_edges",
    "load_config_edges",
    "load_network_config",
    "network_map",
    "render_graph",
]
