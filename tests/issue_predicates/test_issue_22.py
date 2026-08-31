from __future__ import annotations

import csv
import io
import json
from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
import yaml

from conftest import synthetic_bearer_authenticator
from hospes import authentication, network_graph, platform, service, store
from hospes.__main__ import main
from hospes.api import create_app


try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]


TENANT = "tenant-issue-22"
SHOW = "show-issue-22"
OTHER_TENANT = "tenant-other-22"
OTHER_SHOW = "show-other-22"


def _archive(path: Path) -> Path:
    path.write_text(
        json.dumps(
            [
                {
                    "episode_no": "0",
                    "title": "Theo Von",
                    "date": "2023-12-01",
                    "type": "guest",
                    "guest": "Theo Von",
                    "confidence": "high",
                },
                {
                    "episode_no": "1",
                    "title": "Mark Normand",
                    "date": "2024-01-02",
                    "type": "guest",
                    "guest": "Mark Normand",
                    "guest_raw": "must-not-project",
                    "confidence": "high",
                    "description_head": "must-not-project",
                },
                {
                    "episode_no": "2",
                    "title": "Returning Alum",
                    "date": "2024-02-03",
                    "type": "guest",
                    "guest": "Returning Alum",
                    "confidence": "high",
                },
                {
                    "episode_no": "3",
                    "title": "Returning Alum",
                    "date": "2025-02-03",
                    "type": "guest",
                    "guest": "Returning Alum",
                    "confidence": "high",
                },
                {
                    "episode_no": "4",
                    "title": "Low Confidence Alum",
                    "date": "2025-03-04",
                    "type": "guest",
                    "guest": "Low Confidence Alum",
                    "confidence": "low",
                },
                {
                    "episode_no": "5",
                    "title": "Solo episode",
                    "date": "2025-04-05",
                    "type": "solo",
                    "guest": "Ignored Solo",
                    "confidence": "high",
                },
            ]
        ),
        encoding="utf-8",
    )
    return path


def _config(path: Path, *, include_private: bool = False) -> Path:
    aliases: list[dict[str, object]] = [
        {
            "tenant_id": TENANT,
            "show_id": SHOW,
            "guest_id": "ari",
            "visibility": "public",
            "label": "Host",
        },
        {
            "tenant_id": TENANT,
            "show_id": SHOW,
            "guest_id": "theo-von",
            "visibility": "public",
            "label": "Theo Von",
        },
        {
            "tenant_id": TENANT,
            "show_id": SHOW,
            "guest_id": "mark-normand",
            "visibility": "public",
            "label": "Mark Normand",
        },
        {
            "tenant_id": OTHER_TENANT,
            "show_id": OTHER_SHOW,
            "guest_id": "private-other-22",
            "visibility": "private",
            "label_ref": "private-field://00000000-0000-4000-8000-000000000022",
        },
    ]
    edges: list[dict[str, object]] = [
        {
            "tenant_id": TENANT,
            "show_id": SHOW,
            "source_guest_id": "theo-von",
            "target_guest_id": "mark-normand",
            "edge_type": "introduced by",
            "relationship_class": "C1",
            "provenance_ref": "owner://network/theo-mark",
        },
        {
            "tenant_id": OTHER_TENANT,
            "show_id": OTHER_SHOW,
            "source_guest_id": "theo-von",
            "target_guest_id": "private-other-22",
            "edge_type": "knows",
            "relationship_class": "C1",
            "provenance_ref": "owner://network/private-other",
        },
    ]
    targets: list[dict[str, str]] = [
        {"tenant_id": TENANT, "show_id": SHOW, "guest_id": "mark-normand"}
    ]
    if include_private:
        aliases.append(
            {
                "tenant_id": TENANT,
                "show_id": SHOW,
                "guest_id": "private-connector-22",
                "visibility": "private",
                "label_ref": "private-field://00000000-0000-4000-8000-000000000122",
            }
        )
        edges.append(
            {
                "tenant_id": TENANT,
                "show_id": SHOW,
                "source_guest_id": "mark-normand",
                "target_guest_id": "private-connector-22",
                "edge_type": "represented by",
                "relationship_class": "C3",
                "provenance_ref": "owner://network/private-connector",
            }
        )
        targets.append(
            {
                "tenant_id": TENANT,
                "show_id": SHOW,
                "guest_id": "private-connector-22",
            }
        )
    path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "aliases": aliases,
                "targets": targets,
                "edges": edges,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_live_archive_manual_graph_resolves_name_and_highlights_target_paths(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "network.sqlite3")
    platform.add_relationship_edge(
        conn,
        tenant_id=OTHER_TENANT,
        show_id=OTHER_SHOW,
        source_guest_id="theo-von",
        target_guest_id="other-secret",
        edge_type="knows",
        relationship_class="C1",
        provenance_ref="owner://network/other-secret",
    )
    value = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="Theo Von",
        depth=2,
        config_path=_config(tmp_path / "network.yaml"),
        archive_path=_archive(tmp_path / "archive.json"),
    )
    assert value["root"] == "theo-von"
    assert {node["id"] for node in value["nodes"]} == {
        "ari",
        "low-confidence-alum",
        "returning-alum",
        "theo-von",
        "mark-normand",
    }
    paths = {path["target_id"]: path for path in value["target_paths"]}
    assert paths["mark-normand"]["node_ids"] == ["theo-von", "mark-normand"]
    assert paths["returning-alum"]["node_ids"] == [
        "theo-von",
        "ari",
        "returning-alum",
    ]
    direct = next(
        edge
        for edge in value["edges"]
        if edge["source_guest_id"] == "theo-von"
        and edge["target_guest_id"] == "mark-normand"
    )
    incoming = next(
        edge
        for edge in value["edges"]
        if edge["source_guest_id"] == "ari"
        and edge["target_guest_id"] == "theo-von"
        and edge["source_kind"] == "archive"
    )
    assert direct["edge_type"] == "introduced_by"
    assert direct["relationship_strength"] == "public_adjacency"
    assert direct["on_target_path"] is True
    assert incoming["on_target_path"] is True
    assert value["summary"] == {
        "node_count": 5,
        "edge_count": 5,
        "reachable_target_count": 2,
        "source_counts": {"archive": 4, "manual": 1},
    }
    serialized = json.dumps(value)
    assert "other-secret" not in serialized
    assert "private-other-22" not in serialized
    assert "guest_raw" not in serialized
    assert "description_head" not in serialized

    mermaid = network_graph.render_graph(value, "mermaid")
    graphviz = network_graph.render_graph(value, "graphviz")
    csv_rows = list(
        csv.DictReader(io.StringIO(network_graph.render_graph(value, "csv")))
    )
    assert mermaid.startswith("graph LR")
    assert "linkStyle 0" in mermaid
    assert graphviz.startswith("digraph hospes")
    assert 'color="#7aa300"' in graphviz
    direct_row = next(
        row for row in csv_rows if row["provenance_ref"] == "owner://network/theo-mark"
    )
    assert direct_row["root_id"] == "theo-von"
    assert direct_row["target_candidate"] == "True"
    conn.close()


