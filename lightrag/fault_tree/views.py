"""Bounded graph projections over one fault-tree workspace."""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from lightrag.fault_tree.persistence import (
    DOMAIN_PROPERTIES_JSON,
    RELATION_PROPERTIES_JSON,
    decode_json_property,
)

GRAPH_VIEW_IDS = (
    "department-files",
    "department-equipment",
    "equipment-faults",
    "fault-causes",
)


@dataclass(frozen=True)
class GraphViewSpec:
    anchor_class: str
    target_class: str
    relation_codes: frozenset[str]
    requires_root: bool = False


GRAPH_VIEW_SPECS = {
    "department-files": GraphViewSpec(
        anchor_class="Department",
        target_class="File",
        relation_codes=frozenset({"OWNS_FILE"}),
    ),
    "department-equipment": GraphViewSpec(
        anchor_class="Department",
        target_class="Equipment",
        relation_codes=frozenset({"HAS_EQUIPMENT"}),
    ),
    "equipment-faults": GraphViewSpec(
        anchor_class="Equipment",
        target_class="FaultEvent",
        relation_codes=frozenset({"HAS_FAULT"}),
    ),
    "fault-causes": GraphViewSpec(
        anchor_class="FaultEvent",
        target_class="FaultEvent",
        relation_codes=frozenset(
            {"CAUSES", "HAS_CAUSE_RULE", "INPUT_OF", "OUTPUT_TO"}
        ),
        requires_root=True,
    ),
}

_CAUSAL_UPSTREAM_RELATIONS = frozenset({"CAUSES", "INPUT_OF", "OUTPUT_TO"})
_CANONICAL_CLASSES = {
    "department": "Department",
    "file": "File",
    "equipment": "Equipment",
    "faultevent": "FaultEvent",
    "faultcauserule": "FaultCauseRule",
    "logicgate": "LogicGate",
}
_KNOWN_RELATION_CODES = frozenset(
    {
        "CAUSES",
        "HAS_FAULT",
        "HAS_EQUIPMENT",
        "OWNS_FILE",
        "DESCRIBES_EQUIPMENT",
        "PROVIDES_EVIDENCE_FOR",
        "HAS_CAUSE_RULE",
        "INPUT_OF",
        "OUTPUT_TO",
    }
)


class GraphViewError(ValueError):
    """A graph-view request that cannot be satisfied."""


async def get_graph_view(
    rag,
    *,
    view_id: str,
    root: str | None,
    max_depth: int,
    max_nodes: int,
) -> dict[str, Any]:
    """Load this workspace's graph and build one of the four fixed projections.

    The MVP reads the complete workspace graph because backends expose neither
    typed-edge queries nor class filters through one common interface. The
    projection itself is bounded and deterministic; a backend-native query can
    replace the two ``get_all_*`` calls without changing the API contract.
    """

    if view_id not in GRAPH_VIEW_SPECS:
        raise GraphViewError(f"unknown graph view {view_id!r}")
    if max_depth < 1:
        raise GraphViewError("max_depth must be at least 1")
    if max_nodes < 1:
        raise GraphViewError("max_nodes must be at least 1")

    root_label = (root or "").strip()
    if GRAPH_VIEW_SPECS[view_id].requires_root and not root_label:
        raise GraphViewError(f"graph view {view_id!r} requires root")

    workspace = getattr(rag, "workspace", "") or ""
    if view_id == "department-files":
        documents, documents_truncated = await _load_workspace_documents(
            rag, max_nodes=max_nodes
        )
        return build_graph_view(
            [],
            [],
            documents=documents,
            view_id=view_id,
            max_depth=max_depth,
            max_nodes=max_nodes,
            workspace=workspace,
            documents_truncated=documents_truncated,
        )

    storage = rag.chunk_entity_relation_graph
    nodes = await storage.get_all_nodes()
    if view_id == "department-equipment":
        return build_graph_view(
            nodes,
            [],
            view_id=view_id,
            max_depth=max_depth,
            max_nodes=max_nodes,
            workspace=workspace,
        )
    edges = await storage.get_all_edges()
    return build_graph_view(
        nodes,
        edges,
        view_id=view_id,
        root=root_label or None,
        max_depth=max_depth,
        max_nodes=max_nodes,
        workspace=workspace,
    )


