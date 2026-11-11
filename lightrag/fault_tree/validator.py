"""Validate fault-tree candidates before LightRAG graph ingestion."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.fault_tree.schemas import (
    FaultCandidateBatch,
    FaultEntityPayload,
    FaultRelationPayload,
)
from lightrag.utils import logger

CandidateSink = Callable[[FaultCandidateBatch], Any | Awaitable[Any]]


class FaultCandidateValidator:
    """Validate domain payloads attached by the JSON extraction parser.

    The validator consumes the private ``_fault_payload`` carried by parsed
    records. Non-domain extraction output without that payload is left
    unchanged, so the same LightRAG instance can still process ordinary data.
    """

    def __init__(
        self,
        ontology: OntologyRegistry,
        sink: CandidateSink | None = None,
    ) -> None:
        self.ontology = ontology
        self.sink = sink

    @classmethod
    def from_prompt_profile(
        cls,
        profile_name: str,
        sink: CandidateSink | None = None,
    ) -> "FaultCandidateValidator":
        """Build a validator from a file under ``PROMPT_DIR/entity_type``."""

        from lightrag.prompt import resolve_entity_type_prompt_path

        return cls(
            OntologyRegistry.from_file(
                resolve_entity_type_prompt_path(profile_name)
            ),
            sink=sink,
        )

    async def __call__(
        self,
        chunk_key: str,
        chunk_text: str,
        maybe_nodes: dict[str, list[dict]],
        maybe_edges: dict[tuple[str, str], list[dict]],
    ) -> tuple[dict, dict]:
        batch = FaultCandidateBatch(
            chunk_id=chunk_key,
            chunk_text=chunk_text,
            file_path=_first_file_path(maybe_nodes, maybe_edges),
        )
        entity_classes: dict[str, str] = {}
        entity_local_ids: dict[str, str] = {}
        record_by_local_id: dict[str, dict] = {}
        generated_edges: dict[tuple[str, str], list[dict]] = {}
        local_ids: set[str] = set()

        for entity_name, records in list(maybe_nodes.items()):
            accepted: list[dict] = []
            for record in records:
                payload_json = record.get("_fault_payload")
                if not payload_json:
                    accepted.append(record)
                    continue
                try:
                    payload = FaultEntityPayload.model_validate_json(payload_json)
                except Exception as exc:
                    batch.warnings.append(
                        f"invalid entity payload for {entity_name!r}: {exc}"
                    )
                    continue
                canonical_class = self.ontology.canonical_class(payload.class_name)
                if canonical_class is None:
                    batch.warnings.append(
                        f"unknown entity class {payload.class_name!r} "
                        f"for {entity_name!r}"
                    )
                    continue
                if not _evidence_is_grounded(payload.evidence, chunk_text):
                    batch.warnings.append(
                        f"entity {entity_name!r} has no verbatim evidence"
                    )
                    continue
                if payload.local_id in local_ids:
                    batch.warnings.append(
                        f"duplicate local_id {payload.local_id!r}"
                    )
                    continue

                payload = payload.model_copy(
                    update={
                        "class_name": canonical_class,
                        "ontology_version": self.ontology.version,
                    }
                )
                record["entity_type"] = canonical_class
                record["_fault_payload"] = payload.model_dump_json(
                    by_alias=True
                )
                accepted.append(record)
                batch.entities.append(payload)
                entity_classes[entity_name] = canonical_class
                entity_local_ids[entity_name] = payload.local_id
                record_by_local_id[payload.local_id] = record
                local_ids.add(payload.local_id)
            if accepted:
                maybe_nodes[entity_name] = accepted
            else:
                maybe_nodes.pop(entity_name, None)

        for entity in batch.entities:
            if entity.cause_rule is None:
                continue
            for local_id in _expression_event_ids(entity.cause_rule):
                if local_id not in local_ids:
                    batch.warnings.append(
                        f"cause_rule of {entity.local_id!r} references "
                        f"unknown local_id {local_id!r}"
                    )
                    entity.cause_rule = None
                    break

        for entity in batch.entities:
            record = record_by_local_id[entity.local_id]
            record["_fault_payload"] = entity.model_dump_json(by_alias=True)

        for edge_key, records in list(maybe_edges.items()):
            accepted = []
            for record in records:
                payload_json = record.get("_fault_payload")
                if not payload_json:
                    accepted.append(record)
                    continue
                try:
                    payload = FaultRelationPayload.model_validate_json(payload_json)
                    source_type = entity_classes[record["src_id"]]
                    target_type = entity_classes[record["tgt_id"]]
                    self.ontology.validate_relation(
                        payload.relation_code,
                        source_type,
                        target_type,
                    )
                    payload = payload.model_copy(
                        update={
                            "source_local_id": entity_local_ids[record["src_id"]],
                            "target_local_id": entity_local_ids[record["tgt_id"]],
                            "ontology_version": self.ontology.version,
                        }
                    )
                except Exception as exc:
                    batch.warnings.append(
                        f"invalid relation {edge_key!r}: {exc}"
                    )
                    continue
                if not _evidence_is_grounded(payload.evidence, chunk_text):
                    batch.warnings.append(
                        f"relation {edge_key!r} has no verbatim evidence"
                    )
                    continue
                if payload.relation_code == "CAUSES" and payload.gate_type_hint:
                    gate_name = _stable_gate_name(
                        payload.gate_type_hint,
                        record["tgt_id"],
                        payload.gate_key or "",
                    )
                    if gate_name not in maybe_nodes:
                        maybe_nodes[gate_name] = [
                            {
                                "entity_name": gate_name,
                                "entity_type": "LogicGate",
                                "description": (
                                    f"{payload.gate_type_hint} gate for "
                                    f"{record['tgt_id']} ({payload.gate_key})"
                                ),
                                "source_id": chunk_key,
                                "file_path": record.get(
                                    "file_path", "unknown_source"
                                ),
                                "timestamp": record.get("timestamp", 0),
                            }
                        ]
                    _append_generated_edge(
                        generated_edges,
                        source=record["src_id"],
                        target_name=gate_name,
                        relation_code="INPUT_OF",
                        description=(
                            f"{record['src_id']} is an input of {gate_name}"
                        ),
                        record=record,
                    )
                    _append_generated_edge(
                        generated_edges,
                        source=gate_name,
                        target_name=record["tgt_id"],
                        relation_code="OUTPUT_TO",
                        description=f"{gate_name} outputs to {record['tgt_id']}",
                        record=record,
                    )
                    batch.relations.append(payload)
                    continue
                record["keywords"] = _with_relation_code(
                    record.get("keywords", ""),
                    payload.relation_code,
                )
                record["_fault_payload"] = payload.model_dump_json()
                accepted.append(record)
                batch.relations.append(payload)
            if accepted:
                maybe_edges[edge_key] = accepted
            else:
                maybe_edges.pop(edge_key, None)

        for edge_key, records in generated_edges.items():
            maybe_edges.setdefault(edge_key, []).extend(records)

        for warning in batch.warnings:
            logger.warning("%s: %s", chunk_key, warning)

        if self.sink is not None:
            result = self.sink(batch)
            if inspect.isawaitable(result):
                await result

        return maybe_nodes, maybe_edges


def _first_file_path(
    maybe_nodes: dict[str, list[dict]],
    maybe_edges: dict[tuple[str, str], list[dict]],
) -> str | None:
    for records in (*maybe_nodes.values(), *maybe_edges.values()):
        for record in records:
            value = record.get("file_path")
            if isinstance(value, str) and value.strip():
                return value
    return None


def _evidence_is_grounded(evidence: list[Any], chunk_text: str) -> bool:
    if not evidence:
        return False
    return all(item.quote.strip() in chunk_text for item in evidence)


def _with_relation_code(keywords: Any, relation_code: str) -> str:
    """Expose the ontology relation code in LightRAG's existing edge keywords."""

    values = [
        item.strip()
        for item in str(keywords or "").replace("，", ",").split(",")
        if item.strip()
    ]
    if relation_code not in values:
        values.insert(0, relation_code)
    return ", ".join(values)


def _stable_gate_name(gate_type: str, target_name: str, gate_key: str) -> str:
    """Build a chunk-independent gate name for cross-chunk aggregation."""

    identity = "\x1f".join(
        (
            gate_type.strip().upper(),
            " ".join(target_name.split()).casefold(),
            " ".join(gate_key.split()).casefold(),
        )
    )
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    return f"GATE-{gate_type.upper()}-{digest}"


def _append_generated_edge(
    destination: dict[tuple[str, str], list[dict]],
    *,
    source: str,
    target_name: str,
    relation_code: str,
    description: str,
    record: dict,
) -> None:
    destination.setdefault((source, target_name), []).append(
        {
            "src_id": source,
            "tgt_id": target_name,
            "weight": 1.0,
            "description": description,
            "keywords": relation_code,
            "source_id": record.get("source_id", ""),
            "file_path": record.get("file_path", "unknown_source"),
            "timestamp": record.get("timestamp", 0),
        }
    )


def _expression_event_ids(expression: Any) -> list[str]:
    if expression.event_local_id:
        return [expression.event_local_id]
    result: list[str] = []
    for child in expression.children:
        result.extend(_expression_event_ids(child))
    return result