def test_real_checkout_archive_is_aggregated_minimally_and_not_wheel_duplicated(
    tmp_path: Path,
) -> None:
    edges, labels, targets = network_graph.load_archive_edges(
        Path("data/unlicensed-therapy/archive.json"),
        tenant_id="hospes",
        show_id="flagship",
        host_guest_id="ari",
        provenance_ref="public-rss://unlicensed-therapy/archive",
    )
    classes = Counter(edge["relationship_class"] for edge in edges)
    assert len(edges) == 169
    assert len(labels) == 169
    assert classes == {"C1": 1, "C2": 152, "C3": 16}
    assert len(targets) == 168
    assert labels["mark-normand"] == "Mark Normand"
    assert all(edge["tenant_id"] == "hospes" for edge in edges)
    assert all(edge["show_id"] == "flagship" for edge in edges)
    assert all(
        set(edge)
        == {
            "tenant_id",
            "show_id",
            "source_guest_id",
            "target_guest_id",
            "edge_type",
            "relationship_class",
            "provenance_ref",
            "source_kind",
        }
        for edge in edges
    )

    conn = store.connect(tmp_path / "live.sqlite3")
    value = network_graph.graph(
        conn,
        tenant_id="hospes",
        show_id="flagship",
        root_guest_id="Theo Von",
    )
    assert value["target_paths"][0]["target_id"] == "mark-normand"
    assert value["summary"] == {
        "node_count": 171,
        "edge_count": 171,
        "reachable_target_count": 168,
        "source_counts": {"archive": 169, "manual": 2},
    }
    assert next(node for node in value["nodes"] if node["id"] == "ari")["distance"] == 1
    assert (
        next(node for node in value["nodes"] if node["id"] == "anthony-devries")[
            "distance"
        ]
        == 2
    )
    conn.close()

    assert not Path("hospes/resources/data/unlicensed-therapy/archive.json").exists()
    assert not Path("hospes/resources/data/unlicensed-therapy/guests.json").exists()
    fixture = json.loads(
        Path("hospes/resources/data/network_archive_fixture.json").read_text()
    )
    assert all(str(item["guest"]).startswith("Synthetic ") for item in fixture)