def build_graph_view(
    nodes: Iterable[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]],
    *,
    view_id: str,
    root: str | None = None,
    max_depth: int = 2,
    max_nodes: int = 1000,
    workspace: str = "",
    documents: Iterable[Mapping[str, Any]] = (),
    documents_truncated: bool = False,
) -> dict[str, Any]:
    """Build a graph view from storage rows without changing the stored graph."""

    if view_id not in GRAPH_VIEW_SPECS:
        raise GraphViewError(f"unknown graph view {view_id!r}")

    node_by_id = {
        str(node["id"]): dict(node)
        for node in nodes
        if node.get("id") not in (None, "")
    }
    normalized_edges = [
        edge
        for edge in (_normalize_edge(edge, node_by_id) for edge in edges)
        if edge is not None
    ]

    selected_node_rows: dict[str, Mapping[str, Any]] | None = None
    if view_id == "department-files":
        selected_node_rows, selected_edges, truncated = (
            _build_department_files_view(
                documents,
                workspace=workspace,
                max_nodes=max_nodes,
            )
        )
        truncated = truncated or documents_truncated
    elif view_id == "department-equipment":
        selected_node_rows, selected_edges, truncated = (
            _build_department_equipment_view(
                node_by_id,
                workspace=workspace,
                max_nodes=max_nodes,
            )
        )
    elif view_id == "fault-causes":
        selected_nodes, selected_edges, truncated = _build_causal_view(
            node_by_id,
            normalized_edges,
            root=root,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    else:
        selected_nodes, selected_edges, truncated = _build_direct_view(
            node_by_id,
            normalized_edges,
            GRAPH_VIEW_SPECS[view_id],
            root=root,
            max_nodes=max_nodes,
        )

    public_nodes = (
        [_public_node(node) for node in selected_node_rows.values()]
        if selected_node_rows is not None
        else [_public_node(node_by_id[node_id]) for node_id in selected_nodes]
    )
    public_nodes.sort(key=lambda node: str(node["id"]))

    return {
        "view": view_id,
        "workspace": workspace,
        "root": root,
        "nodes": public_nodes,
        "edges": [_public_edge(edge) for edge in selected_edges],
        "is_truncated": truncated,
    }


def _build_department_files_view(
    documents: Iterable[Mapping[str, Any]],
    *,
    workspace: str,
    max_nodes: int,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], bool]:
    department_id = _workspace_department_id(workspace)
    node_by_id: dict[str, dict[str, Any]] = {
        department_id: _department_node(workspace)
    }
    edge_rows: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    truncated = False

    for document in documents:
        file_path = document.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            continue
        doc_id = str(document.get("id") or file_path)
        file_id = f"file:{doc_id}"
        if file_id in seen_files or len(node_by_id) >= max_nodes:
            truncated = True
            continue
        seen_files.add(file_id)
        node_by_id[file_id] = {
            "id": file_id,
            "entity_id": file_id,
            "entity_type": "File",
            "class_code": "File",
            "canonical_name": file_path,
            "file_path": file_path,
            "document_id": doc_id,
            "parse_status": _enum_value(document.get("status")),
            "chunks_count": document.get("chunks_count", 0),
            "content_summary": document.get("content_summary", ""),
            "created_at": document.get("created_at", ""),
            "updated_at": document.get("updated_at", ""),
        }
        edge_rows.append(
            {
                "id": _stable_edge_id(department_id, "OWNS_FILE", file_id),
                "source": department_id,
                "target": file_id,
                "relation_code": "OWNS_FILE",
            }
        )

    return node_by_id, _sort_edges(edge_rows), truncated


