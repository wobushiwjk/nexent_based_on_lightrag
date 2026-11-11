"""Project validated fault candidates into a property-graph representation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from pydantic import BaseModel, Field

from lightrag.fault_tree.contracts import (
    DomainBundle,
    EntityRecord,
    FaultTreeDraft,
    FileAsset,
)
from lightrag.fault_tree.schemas import Expression, FaultCandidateBatch


class GraphProjection(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


def project_candidate_batch(
    batch: FaultCandidateBatch,
    resolve_uid: Callable[[str, str], str] | None = None,
) -> GraphProjection:
    """Turn validated candidates into graph-shaped nodes and edges.

    ``resolve_uid`` maps ``(class_code, local_id)`` to the durable ID used by
    the target graph store. Without a resolver the local ID is retained, which
    is useful for previews and tests.
    """

    resolve_uid = resolve_uid or (lambda _class_code, local_id: local_id)
    projection = GraphProjection()
    local_to_uid: dict[str, str] = {}

    for entity in batch.entities:
        uid = resolve_uid(entity.class_name, entity.local_id)
        local_to_uid[entity.local_id] = uid
        properties = dict(entity.properties)
        for field_name, value in entity.model_extra.items():
            if field_name not in {"local_id", "class", "properties", "evidence"}:
                properties[field_name] = value
        properties["canonical_name"] = (
            properties.get("name")
            or properties.get("canonical_name")
            or entity.local_id
        )
        if entity.ontology_version:
            properties["ontology_version"] = entity.ontology_version
        properties["evidence_quotes"] = [
            item.quote for item in entity.evidence
        ]
        projection.nodes.append(
            {
                "label": entity.class_name,
                "uid": uid,
                "properties": properties,
            }
        )

    for relation in batch.relations:
        if not relation.source_local_id or not relation.target_local_id:
            continue
        projection.edges.append(
            {
                "type": relation.relation_code,
                "source_uid": local_to_uid[relation.source_local_id],
                "target_uid": local_to_uid[relation.target_local_id],
                "properties": {
                    "confidence": relation.model_confidence,
                    "ontology_version": relation.ontology_version,
                    "evidence_quotes": [
                        item.quote for item in relation.evidence
                    ],
                },
            }
        )

    for entity in batch.entities:
        if entity.cause_rule is None:
            continue
        effect_uid = local_to_uid[entity.local_id]
        rule_uid = _stable_id("rule", batch.chunk_id, entity.local_id)
        projection.nodes.append(
            {
                "label": "FaultCauseRule",
                "uid": rule_uid,
                "properties": {
                    "effect_fault_uid": effect_uid,
                    "expression": entity.cause_rule.model_dump(exclude_none=True),
                    "evidence_quotes": [
                        item.quote for item in entity.evidence
                    ],
                },
            }
        )
        projection.edges.append(
            {
                "type": "HAS_CAUSE_RULE",
                "source_uid": effect_uid,
                "target_uid": rule_uid,
                "properties": {},
            }
        )
        _append_expression(
            projection=projection,
            expression=entity.cause_rule,
            effect_uid=effect_uid,
            rule_uid=rule_uid,
            local_to_uid=local_to_uid,
            path="root",
            output_uid=effect_uid,
        )

    return projection


def _append_expression(
    *,
    projection: GraphProjection,
    expression: Expression,
    effect_uid: str,
    rule_uid: str,
    local_to_uid: dict[str, str],
    path: str,
    output_uid: str | None,
) -> str | None:
    if expression.event_local_id:
        source_uid = local_to_uid[expression.event_local_id]
        projection.edges.append(
            {
                "type": "CAUSES",
                "source_uid": source_uid,
                "target_uid": effect_uid,
                "properties": {"rule_uid": rule_uid},
            }
        )
        return source_uid

    assert expression.op is not None
    gate_uid = _stable_id("gate", rule_uid, path, expression.op)
    projection.nodes.append(
        {
            "label": "LogicGate",
            "uid": gate_uid,
            "properties": {
                "gate_type": expression.op,
                "rule_uid": rule_uid,
                "input_count": len(expression.children),
                "expression_path": path,
            },
        }
    )
    for index, child in enumerate(expression.children):
        child_uid = _append_expression(
            projection=projection,
            expression=child,
            effect_uid=effect_uid,
            rule_uid=rule_uid,
            local_to_uid=local_to_uid,
            path=f"{path}.{index}",
            output_uid=gate_uid,
        )
        if child_uid is not None:
            projection.edges.append(
                {
                    "type": "INPUT_OF",
                    "source_uid": child_uid,
                    "target_uid": gate_uid,
                    "properties": {},
                }
            )

    if output_uid is not None:
        projection.edges.append(
            {
                "type": "OUTPUT_TO",
                "source_uid": gate_uid,
                "target_uid": output_uid,
                "properties": {},
            }
        )
    return gate_uid


def project_domain_graph(bundle: DomainBundle) -> GraphProjection:
    """Project a reviewed bundle into canonical graph nodes and edges.

    This is the storage-facing projection.  Unlike :func:`project_candidate_batch`
    it uses the durable IDs already assigned by the domain projector and the
    reviewed tree, so a graph rebuild cannot invent a second rule or gate.
    Evidence quotes are deliberately absent: the graph carries IDs only and the
    SQLite evidence store remains the single text source.
    """

    projection = GraphProjection()
    for entity in bundle.entities:
        projection.nodes.append(
            {
                "label": entity.class_code,
                "uid": entity.uid,
                "properties": _entity_node_properties(entity),
            }
        )

    for fact in bundle.facts:
        projection.edges.append(
            {
                "type": fact.predicate,
                "source_uid": fact.subject_uid,
                "target_uid": fact.object_uid,
                "properties": {
                    "fact_id": fact.fact_id,
                    "weight": 1.0,
                    "description": (
                        f"{fact.subject_uid} {fact.predicate} {fact.object_uid}"
                    ),
                    "source_id": ",".join(fact.evidence_ids),
                    "relation_code": fact.predicate,
                },
            }
        )

    tree = bundle.tree
    if tree is not None:
        _append_tree_nodes(projection, tree)
        for edge in tree.edges:
            projection.edges.append(
                {
                    "type": edge.relation_code,
                    "source_uid": edge.source_uid,
                    "target_uid": edge.target_uid,
                    "properties": {
                        "weight": 1.0,
                        "description": (
                            f"{edge.source_uid} {edge.relation_code} "
                            f"{edge.target_uid}"
                        ),
                        "relation_code": edge.relation_code,
                        **edge.properties,
                    },
                }
            )

    return projection


def _entity_node_properties(entity: EntityRecord) -> dict[str, Any]:
    file_path = (
        entity.object_uri if isinstance(entity, FileAsset) else "seed"
    )
    return {
        "entity_id": entity.uid,
        "entity_type": entity.class_code,
        "description": entity.description or entity.canonical_name,
        "source_id": ",".join(entity.source_uids),
        "file_path": file_path,
        "created_at": entity.created_at.isoformat(),
        "domain_properties_json": json.dumps(
            entity.model_dump(mode="json", exclude={"evidence_ids"}),
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def _append_tree_nodes(
    projection: GraphProjection,
    tree: FaultTreeDraft,
) -> None:
    for node in tree.nodes:
        if node.class_code == "FaultEvent":
            # The entity loop already emitted the reviewed FaultEvent node.
            continue
        projection.nodes.append(
            {
                "label": node.class_code,
                "uid": node.uid,
                "properties": {
                    "entity_id": node.uid,
                    "entity_type": node.class_code,
                    "description": node.label,
                    "source_id": tree.tenant_id,
                    "file_path": "seed",
                    "created_at": tree.created_at.isoformat(),
                    "tree_properties_json": json.dumps(
                        node.properties,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            }
        )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"