def test_private_aliases_are_validated_scoped_and_redacted_from_every_export(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "private.sqlite3")
    value = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="Theo Von",
        depth=3,
        config_path=_config(tmp_path / "private.yaml", include_private=True),
        archive_path=_archive(tmp_path / "private-archive.json"),
    )
    private_node = next(
        node for node in value["nodes"] if node["id"] == "private-connector-22"
    )
    assert private_node["visibility"] == "private"
    assert private_node["label"] == "Private relationship"
    assert any(
        path["target_label"] == "Private relationship" for path in value["target_paths"]
    )
    for export_format in ("json", "mermaid", "graphviz", "dot", "csv"):
        rendered = network_graph.render_graph(value, export_format)
        assert "private-field://" not in rendered
        assert "000000000122" not in rendered
        assert "Private relationship" in rendered

    other = network_graph.load_network_config(
        tmp_path / "private.yaml", tenant_id=OTHER_TENANT, show_id=OTHER_SHOW
    )
    assert set(other.aliases) == {"private-other-22"}
    assert other.edges[0]["target_guest_id"] == "private-other-22"
    conn.close()


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda raw: raw["aliases"].append(
                {
                    "tenant_id": TENANT,
                    "show_id": SHOW,
                    "guest_id": "private-bad-label",
                    "visibility": "private",
                    "label": "A private person",
                    "label_ref": "private-field://00000000-0000-4000-8000-000000000222",
                }
            ),
            "cannot contain display labels",
        ),
        (
            lambda raw: raw["aliases"].append(
                {
                    "tenant_id": TENANT,
                    "show_id": SHOW,
                    "guest_id": "named-private-alias",
                    "visibility": "private",
                    "label_ref": "private-field://00000000-0000-4000-8000-000000000222",
                }
            ),
            "private-.* id",
        ),
        (
            lambda raw: raw["aliases"].append(
                {
                    "tenant_id": TENANT,
                    "show_id": SHOW,
                    "guest_id": "public-alias",
                    "visibility": "public",
                    "label": "Public Alias",
                    "label_ref": "private-field://00000000-0000-4000-8000-000000000222",
                }
            ),
            "cannot contain private label references",
        ),
        (
            lambda raw: raw["edges"].append(
                {
                    "tenant_id": TENANT,
                    "show_id": SHOW,
                    "source_guest_id": "theo-von",
                    "target_guest_id": "private-undefined-22",
                    "edge_type": "knows",
                    "relationship_class": "C2",
                    "provenance_ref": "owner://network/private-undefined",
                }
            ),
            "require a validated scoped alias",
        ),
        (
            lambda raw: raw["edges"].append(
                {
                    "tenant_id": TENANT,
                    "show_id": SHOW,
                    "source_guest_id": "theo-von",
                    "target_guest_id": "theo-von",
                    "edge_type": "knows",
                    "relationship_class": "C2",
                    "provenance_ref": "owner://network/self",
                }
            ),
            "cannot point to themselves",
        ),
        (
            lambda raw: raw["edges"][0].update({"edge_type": "follows online"}),
            "edge_type must be",
        ),
        (
            lambda raw: raw["edges"][0].update({"unexpected": "hidden"}),
            "unsupported fields",
        ),
        (
            lambda raw: raw.update({"version": True}),
            "version must be",
        ),
    ],
)
def test_invalid_manual_alias_and_edge_contracts_fail_closed(
    tmp_path: Path, mutate, match: str
) -> None:
    path = _config(tmp_path / "invalid.yaml")
    raw = yaml.safe_load(path.read_text())
    mutate(raw)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(platform.PlatformError, match=match):
        network_graph.load_network_config(path, tenant_id=TENANT, show_id=SHOW)


def test_archive_and_archive_source_validation_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "archive.json"
    for payload, match in (
        ({"guest": "not-a-list"}, "bounded list"),
        (["not-an-object"], "entries must be objects"),
        (
            [
                {
                    "guest": "Guest One",
                    "date": "not-a-date",
                    "type": "guest",
                    "confidence": "high",
                }
            ],
            "ISO date",
        ),
        (
            [
                {
                    "guest": "Guest One",
                    "date": "2026-01-01",
                    "type": "guest",
                    "confidence": "invented",
                }
            ],
            "confidence",
        ),
        (
            [
                {
                    "guest": "A B",
                    "date": "2026-01-01",
                    "type": "guest",
                    "confidence": "high",
                },
                {
                    "guest": "A-B",
                    "date": "2026-01-02",
                    "type": "guest",
                    "confidence": "high",
                },
            ],
            "ambiguous alias",
        ),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(platform.PlatformError, match=match):
            network_graph.load_archive_edges(
                path,
                tenant_id=TENANT,
                show_id=SHOW,
                host_guest_id="ari",
            )

    bad_source = tmp_path / "bad-source.yaml"
    bad_source.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "archive_sources": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "host_guest_id": "ari",
                        "path_ref": "resource://data/../../private.json",
                        "provenance_ref": "public-rss://fixture/archive",
                        "required": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(platform.PlatformError, match="escapes the resource root"):
        network_graph.load_network_config(bad_source, tenant_id=TENANT, show_id=SHOW)
    raw = yaml.safe_load(bad_source.read_text())
    raw["archive_sources"][0]["path_ref"] = "owner://outside/archive"
    bad_source.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(platform.PlatformError, match="resource://data"):
        network_graph.load_network_config(bad_source, tenant_id=TENANT, show_id=SHOW)


def test_renderers_revalidate_escape_and_project_only_declared_fields(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "render.sqlite3")
    value = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="Theo Von",
        config_path=_config(tmp_path / "render.yaml"),
        archive_path=_archive(tmp_path / "render-archive.json"),
    )
    injected = deepcopy(value)
    injected_target_id = injected["edges"][0]["target_guest_id"]
    injected_node = next(
        node for node in injected["nodes"] if node["id"] == injected_target_id
    )
    injected_node["label"] = 'Mark "]--> n9[<script>alert(1)</script>'
    injected["edges"][0]["target_label"] = "untrusted duplicate label"
    injected["secret"] = "must-not-project"
    mermaid = network_graph.render_graph(injected, "mermaid")
    graphviz = network_graph.render_graph(injected, "graphviz")
    json_value = json.loads(network_graph.render_graph(injected, "json"))
    assert "<script>" not in mermaid
    assert "&lt;script&gt;" in mermaid
    assert '\\"]-->' in graphviz
    assert json_value["edges"][0]["target_label"] == injected_node["label"]
    assert "secret" not in json_value

    private = deepcopy(value)
    private["nodes"][1]["visibility"] = "private"
    private["nodes"][1]["label"] = "Private real name"
    private["edges"][0]["target_label"] = "Private real name"
    assert "Private real name" not in network_graph.render_graph(private, "json")

    for mutation, match in (
        (lambda item: item.update({"root": "ari]-->evil"}), "guest_id"),
        (
            lambda item: item["nodes"][0].update({"target_candidate": "false"}),
            "must be a boolean",
        ),
        (
            lambda item: item["edges"][0].update({"target_guest_id": "unknown-guest"}),
            "unknown node",
        ),
    ):
        invalid = deepcopy(value)
        mutation(invalid)
        with pytest.raises(platform.PlatformError, match=match):
            network_graph.render_graph(invalid, "json")
    with pytest.raises(platform.PlatformError, match="format must be"):
        network_graph.render_graph(value, "html")
    conn.close()