def _build_department_equipment_view(
    node_by_id: Mapping[str, Mapping[str, Any]],
    *,
    workspace: str,
    max_nodes: int,
) -> tuple[dict[str, Mapping[str, Any]], list[dict[str, Any]], bool]:
    department_id = _workspace_department_id(workspace)
    selected: dict[str, Mapping[str, Any]] = {
        department_id: _department_node(workspace)
    }
    equipment_ids = sorted(
        node_id
        for node_id, node in node_by_id.items()
        if _node_class(node) == "Equipment"
    )
    truncated = False
    for equipment_id in equipment_ids:
        if len(selected) >= max_nodes:
            truncated = True
            break
        selected[equipment_id] = node_by_id[equipment_id]

    edges = [
        {
            "id": _stable_edge_id(department_id, "HAS_EQUIPMENT", equipment_id),
            "source": department_id,
            "target": equipment_id,
            "relation_code": "HAS_EQUIPMENT",
        }
        for equipment_id in sorted(selected)
        if equipment_id != department_id
    ]
    return selected, _sort_edges(edges), truncated


def _build_direct_view(
    node_by_id: Mapping[str, Mapping[str, Any]],
    edges: list[dict[str, Any]],
    spec: GraphViewSpec,
    *,
    root: str | None,
    max_nodes: int,
) -> tuple[list[str], list[dict[str, Any]], bool]:
    if root and root not in node_by_id:
        raise GraphViewError(f"graph view root {root!r} was not found")

    matching: list[tuple[str, str, dict[str, Any]]] = []
    for edge in edges:
        if edge["relation_code"] not in spec.relation_codes:
            continue
        oriented = _orient_for_classes(
            edge, spec.anchor_class, spec.target_class, node_by_id
        )
        if oriented is None:
            continue
        source, target = oriented
        if root and source != root:
            continue
        matching.append(
            (
                source,
                target,
                {**edge, "source": source, "target": target},
            )
        )

    matching.sort(
        key=lambda item: (
            item[0],
            item[1],
            str(item[2]["relation_code"]),
        )
    )
    selected_node_ids = _bounded_node_ids(matching, max_nodes)
    selected_edges = [
        edge
        for source, target, edge in matching
        if source in selected_node_ids and target in selected_node_ids
    ]
    truncated = len(selected_node_ids) < len(
        {node_id for pair in matching for node_id in pair[:2]}
    )
    return (
        sorted(selected_node_ids),
        _sort_edges(selected_edges),
        truncated,
    )


def _build_causal_view(
    node_by_id: Mapping[str, Mapping[str, Any]],
    edges: list[dict[str, Any]],
    *,
    root: str | None,
    max_depth: int,
    max_nodes: int,
) -> tuple[list[str], list[dict[str, Any]], bool]:
    if not root or root not in node_by_id:
        raise GraphViewError(f"graph view root {root!r} was not found")
    if _node_class(node_by_id[root]) != "FaultEvent":
        raise GraphViewError("fault-causes root must be a FaultEvent")

    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        if edge["relation_code"] not in GRAPH_VIEW_SPECS["fault-causes"].relation_codes:
            continue
        oriented = _orient_causal_edge(edge, node_by_id)
        if oriented is None:
            continue
        source, target = oriented
        incoming[target].append(edge)
        outgoing[source].append(edge)

    selected_node_ids: set[str] = {root}
    selected_edge_ids: set[str] = set()
    selected_edges: list[dict[str, Any]] = []
    queued: set[str] = {root}
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    truncated = False

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            if any(
                edge["relation_code"] in _CAUSAL_UPSTREAM_RELATIONS
                for edge in incoming.get(current, [])
            ):
                truncated = True
            continue

        # Rules belong to their effect and are not causal inputs. Keep this
        # traversal one-way so a selected fault never expands into unrelated
        # faults for which it is merely a cause.
        for edge in outgoing.get(current, []):
            if edge["relation_code"] != "HAS_CAUSE_RULE":
                continue
            oriented = _orient_causal_edge(edge, node_by_id)
            if oriented is None:
                continue
            source, target = oriented
            if source != current or target not in node_by_id:
                continue
            if not _admit_node(target, selected_node_ids, max_nodes):
                truncated = True
                continue
            selected_node_ids.add(target)
            _append_edge(
                {**edge, "source": source, "target": target},
                selected_edge_ids,
                selected_edges,
            )
            _set_view_depth(node_by_id[target], depth + 1)

        for edge in incoming.get(current, []):
            relation = edge["relation_code"]
            if relation not in _CAUSAL_UPSTREAM_RELATIONS:
                continue
            oriented = _orient_causal_edge(edge, node_by_id)
            if oriented is None:
                continue
            source, target = oriented
            if target != current or source not in node_by_id:
                continue
            if not _admit_node(source, selected_node_ids, max_nodes):
                truncated = True
                continue
            selected_node_ids.add(source)
            _append_edge(
                {**edge, "source": source, "target": target},
                selected_edge_ids,
                selected_edges,
            )
            _set_view_depth(node_by_id[source], depth + 1)
            if source not in queued:
                queued.add(source)
                queue.append((source, depth + 1))

    return (
        sorted(selected_node_ids),
        _sort_edges(selected_edges),
        truncated,
    )