def test_bounded_config_archive_and_guest_validation_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(platform.PlatformError, match="must be a list"):
        network_graph._mapping_items({"edges": "not-a-list"}, "edges")
    with monkeypatch.context() as bounded:
        bounded.setattr(network_graph, "_MAX_ARCHIVE_RECORDS", 0)
        with pytest.raises(platform.PlatformError, match="record limit"):
            network_graph._mapping_items({"edges": [{}]}, "edges")
    with pytest.raises(platform.PlatformError, match="entries must be objects"):
        network_graph._mapping_items({"edges": ["not-an-object"]}, "edges")

    invalid_helpers = (
        lambda: network_graph._label(1),
        lambda: network_graph._label("\x00hidden"),
        lambda: network_graph._label("person@example.test"),
        lambda: network_graph._label("+1 310-555-0199"),
        lambda: network_graph._edge_type(1),
        lambda: network_graph._relationship_class(1),
        lambda: network_graph._private_label_ref("owner://not-private-custody"),
        lambda: network_graph._guest_slug(1),
        lambda: network_graph._guest_slug("\x00hidden"),
        lambda: network_graph._resolve_guest(1, aliases={}, known_ids=set()),
        lambda: network_graph._resolve_guest(" ", aliases={}, known_ids=set()),
    )
    for invalid in invalid_helpers:
        with pytest.raises(platform.PlatformError):
            invalid()
    assert network_graph._guest_slug("é") == "guest-e"
    assert len(network_graph._guest_slug("Alpha " * 20)) <= 80
    assert network_graph._display_label("private-opaque-22", {}) == (
        "Private relationship"
    )

    missing = network_graph.load_network_config(
        tmp_path / "missing.yaml", tenant_id=TENANT, show_id=SHOW
    )
    assert missing == network_graph.NetworkConfig((), {}, frozenset(), ())

    config_path = tmp_path / "bounded.yaml"
    config_path.write_text("version: 2\n", encoding="utf-8")
    with monkeypatch.context() as bounded:
        bounded.setattr(network_graph, "_MAX_CONFIG_BYTES", 0)
        with pytest.raises(platform.PlatformError, match="size limit"):
            network_graph.load_network_config(
                config_path, tenant_id=TENANT, show_id=SHOW
            )

    malformed_configs: tuple[tuple[object, str], ...] = (
        (["not-an-object"], "must be an object"),
        ({"version": 2, "unknown": True}, "unsupported fields"),
        ({"version": 2, "edges": "not-a-list"}, "must be a list"),
        ({"version": 2, "edges": ["not-an-object"]}, "entries must be objects"),
        (
            {
                "version": 2,
                "aliases": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "guest_id": "private-public-22",
                        "visibility": "public",
                        "label": "Must stay private",
                    }
                ],
            },
            "reserved for private aliases",
        ),
        (
            {
                "version": 2,
                "aliases": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "guest_id": "public-22",
                        "visibility": "internal",
                        "label": "Public 22",
                    }
                ],
            },
            "visibility",
        ),
        (
            {
                "version": 2,
                "aliases": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "guest_id": "private-ref-22",
                        "visibility": "private",
                        "label_ref": "owner://bad-ref",
                    }
                ],
            },
            "label_ref",
        ),
        (
            {
                "version": 2,
                "aliases": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "guest_id": "same-22",
                        "visibility": "public",
                        "label": "First label",
                    },
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "guest_id": "same-22",
                        "visibility": "public",
                        "label": "Second label",
                    },
                ],
            },
            "conflicting definitions",
        ),
        (
            {
                "version": 2,
                "archive_sources": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "host_guest_id": "ari",
                        "path_ref": "resource://spec/not-data.json",
                        "provenance_ref": "public-rss://fixture/archive",
                        "required": False,
                    }
                ],
            },
            "stay under resource://data",
        ),
        (
            {
                "version": 2,
                "archive_sources": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "host_guest_id": "ari",
                        "path_ref": "resource://data/missing.json",
                        "provenance_ref": "public-rss://fixture/archive",
                        "required": "false",
                    }
                ],
            },
            "required must be a boolean",
        ),
    )
    for payload, match in malformed_configs:
        config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        with pytest.raises(platform.PlatformError, match=match):
            network_graph.load_network_config(
                config_path, tenant_id=TENANT, show_id=SHOW
            )
    config_path.write_text("aliases: [\n", encoding="utf-8")
    with pytest.raises(platform.PlatformError, match="could not be parsed"):
        network_graph.load_network_config(config_path, tenant_id=TENANT, show_id=SHOW)
    with pytest.raises(platform.PlatformError, match="tenant_id and show_id"):
        network_graph.load_config_edges(config_path)

    with pytest.raises(platform.PlatformError, match="cannot be relabeled"):
        network_graph._merge_alias_label(
            {
                "private-conflict-22": {
                    "label": "Private relationship",
                    "visibility": "private",
                }
            },
            {
                "guest_id": "private-conflict-22",
                "label": "Leaked label",
                "visibility": "public",
            },
        )
    with pytest.raises(platform.PlatformError, match="ambiguous"):
        network_graph._resolve_guest(
            "Same Label",
            aliases={
                "same-a": {"label": "Same Label", "visibility": "public"},
                "same-b": {"label": "Same Label", "visibility": "public"},
            },
            known_ids={"same-a", "same-b"},
        )

    archive_path = tmp_path / "bounded-archive.json"
    with pytest.raises(platform.PlatformError, match="unavailable"):
        network_graph.load_archive_edges(
            archive_path,
            tenant_id=TENANT,
            show_id=SHOW,
            host_guest_id="ari",
        )
    archive_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(platform.PlatformError, match="could not be parsed"):
        network_graph.load_archive_edges(
            archive_path,
            tenant_id=TENANT,
            show_id=SHOW,
            host_guest_id="ari",
        )
    archive_path.write_text("[]", encoding="utf-8")
    with monkeypatch.context() as bounded:
        bounded.setattr(network_graph, "_MAX_ARCHIVE_BYTES", 0)
        with pytest.raises(platform.PlatformError, match="size limit"):
            network_graph.load_archive_edges(
                archive_path,
                tenant_id=TENANT,
                show_id=SHOW,
                host_guest_id="ari",
            )
    archive_path.write_text(
        json.dumps(
            [
                {
                    "type": "guest",
                    "guest": "Host",
                    "date": "2026-01-01",
                    "confidence": "high",
                }
            ]
        ),
        encoding="utf-8",
    )
    edges, labels, targets = network_graph.load_archive_edges(
        archive_path,
        tenant_id=TENANT,
        show_id=SHOW,
        host_guest_id="ari",
    )
    assert edges == []
    assert labels == {"ari": "Host"}
    assert targets == set()
    archive_path.write_text(
        json.dumps([{"type": "guest", "guest": "No Date", "date": None}]),
        encoding="utf-8",
    )
    with pytest.raises(platform.PlatformError, match="ISO date"):
        network_graph.load_archive_edges(
            archive_path,
            tenant_id=TENANT,
            show_id=SHOW,
            host_guest_id="ari",
        )
    archive_path.write_text(
        json.dumps(
            [
                {
                    "type": "guest",
                    "guest": "Future Guest",
                    "date": "2026-08-11",
                    "confidence": "high",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(platform.PlatformError, match="cannot be in the future"):
        network_graph.load_archive_edges(
            archive_path,
            tenant_id=TENANT,
            show_id=SHOW,
            host_guest_id="ari",
            effective_date=date(2026, 8, 10),
        )


def test_archive_alias_provenance_and_csv_cells_fail_closed(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive-safety.json"
    archive_path.write_text(
        json.dumps(
            [
                {
                    "type": "guest",
                    "guest": "John Smith",
                    "date": "2026-08-01",
                    "confidence": "high",
                }
            ]
        ),
        encoding="utf-8",
    )
    long_provenance = "owner://" + "a" * 192
    edges, _, _ = network_graph.load_archive_edges(
        archive_path,
        tenant_id=TENANT,
        show_id=SHOW,
        host_guest_id="ari",
        provenance_ref=long_provenance,
        effective_date=date(2026, 8, 10),
    )
    assert len(edges[0]["provenance_ref"]) <= 200
    assert edges[0]["provenance_ref"].endswith(
        "edge-" + edges[0]["provenance_ref"][-20:]
    )

    config_path = tmp_path / "alias-conflict.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "aliases": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "guest_id": "john-smith",
                        "visibility": "public",
                        "label": "Jane Smith",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conn = store.connect(tmp_path / "alias-conflict.sqlite3")
    with pytest.raises(
        platform.PlatformError, match="conflicts with a configured alias"
    ):
        network_graph.graph(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            root_guest_id="ari",
            config_path=config_path,
            archive_path=archive_path,
        )

    value = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="ari",
        archive_path=archive_path,
    )
    value["nodes"][0]["label"] = "=FORMULA()"
    value["nodes"][0]["target_candidate"] = True
    rows = list(csv.DictReader(io.StringIO(network_graph.render_graph(value, "csv"))))
    source_row = next(row for row in rows if row["source_id"] == "ari")
    assert source_row["source_label"] == "'=FORMULA()"
    assert source_row["target_candidate"] == "True"
    conn.close()


def test_archive_deduplicates_appearance_ids_and_hashes_unicode_only_names(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "archive-identities.json"
    duplicate = {
        "episode_no": "one",
        "title": "One appearance",
        "date": "2026-08-01",
        "type": "guest",
        "guest": "单次嘉宾",
        "confidence": "low",
    }
    archive_path.write_text(
        json.dumps(
            [
                duplicate,
                duplicate,
                {
                    "episode_no": "two",
                    "title": "Different guest",
                    "date": "2026-08-02",
                    "type": "guest",
                    "guest": "另一位嘉宾",
                    "confidence": "low",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    edges, labels, targets = network_graph.load_archive_edges(
        archive_path,
        tenant_id=TENANT,
        show_id=SHOW,
        host_guest_id="ari",
        effective_date=date(2026, 8, 10),
    )

    assert len(edges) == 2
    assert len(labels) == 2
    assert len(set(labels)) == 2
    assert all(guest_id.startswith("guest-") for guest_id in labels)
    assert all(edge["relationship_class"] == "C1" for edge in edges)
    assert targets == set()


@pytest.mark.parametrize("schema_version", [None, True, 2, "1"])
def test_renderers_reject_missing_or_unsupported_schema_versions(
    tmp_path: Path, schema_version: object
) -> None:
    conn = store.connect(tmp_path / "schema-version.sqlite3")
    value = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="ari",
    )
    conn.close()
    if schema_version is None:
        value.pop("schema_version")
    else:
        value["schema_version"] = schema_version

    with pytest.raises(platform.PlatformError, match="schema_version must be 1"):
        network_graph.render_graph(value, "json")


def test_legacy_edges_are_visible_but_new_writes_are_canonical(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "legacy.sqlite3")
    with pytest.raises(platform.PlatformError, match="relationship-map taxonomy"):
        platform.add_relationship_edge(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            source_guest_id="ari",
            target_guest_id="legacy-guest",
            edge_type="collaborated",
            relationship_class="C2",
            provenance_ref="owner://legacy/write",
        )
    conn.execute(
        "INSERT INTO relationship_edges "
        "(id, tenant_id, show_id, source_guest_id, target_guest_id, edge_type, "
        "relationship_class, provenance_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "relationship-edge-legacy-22",
            TENANT,
            SHOW,
            "ari",
            "legacy-guest",
            "collaborated",
            "C2",
            "owner://legacy/stored",
            "2026-08-01T12:00:00+00:00",
        ),
    )
    conn.commit()
    value = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="ari",
        config_path=tmp_path / "missing.yaml",
    )
    assert value["edges"][0]["edge_type"] == network_graph.LEGACY_EDGE_TYPE
    assert "legacy unspecified" in network_graph.render_graph(value, "mermaid")

    legacy_config = tmp_path / "legacy-v1.yaml"
    legacy_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "edges": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "source_guest_id": "ari",
                        "target_guest_id": "legacy-config-guest",
                        "edge_type": "collaborated",
                        "relationship_class": "C2",
                        "provenance_ref": "owner://legacy/config",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = network_graph.load_network_config(
        legacy_config, tenant_id=TENANT, show_id=SHOW
    )
    assert loaded.edges[0]["edge_type"] == network_graph.LEGACY_EDGE_TYPE
    conn.close()


def test_unaliased_contact_like_ids_fail_before_the_graph_is_returned(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "contact-like-id.sqlite3")
    platform.add_relationship_edge(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        source_guest_id="ari",
        target_guest_id="212-555-1212",
        edge_type="knows",
        relationship_class="C2",
        provenance_ref="owner://network/contact-like-id",
    )
    with pytest.raises(
        platform.PlatformError, match="unaliased guest label must not contain"
    ) as error:
        network_graph.graph(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            root_guest_id="ari",
            config_path=tmp_path / "missing.yaml",
        )
    assert "212" not in str(error.value)
    conn.close()


def test_writer_rejects_self_edges_and_legacy_rows_do_not_break_reads(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "self-edge.sqlite3")
    with pytest.raises(platform.PlatformError, match="cannot point to themselves"):
        platform.add_relationship_edge(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            source_guest_id="ari",
            target_guest_id="ari",
            edge_type="knows",
            relationship_class="C2",
            provenance_ref="owner://network/rejected-self-edge",
        )
    conn.execute(
        "INSERT INTO relationship_edges "
        "(id, tenant_id, show_id, source_guest_id, target_guest_id, edge_type, "
        "relationship_class, provenance_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "relationship-edge-legacy-self-22",
            TENANT,
            SHOW,
            "ari",
            "ari",
            "knows",
            "C2",
            "owner://network/legacy-self-edge",
            "2026-08-01T12:00:00+00:00",
        ),
    )
    conn.commit()
    value = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="ari",
        config_path=tmp_path / "missing.yaml",
    )
    assert value["nodes"] == [
        {
            "id": "ari",
            "label": "Host",
            "visibility": "public",
            "distance": 0,
            "relationship_class": None,
            "target_candidate": False,
            "on_target_path": False,
        }
    ]
    assert value["edges"] == []
    conn.close()


def test_graph_boundaries_and_projection_rejection_matrix(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "projection.sqlite3")
    config_path = _config(tmp_path / "projection.yaml", include_private=True)
    value = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="Theo Von",
        depth=3,
        config_path=config_path,
    )
    for depth in (True, -1, 6, "2"):
        with pytest.raises(platform.PlatformError, match="depth"):
            network_graph.graph(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                root_guest_id="Theo Von",
                depth=depth,  # type: ignore[arg-type]
                config_path=config_path,
            )

    optional_path = tmp_path / "optional.yaml"
    optional_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "archive_sources": [
                    {
                        "tenant_id": TENANT,
                        "show_id": SHOW,
                        "host_guest_id": "ari",
                        "path_ref": "resource://data/missing-optional.json",
                        "provenance_ref": "public-rss://fixture/archive",
                        "required": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    empty = network_graph.graph(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        root_guest_id="root-22",
        depth=0,
        config_path=optional_path,
    )
    assert empty["summary"]["edge_count"] == 0
    assert network_graph.render_graph(empty, "csv").count("\n") == 1
    assert "linkStyle" not in network_graph.render_graph(empty, "mermaid")
    assert network_graph.render_graph(empty, "graphviz").endswith("}")

    invalid_values: list[object] = [[], {**value, "depth": True}]
    for mutation in (
        lambda item: item.update({"nodes": "not-a-list"}),
        lambda item: item["nodes"].__setitem__(0, "not-an-object"),
        lambda item: item["nodes"][0].update({"visibility": "secret"}),
        lambda item: item["nodes"][0].update({"distance": True}),
        lambda item: item["nodes"].append(deepcopy(item["nodes"][0])),
        lambda item: item.update({"root": "missing-root"}),
        lambda item: item["edges"].__setitem__(0, "not-an-object"),
        lambda item: item["edges"].append(deepcopy(item["edges"][0])),
        lambda item: item["edges"][0].update({"id": "owner://not-generated"}),
        lambda item: item["edges"][0].update({"source_kind": "secret"}),
        lambda item: item["edges"][0].update({"distance": True}),
        lambda item: item.update({"target_paths": "not-a-list"}),
        lambda item: item["target_paths"].__setitem__(0, "not-an-object"),
        lambda item: item["target_paths"][0].update({"target_id": "unknown-22"}),
        lambda item: item["target_paths"][0].update({"node_ids": []}),
        lambda item: item["target_paths"][0].update(
            {"edge_ids": [item["target_paths"][-1]["edge_ids"][-1]]}
        ),
    ):
        invalid = deepcopy(value)
        mutation(invalid)
        invalid_values.append(invalid)
    for invalid in invalid_values:
        with pytest.raises(platform.PlatformError):
            network_graph.render_graph(invalid, "json")  # type: ignore[arg-type]

    unhighlighted = deepcopy(value)
    unhighlighted["target_paths"] = []
    for node in unhighlighted["nodes"]:
        node["target_candidate"] = False
        node["on_target_path"] = False
    for edge in unhighlighted["edges"]:
        edge["on_target_path"] = False
    assert "linkStyle" not in network_graph.render_graph(unhighlighted, "mermaid")
    assert "penwidth=3" not in network_graph.render_graph(unhighlighted, "dot")
    conn.close()


@pytest.mark.skipif(TestClient is None, reason="api extra is not installed")
def test_cli_api_dashboard_and_show_identity_use_the_same_scoped_projection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "api.sqlite3"
    conn = store.connect(database)
    platform.register_show(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        label="Issue 22",
        config_ref="config://issue-22",
    )
    platform.register_show(
        conn,
        tenant_id=TENANT,
        show_id=OTHER_SHOW,
        label="Other 22",
        config_ref="config://other-22",
    )
    platform.add_relationship_edge(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        source_guest_id="theo-von",
        target_guest_id="mark-normand",
        edge_type="knows",
        relationship_class="C2",
        provenance_ref="owner://network/api",
    )
    platform.add_relationship_edge(
        conn,
        tenant_id=TENANT,
        show_id=OTHER_SHOW,
        source_guest_id="theo-von",
        target_guest_id="other-secret",
        edge_type="knows",
        relationship_class="C1",
        provenance_ref="owner://network/other-show",
    )
    conn.close()

    config_path = _config(tmp_path / "cli.yaml")
    archive_path = _archive(tmp_path / "cli-archive.json")
    assert (
        main(
            [
                "network-map",
                "--guest",
                "Theo Von",
                "--depth",
                "2",
                "--format",
                "csv",
                "--db",
                str(database),
                "--tenant",
                TENANT,
                "--show",
                SHOW,
                "--config",
                str(config_path),
                "--archive",
                str(archive_path),
                "--target",
                "Mark Normand",
            ]
        )
        == 0
    )
    cli_rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert cli_rows[0]["source_id"] == "theo-von"
    assert cli_rows[0]["target_id"] == "mark-normand"

    token = "synthetic-network-map-token-issue-22"  # allow-secret: fixture
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {
                token: ("network_operator_22", "producer", TENANT)  # allow-secret: fixture
            }
        ),
    )
    with TestClient(app) as client:
        response = client.get(
            f"/v1/shows/{SHOW}/network-map?guest=Theo%20Von&target=Mark%20Normand",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, private"
        assert response.json()["target_paths"][0]["target_id"] == "mark-normand"
        assert "other-secret" not in response.text
        csv_response = client.get(
            f"/v1/shows/{SHOW}/network-map?guest=Theo%20Von&target=Mark%20Normand&format=csv",
            headers=headers,
        )
        assert csv_response.status_code == 200
        assert csv_response.headers["cache-control"] == "no-store, private"
        assert csv_response.text.startswith("tenant_id,show_id,root_id")
        uppercase_json = client.get(
            f"/v1/shows/{SHOW}/network-map?guest=Theo%20Von&format=JSON",
            headers=headers,
        )
        assert uppercase_json.status_code == 200
        assert uppercase_json.headers["content-type"].startswith("application/json")
        mixed_csv = client.get(
            f"/v1/shows/{SHOW}/network-map?guest=Theo%20Von&format=Csv",
            headers=headers,
        )
        assert mixed_csv.status_code == 200
        assert mixed_csv.headers["content-type"].startswith("text/csv")
        assert (
            client.get(
                f"/v1/shows/{SHOW}/network-map?guest=Theo%20Von&format=html",
                headers=headers,
            ).status_code
            == 422
        )

    class ScopedAuthenticator:
        def authenticate(self, conn, assertion, requested_show=None):
            del conn, assertion, requested_show
            return authentication.AuthenticatedOperator(
                actor_id="network_operator_22",
                role=service.HumanRole.PRODUCER,
                tenant_id=TENANT,
                show_id=SHOW,
                provider="synthetic_show_scope",
                subject_digest="2" * 64,
            )

    scoped_app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        csrf_secret=b"synthetic-csrf-secret-issue-22-32",  # allow-secret: fixture
        access_authenticator=ScopedAuthenticator(),
    )
    with TestClient(scoped_app) as client:
        assert (
            client.get(f"/v1/shows/{SHOW}/network-map?guest=theo-von").status_code
            == 200
        )
        assert (
            client.get(f"/v1/shows/{OTHER_SHOW}/network-map?guest=theo-von").status_code
            == 403
        )


def test_schema_resource_dashboard_and_documentation_contracts_are_complete() -> None:
    for relative in (
        "config/network_edges.yaml",
        "config/domain_kernel.yaml",
        "dashboard/assets/app.js",
        "dashboard/index.html",
        "dashboard/assets/api.js",
        "dashboard/assets/partnership.js",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/partnership-shell.js",
        "dashboard/assets/styles.css",
        "spec/network-map.schema.json",
    ):
        assert (
            Path(relative).read_bytes()
            == (Path("hospes/resources") / relative).read_bytes()
        )

    schema = json.loads(Path("spec/network-map.schema.json").read_text())
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["edge"]["additionalProperties"] is False
    assert {"mermaid", "graphviz", "json", "csv"} == (
        network_graph.EXPORT_FORMATS - {"dot"}
    )
    config = yaml.safe_load(Path("config/network_edges.yaml").read_text())
    assert config["version"] == 2
    assert all({"tenant_id", "show_id"} <= set(item) for item in config["edges"])
    assert all({"tenant_id", "show_id"} <= set(item) for item in config["aliases"])
    assert config["archive_sources"][0]["required"] is False

    html_text = "\n".join(
        Path(relative).read_text()
        for relative in (
            "dashboard/index.html",
            "dashboard/assets/partnership-workspace.html",
        )
    )
    api_javascript = Path("dashboard/assets/api.js").read_text()
    javascript = "\n".join(
        Path(relative).read_text()
        for relative in (
            "dashboard/assets/partnership.js",
            "dashboard/assets/partnership-workspace.js",
        )
    )
    queue_javascript = Path("dashboard/assets/app.js").read_text()
    shell_javascript = Path("dashboard/assets/partnership-shell.js").read_text()
    assert 'id="relationship-map-panel"' in html_text
    assert 'id="network-map-form"' in html_text
    assert "loadNetworkMap" in api_javascript
    assert "encodeURIComponent(showId)" in api_javascript
    assert "path.node_ids.map" in javascript
    assert "labels.map(escapeHTML)" in javascript
    assert "path.edge_ids.map(escapeHTML)" in javascript
    assert "requestVersion !== networkRequestVersion" in javascript
    assert "current?.show_id !== showId" in javascript
    assert "const logoutCsrf" in javascript
    assert "const logoutCsrf" not in queue_javascript
    assert "initializeLogoutCsrf" in shell_javascript
    assert "import('./partnership.js')" in shell_javascript

    docs = Path("docs/network-map.md").read_text()
    readme = Path("README.md").read_text()
    domain = Path("config/domain_kernel.yaml").read_text()
    for phrase in (
        "tenant and show",
        "Private relationship",
        "deterministic shortest paths",
        "cannot send, book, consent, publish",
        "Mermaid",
        "Graphviz",
        "CSV",
    ):
        assert phrase in docs
    assert 'network-map --guest "Theo Von"' in readme
    assert "relationship_map_contract:" in domain
    assert "exports always replace their label with Private relationship" in domain
    assert schema["$defs"]["edge"]["properties"]["relationship_strength"][
        "enum"
    ] == list(network_graph.RELATIONSHIP_STRENGTH.values())
    assert set(
        schema["$defs"]["edge"]["properties"]["edge_type"]["enum"]
    ) == network_graph.PROJECTED_EDGE_TYPES