def _normalize_edge(
    edge: Mapping[str, Any],
    node_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    source = edge.get("source")
    target = edge.get("target")
    if not isinstance(source, str) or not isinstance(target, str):
        return None
    if source not in node_by_id or target not in node_by_id:
        return None

    relation_code = _edge_relation_code(edge)
    if not relation_code:
        return None
    normalized = dict(edge)
    normalized["source"] = source
    normalized["target"] = target
    normalized["relation_code"] = relation_code
    normalized["id"] = str(
        edge.get("id")
        or _stable_edge_id(source, relation_code, target)
    )
    return normalized


def _edge_relation_code(edge: Mapping[str, Any]) -> str | None:
    explicit = edge.get("relation_code")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    keywords = edge.get("keywords")
    if not isinstance(keywords, str):
        return None
    for token in keywords.replace("，", ",").split(","):
        candidate = token.strip()
        if candidate in _KNOWN_RELATION_CODES:
            return candidate
    return None


def _orient_for_classes(
    edge: Mapping[str, Any],
    source_class: str,
    target_class: str,
    node_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str] | None:
    source = edge["source"]
    target = edge["target"]
    if (
        _node_class(node_by_id[source]) == source_class
        and _node_class(node_by_id[target]) == target_class
    ):
        return source, target
    if (
        _node_class(node_by_id[target]) == source_class
        and _node_class(node_by_id[source]) == target_class
    ):
        return target, source
    return None


def _orient_causal_edge(
    edge: Mapping[str, Any],
    node_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str] | None:
    relation = edge["relation_code"]
    source_class = _node_class(node_by_id[edge["source"]])
    target_class = _node_class(node_by_id[edge["target"]])

    allowed = {
        "CAUSES": ({"FaultEvent"}, {"FaultEvent"}),
        "HAS_CAUSE_RULE": ({"FaultEvent"}, {"FaultCauseRule"}),
        "INPUT_OF": ({"FaultEvent", "LogicGate"}, {"LogicGate"}),
        "OUTPUT_TO": ({"LogicGate"}, {"FaultEvent", "LogicGate"}),
    }.get(relation)
    if allowed is None:
        return None

    expected_sources, expected_targets = allowed
    if source_class in expected_sources and target_class in expected_targets:
        return edge["source"], edge["target"]
    if target_class in expected_sources and source_class in expected_targets:
        return edge["target"], edge["source"]
    return None


def _node_class(node: Mapping[str, Any]) -> str | None:
    value = node.get("class_code") or node.get("entity_type")
    if not isinstance(value, str):
        return None
    compact = "".join(value.split()).casefold()
    return _CANONICAL_CLASSES.get(compact, value)


def _bounded_node_ids(
    edges: list[tuple[str, str, dict[str, Any]]], max_nodes: int
) -> set[str]:
    selected: set[str] = set()
    for source, target, _edge in edges:
        for node_id in (source, target):
            if len(selected) >= max_nodes:
                return selected
            selected.add(node_id)
    return selected


def _admit_node(
    node_id: str, selected_node_ids: set[str], max_nodes: int
) -> bool:
    return node_id in selected_node_ids or len(selected_node_ids) < max_nodes


def _append_edge(
    edge: Mapping[str, Any],
    selected_edge_ids: set[str],
    destination: list[dict[str, Any]],
) -> None:
    edge_id = str(edge["id"])
    if edge_id in selected_edge_ids:
        return
    selected_edge_ids.add(edge_id)
    destination.append(dict(edge))


def _set_view_depth(node: Mapping[str, Any], depth: int) -> None:
    if isinstance(node, dict):
        node["_view_depth"] = depth


def _public_node(node: Mapping[str, Any]) -> dict[str, Any]:
    node_id = str(node["id"])
    properties = dict(node)
    properties.pop("id", None)
    if DOMAIN_PROPERTIES_JSON in properties:
        properties["domain_properties"] = decode_json_property(
            properties.pop(DOMAIN_PROPERTIES_JSON)
        )
    class_code = _node_class(node)
    if class_code:
        properties["class_code"] = class_code
    display_name = (
        properties.get("canonical_name")
        or properties.get("entity_id")
        or node_id
    )
    return {
        "id": node_id,
        "labels": [str(display_name)],
        "properties": properties,
    }


def _public_edge(edge: Mapping[str, Any]) -> dict[str, Any]:
    properties = {
        key: value
        for key, value in edge.items()
        if key not in {"id", "source", "target", "relation_code"}
    }
    if RELATION_PROPERTIES_JSON in properties:
        properties["relation_properties"] = decode_json_property(
            properties.pop(RELATION_PROPERTIES_JSON)
        )
    return {
        "id": str(edge["id"]),
        "type": str(edge["relation_code"]),
        "source": str(edge["source"]),
        "target": str(edge["target"]),
        "properties": {
            **properties,
            "relation_code": str(edge["relation_code"]),
        },
    }


def _sort_edges(edges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        edges,
        key=lambda edge: (
            str(edge["source"]),
            str(edge["relation_code"]),
            str(edge["target"]),
        ),
    )


def _stable_edge_id(source: str, relation_code: str, target: str) -> str:
    digest = hashlib.sha1(
        "\x1f".join((source, relation_code, target)).encode("utf-8")
    ).hexdigest()[:16]
    return f"rel-{digest}"


async def _load_workspace_documents(
    rag, *, max_nodes: int
) -> tuple[list[dict[str, Any]], bool]:
    doc_status = getattr(rag, "doc_status", None)
    if doc_status is None:
        return [], False
    page_size = max(10, min(max_nodes, 200))
    rows, _total = await doc_status.get_docs_paginated(
        page=1,
        page_size=page_size,
        sort_field="updated_at",
        sort_direction="desc",
    )
    documents: list[dict[str, Any]] = []
    for doc_id, row in rows:
        if hasattr(row, "model_dump"):
            values = row.model_dump()
        elif hasattr(row, "__dict__"):
            values = {
                key: value
                for key, value in vars(row).items()
                if not key.startswith("_")
            }
        else:
            values = dict(row)
        values["id"] = doc_id
        documents.append(values)
    return documents, _total > len(rows)


def _workspace_department_id(workspace: str) -> str:
    return f"department:{workspace or 'default'}"


def _department_node(workspace: str) -> dict[str, Any]:
    name = workspace or "default"
    return {
        "id": _workspace_department_id(workspace),
        "entity_id": _workspace_department_id(workspace),
        "entity_type": "Department",
        "class_code": "Department",
        "canonical_name": name,
        "workspace": workspace,
        "virtual": True,
    }


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)
