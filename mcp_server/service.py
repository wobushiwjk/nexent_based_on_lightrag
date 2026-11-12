"""Read-only query surface for the industrial fault-tree domain.

Every method either returns a JSON-ready dictionary or raises
:class:`DomainError`.  The MCP layer turns the error into the common response
envelope; direct callers (tests, scripts) can catch it instead.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from lightrag.fault_tree.contracts import (
    DecisionStep,
    Equipment,
    Evidence,
    Fact,
    FaultCauseRule,
    FaultEvent,
    FaultTreeDraft,
    FileAsset,
    ValidationIssue,
)
from lightrag.fault_tree.domain_validation import (
    errors_only,
    validate_fault_tree_draft,
)
from mcp_server.errors import (
    AMBIGUOUS_SELECTOR,
    INVALID_ARGUMENT,
    NOT_FOUND,
    DomainError,
)
from mcp_server.models import ToolErrorPayload, ToolMeta, ToolResponse
from mcp_server.repository import DomainRepository, TenantIndex

logger = logging.getLogger(__name__)

TARGET_TYPES = frozenset({"entity", "fact", "rule", "logic_gate", "decision_step"})
MAX_SEARCH_LIMIT = 50
MAX_VALIDATION_ISSUES = 100

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*|[㐀-鿿]+")
_CJK_RE = re.compile(r"[㐀-鿿]")


class DomainService:
    """Query validated tenant snapshots without mutating them."""

    def __init__(self, repository: DomainRepository) -> None:
        self.repository = repository
        self.ontology = repository.ontology

    # ------------------------------------------------------------------
    # Response envelope
    # ------------------------------------------------------------------

    def meta(self, tenant_id: str | None = None) -> ToolMeta:
        return ToolMeta(
            ontology_version=self.ontology.version,
            schema_version=self.ontology.schema_version or "",
            tenant_id=tenant_id,
            read_only=True,
        )

    def success(
        self,
        data: dict[str, Any],
        tenant_id: str | None = None,
    ) -> ToolResponse:
        return ToolResponse(ok=True, data=data, meta=self.meta(tenant_id))

    def failure(
        self,
        error: DomainError,
        tenant_id: str | None = None,
    ) -> ToolResponse:
        return ToolResponse(
            ok=False,
            error=ToolErrorPayload(
                code=error.code,
                message=error.message,
                details=error.details,
            ),
            meta=self.meta(tenant_id),
        )

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def get_ontology(self) -> dict[str, Any]:
        raw = self.repository.ontology_document
        raw_classes = raw.get("classes")
        raw_relations = raw.get("relations")
        if not isinstance(raw_classes, dict):
            raw_classes = {}
        if not isinstance(raw_relations, dict):
            raw_relations = {}

        classes = []
        for code, definition in self.ontology.classes.items():
            item = raw_classes.get(code)
            item = item if isinstance(item, dict) else {}
            classes.append(
                {
                    "code": code,
                    "label": definition.label,
                    "parent": definition.parent,
                    "abstract": bool(item.get("abstract", False)),
                    "prompt_visible": definition.prompt_visible,
                    "required_fields": list(definition.required_fields),
                    "extract_properties": definition.extract_properties,
                    "metadata_properties": item.get("metadata_properties") or {},
                    "standards": list(item.get("standards") or []),
                    "schema_ref": item.get("schema_ref"),
                }
            )

        relations = []
        for code, definition in self.ontology.relations.items():
            item = raw_relations.get(code)
            item = item if isinstance(item, dict) else {}
            relations.append(
                {
                    "code": code,
                    "source_types": sorted(definition.source_types),
                    "target_types": sorted(definition.target_types),
                    "directed": definition.directed,
                    "prompt_visible": definition.prompt_visible,
                    "system_generated": definition.system_generated,
                    "evidence_required": definition.evidence_required,
                    "properties": item.get("properties") or {},
                }
            )

        constraints = _dict_list(raw.get("constraints"))
        standards = _dict_list(raw.get("standards"))
        return {
            "ontology_version": self.ontology.version,
            "schema_version": self.ontology.schema_version,
            "domain": raw.get("domain"),
            "label": raw.get("label"),
            "description": raw.get("description"),
            "prompt_profile": raw.get("prompt_profile"),
            "counts": {
                "classes": len(classes),
                "relations": len(relations),
                "constraints": len(constraints),
                "standards": len(standards),
            },
            "classes": classes,
            "relations": relations,
            "constraints": constraints,
            "standards": standards,
        }

    def get_equipment(
        self,
        tenant_id: str,
        equipment_ref: str,
    ) -> dict[str, Any]:
        index = self.repository.tenant_index(tenant_id)
        ref = _require_text(equipment_ref, "equipment_ref")
        matches = _match_equipment(index, ref)
        if not matches:
            raise DomainError(
                NOT_FOUND,
                f"equipment {ref!r} was not found in tenant {index.tenant_id!r}",
                {"equipment_ref": ref},
            )
        if len(matches) > 1:
            raise DomainError(
                AMBIGUOUS_SELECTOR,
                f"equipment reference {ref!r} matches more than one record",
                {"candidates": sorted(matches)},
            )
        uid, matched_by = next(iter(matches.items()))
        equipment = index.equipment(uid)
        if equipment is None:
            raise DomainError(
                NOT_FOUND,
                f"equipment {ref!r} was not found in tenant {index.tenant_id!r}",
                {"equipment_ref": ref},
            )

        facts = _unique_facts(
            index.facts_by_subject.get(uid, ()) + index.facts_by_object.get(uid, ())
        )
        source_uids = set(equipment.source_uids)
        department_uids: set[str] = set()
        fault_uids: set[str] = set()
        for fact in facts:
            if fact.predicate == "HAS_FAULT" and fact.subject_uid == uid:
                fault_uids.add(fact.object_uid)
            elif fact.predicate == "HAS_EQUIPMENT" and fact.object_uid == uid:
                department_uids.add(fact.subject_uid)
            elif fact.predicate == "DESCRIBES_EQUIPMENT" and fact.object_uid == uid:
                source_uids.add(fact.subject_uid)

        faults = []
        for fault_uid in sorted(fault_uids):
            entity = index.entities.get(fault_uid)
            if isinstance(entity, FaultEvent):
                faults.append(_fault_summary(index, entity))
        departments = []
        for department_uid in sorted(department_uids):
            entity = index.entities.get(department_uid)
            if entity is not None:
                departments.append(
                    {
                        "uid": entity.uid,
                        "canonical_name": entity.canonical_name,
                        "class_code": entity.class_code,
                    }
                )
        source_files = [
            _file_summary(entity)
            for entity in (
                index.entities.get(source_uid) for source_uid in sorted(source_uids)
            )
            if isinstance(entity, FileAsset)
        ]
        evidence = [
            _evidence_summary(index, evidence_id)
            for evidence_id in equipment.evidence_ids
            if evidence_id in index.evidence
        ]
        return {
            "match": {"input": ref, "matched_by": matched_by},
            "equipment": _dump(equipment),
            "departments": departments,
            "faults": faults,
            "facts": [_dump(fact) for fact in facts],
            "source_files": source_files,
            "evidence": evidence,
        }

    def search_faults(
        self,
        tenant_id: str,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        index = self.repository.tenant_index(tenant_id)
        text = _require_text(query, "query")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise DomainError(
                INVALID_ARGUMENT,
                "limit must be an integer",
                {"parameter": "limit"},
            )
        if limit < 1 or limit > MAX_SEARCH_LIMIT:
            raise DomainError(
                INVALID_ARGUMENT,
                f"limit must be between 1 and {MAX_SEARCH_LIMIT}",
                {"parameter": "limit", "min": 1, "max": MAX_SEARCH_LIMIT},
            )

        normalized, tokens = _tokenize(text)
        equipment_uids = _equipment_uids_for_search(index, normalized, tokens)
        ranked: list[tuple[int, FaultEvent, list[str]]] = []
        for fault in index.faults():
            score, matched = _score_fault(
                fault,
                normalized,
                tokens,
                equipment_uids,
                index,
            )
            if score > 0:
                ranked.append((score, fault, matched))
        ranked.sort(key=lambda item: (-item[0], item[1].canonical_name, item[1].uid))

        results = []
        for score, fault, matched in ranked[:limit]:
            summary = _fault_summary(index, fault)
            summary["score"] = score
            summary["matched_terms"] = sorted(set(matched))[:12]
            results.append(summary)
        data: dict[str, Any] = {
            "query": text,
            "limit": limit,
            "count": len(results),
            "total_faults": sum(1 for _ in index.faults()),
            "results": results,
        }
        if not results:
            data["hint"] = (
                "No fault matched the query. Try an alias, a shorter term, "
                "or the fault UID."
            )
        return data

    def get_fault_tree(
        self,
        tenant_id: str,
        draft_id: str | None = None,
        top_event_uid: str | None = None,
    ) -> dict[str, Any]:
        index = self.repository.tenant_index(tenant_id)
        draft_id = _optional_text(draft_id)
        top_event_uid = _optional_text(top_event_uid)
        if draft_id and top_event_uid:
            raise DomainError(
                INVALID_ARGUMENT,
                "pass either draft_id or top_event_uid, not both",
                {"parameters": ["draft_id", "top_event_uid"]},
            )
        if not draft_id and not top_event_uid and index.tree is None:
            raise DomainError(
                NOT_FOUND,
                f"tenant {index.tenant_id!r} has no fault-tree draft",
                {"tenant_id": index.tenant_id},
            )
        tree = index.tree
        if tree is None:
            raise DomainError(
                NOT_FOUND,
                f"tenant {index.tenant_id!r} has no fault-tree draft",
                {"tenant_id": index.tenant_id},
            )
        if draft_id and tree.draft_id != draft_id:
            raise DomainError(
                NOT_FOUND,
                f"fault-tree draft {draft_id!r} was not found",
                {"draft_id": draft_id},
            )
        if top_event_uid and tree.top_event_uid != top_event_uid:
            raise DomainError(
                NOT_FOUND,
                f"no fault-tree draft has top event {top_event_uid!r}",
                {"top_event_uid": top_event_uid},
            )

        rule_uids = {
            node.uid for node in tree.nodes if node.class_code == "FaultCauseRule"
        }
        for node in tree.nodes:
            if node.class_code == "LogicGate":
                rule_uid = node.properties.get("rule_uid")
                if isinstance(rule_uid, str) and rule_uid:
                    rule_uids.add(rule_uid)
        rules = [
            _rule_summary(index.rules[rule_uid])
            for rule_uid in sorted(rule_uids)
            if rule_uid in index.rules
        ]

        faults = []
        fault_uids = {
            node.uid for node in tree.nodes if node.class_code == "FaultEvent"
        }
        for fault_uid in sorted(fault_uids):
            entity = index.entities.get(fault_uid)
            if isinstance(entity, FaultEvent):
                faults.append(_fault_summary(index, entity))
        top_event = index.entities.get(tree.top_event_uid)
        top_summary = (
            _fault_summary(index, top_event)
            if isinstance(top_event, FaultEvent)
            else None
        )

        evidence_ids: set[str] = set()
        for rule in rules:
            evidence_ids.update(rule["evidence_ids"])
        for fault in faults:
            evidence_ids.update(fault["evidence_ids"])
        gate_count = sum(1 for node in tree.nodes if node.class_code == "LogicGate")
        return {
            "draft": _dump(tree),
            "top_event": top_summary,
            "faults": faults,
            "rules": rules,
            "rule_expressions": [
                {
                    "rule_uid": rule["rule_uid"],
                    "effect_fault_uid": rule["effect_fault_uid"],
                    "expression": rule["expression"],
                    "evidence_ids": rule["evidence_ids"],
                }
                for rule in rules
            ],
            "evidence_ids": sorted(evidence_ids),
            "counts": {
                "nodes": len(tree.nodes),
                "edges": len(tree.edges),
                "gates": gate_count,
                "faults": len(faults),
                "rules": len(rules),
            },
        }

    def trace_evidence(
        self,
        tenant_id: str,
        target_type: str,
        target_id: str,
    ) -> dict[str, Any]:
        index = self.repository.tenant_index(tenant_id)
        if target_type not in TARGET_TYPES:
            raise DomainError(
                INVALID_ARGUMENT,
                f"unknown target_type {target_type!r}",
                {"allowed": sorted(TARGET_TYPES)},
            )
        target_key = _require_text(target_id, "target_id")
        collected: defaultdict[str, set[str]] = defaultdict(set)
        target_payload: dict[str, Any]

        if target_type == "entity":
            entity = index.entities.get(target_key)
            if entity is None:
                raise _target_not_found(target_type, target_key, index.tenant_id)
            _collect_evidence(
                collected,
                entity.evidence_ids,
                "record",
                index,
                target_type,
                target_key,
            )
            target_payload = _dump(entity)
        elif target_type == "fact":
            fact = index.facts.get(target_key)
            if fact is None:
                raise _target_not_found(target_type, target_key, index.tenant_id)
            _collect_evidence(
                collected,
                fact.evidence_ids,
                "record",
                index,
                target_type,
                target_key,
            )
            target_payload = _dump(fact)
        elif target_type == "rule":
            rule = index.rules.get(target_key)
            if rule is None:
                raise _target_not_found(target_type, target_key, index.tenant_id)
            _collect_evidence(
                collected,
                rule.evidence_ids,
                "record",
                index,
                target_type,
                target_key,
            )
            target_payload = _dump(rule)
        elif target_type == "logic_gate":
            node = _find_tree_node(index, target_key)
            if node is None or node.class_code != "LogicGate":
                raise _target_not_found(target_type, target_key, index.tenant_id)
            _collect_evidence(
                collected,
                (),
                "record",
                index,
                target_type,
                target_key,
            )
            rule_uid = node.properties.get("rule_uid")
            if isinstance(rule_uid, str) and rule_uid in index.rules:
                _add_evidence(
                    collected,
                    index.rules[rule_uid].evidence_ids,
                    f"rule:{rule_uid}",
                )
            target_payload = _dump(node)
        else:
            step, trace = _find_decision_step(index, target_key)
            if step is None or trace is None:
                raise _target_not_found(target_type, target_key, index.tenant_id)
            _collect_evidence(
                collected,
                step.evidence_ids,
                "record",
                index,
                target_type,
                target_key,
            )
            target_payload = {
                "trace_id": trace.trace_id,
                "step": _dump(step),
            }

        evidence = []
        for evidence_id in collected:
            item = index.evidence.get(evidence_id)
            if item is None:
                continue
            evidence.append(_evidence_detail(index, item, collected[evidence_id]))
        evidence.sort(
            key=lambda item: (
                item["asset"]["file_name"] if item["asset"] else "",
                item["char_start"] if item["char_start"] is not None else -1,
                item["evidence_id"],
            )
        )
        return {
            "target_type": target_type,
            "target_id": target_key,
            "target": target_payload,
            "count": len(evidence),
            "evidence": evidence,
        }

    def validate_fault_tree(
        self,
        tenant_id: str,
        draft_id: str | None = None,
        draft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        index = self.repository.tenant_index(tenant_id)
        draft_id = _optional_text(draft_id)
        if draft_id and draft is not None:
            raise DomainError(
                INVALID_ARGUMENT,
                "pass either draft_id or draft, not both",
                {"parameters": ["draft_id", "draft"]},
            )
        if not draft_id and draft is None:
            raise DomainError(
                INVALID_ARGUMENT,
                "pass either draft_id or draft",
                {"parameters": ["draft_id", "draft"]},
            )

        if draft_id:
            tree = index.tree
            if tree is None or tree.draft_id != draft_id:
                raise DomainError(
                    NOT_FOUND,
                    f"fault-tree draft {draft_id!r} was not found",
                    {"draft_id": draft_id},
                )
        else:
            if not isinstance(draft, dict):
                raise DomainError(
                    INVALID_ARGUMENT,
                    "draft must be a JSON object",
                    {"parameter": "draft"},
                )
            declared_tenant = draft.get("tenant_id")
            if declared_tenant and declared_tenant != index.tenant_id:
                raise DomainError(
                    INVALID_ARGUMENT,
                    "draft tenant does not match the requested tenant",
                    {"tenant_id": declared_tenant},
                )
            try:
                tree = FaultTreeDraft.model_validate(draft)
            except ValidationError as exc:
                raise DomainError(
                    INVALID_ARGUMENT,
                    "draft does not match the FaultTreeDraft contract",
                    {"errors": _pydantic_errors(exc)},
                ) from exc

        issues = validate_fault_tree_draft(tree, index.rules, index.entities)
        if tree.tenant_id != index.tenant_id:
            issues.insert(
                0,
                _issue(
                    "tree-tenant-mismatch",
                    "error",
                    f"draft belongs to tenant {tree.tenant_id!r}, not "
                    f"{index.tenant_id!r}",
                    tree.draft_id,
                ),
            )
        if tree.ontology_version != index.ontology_version:
            issues.append(
                _issue(
                    "tree-ontology-version",
                    "warning",
                    f"draft uses ontology version {tree.ontology_version!r}, "
                    f"not {index.ontology_version!r}",
                    tree.draft_id,
                )
            )

        errors = errors_only(issues)
        dumped = [_dump(issue) for issue in issues[:MAX_VALIDATION_ISSUES]]
        return {
            "draft_id": tree.draft_id,
            "top_event_uid": tree.top_event_uid,
            "valid": not errors,
            "error_count": len(errors),
            "warning_count": len(issues) - len(errors),
            "issues_truncated": len(issues) > MAX_VALIDATION_ISSUES,
            "issues": dumped,
            "checked": {
                "rules": len(index.rules),
                "entities": len(index.entities),
            },
        }


# ----------------------------------------------------------------------
# Lookup helpers
# ----------------------------------------------------------------------


def _match_equipment(index: TenantIndex, ref: str) -> dict[str, str]:
    needle = ref.casefold()
    matches: dict[str, str] = {}
    entity = index.entities.get(ref)
    if isinstance(entity, Equipment):
        matches[entity.uid] = "uid"
    tag_uid = index.equipment_by_tag.get(needle)
    if tag_uid:
        matches.setdefault(tag_uid, "equipment_tag")
    for item in index.entities.values():
        if not isinstance(item, Equipment):
            continue
        names = [item.equipment_name, item.canonical_name, *item.aliases]
        if any(needle == name.casefold() for name in names if name):
            matches.setdefault(item.uid, "name")
    return matches


def _equipment_uids_for_search(
    index: TenantIndex,
    normalized: str,
    tokens: set[str],
) -> set[str]:
    uids: set[str] = set()
    if normalized:
        tag_uid = index.equipment_by_tag.get(normalized)
        if tag_uid:
            uids.add(tag_uid)
    for token in tokens:
        tag_uid = index.equipment_by_tag.get(token)
        if tag_uid:
            uids.add(tag_uid)
        for item in index.entities.values():
            if not isinstance(item, Equipment):
                continue
            names = [item.equipment_name, item.canonical_name, *item.aliases]
            if any(token == name.casefold() for name in names if name):
                uids.add(item.uid)
    return uids


def _score_fault(
    fault: FaultEvent,
    normalized: str,
    tokens: set[str],
    equipment_uids: set[str],
    index: TenantIndex,
) -> tuple[int, list[str]]:
    name_text = " ".join(
        {fault.canonical_name, fault.fault_name, *fault.aliases}
    ).casefold()
    body_text = " ".join(
        [
            fault.description or "",
            *fault.symptoms,
            *fault.maintenance_measures,
        ]
    ).casefold()
    score = 0
    matched: list[str] = []
    if normalized and normalized in fault.uid.casefold():
        score += 60
        matched.append(normalized)
    if normalized and normalized in name_text:
        score += 100
        matched.append(normalized)
    for token in tokens:
        if token == normalized:
            continue
        if token in name_text:
            score += 30
            matched.append(token)
        elif token in body_text:
            score += 5
            matched.append(token)
    for equipment_uid in equipment_uids:
        for fact in index.facts_by_subject.get(equipment_uid, ()):
            if fact.predicate == "HAS_FAULT" and fact.object_uid == fault.uid:
                score += 25
                matched.append(f"equipment:{equipment_uid}")
    return score, matched


def _find_tree_node(index: TenantIndex, uid: str) -> Any:
    if index.tree is None:
        return None
    return next((node for node in index.tree.nodes if node.uid == uid), None)


def _find_decision_step(
    index: TenantIndex,
    step_id: str,
) -> tuple[DecisionStep | None, Any]:
    if index.trace is None:
        return None, None
    step = next(
        (item for item in index.trace.steps if item.step_id == step_id),
        None,
    )
    return step, index.trace


def _collect_evidence(
    collected: defaultdict[str, set[str]],
    direct_ids: Iterable[str],
    direct_via: str,
    index: TenantIndex,
    target_type: str,
    target_id: str,
) -> None:
    _add_evidence(collected, direct_ids, direct_via)
    _add_evidence(
        collected,
        index.evidence_by_target.get((target_type, target_id), ()),
        "target",
    )


def _add_evidence(
    collected: defaultdict[str, set[str]],
    evidence_ids: Iterable[str],
    via: str,
) -> None:
    for evidence_id in evidence_ids:
        collected[evidence_id].add(via)


def _target_not_found(
    target_type: str,
    target_id: str,
    tenant_id: str,
) -> DomainError:
    return DomainError(
        NOT_FOUND,
        f"{target_type} {target_id!r} was not found in tenant {tenant_id!r}",
        {"target_type": target_type, "target_id": target_id},
    )


# ----------------------------------------------------------------------
# Serialization helpers
# ----------------------------------------------------------------------


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _file_summary(entity: FileAsset) -> dict[str, Any]:
    return {
        "uid": entity.uid,
        "canonical_name": entity.canonical_name,
        "file_name": entity.file_name,
        "modality": entity.modality,
        "mime_type": entity.mime_type,
        "object_uri": entity.object_uri,
        "text_uri": entity.text_uri,
        "checksum": entity.checksum,
        "page_count": entity.page_count,
        "parse_status": entity.parse_status,
    }


def _evidence_summary(index: TenantIndex, evidence_id: str) -> dict[str, Any]:
    item = index.evidence[evidence_id]
    return {
        "evidence_id": item.evidence_id,
        "asset_id": item.asset_id,
        "quote": item.quote,
        "page": item.page,
        "section_path": item.section_path,
        "char_start": item.char_start,
        "char_end": item.char_end,
        "modality": item.modality,
        "review_status": item.review_status,
    }


def _evidence_detail(
    index: TenantIndex,
    item: Evidence,
    via: set[str],
) -> dict[str, Any]:
    asset = index.entities.get(item.asset_id)
    return {
        "evidence_id": item.evidence_id,
        "asset_id": item.asset_id,
        "asset": _file_summary(asset) if isinstance(asset, FileAsset) else None,
        "quote": item.quote,
        "quote_hash": item.quote_hash,
        "page": item.page,
        "section_path": item.section_path,
        "char_start": item.char_start,
        "char_end": item.char_end,
        "bbox": item.bbox,
        "modality": item.modality,
        "confidence": item.confidence,
        "extractor": item.extractor,
        "extraction_run_id": item.extraction_run_id,
        "review_status": item.review_status,
        "targets": [_dump(target) for target in item.targets],
        "via": sorted(via),
    }


def _fault_summary(index: TenantIndex, fault: FaultEvent) -> dict[str, Any]:
    cause_rules = index.rules_by_effect.get(fault.uid, ())
    input_rules = index.rules_by_input.get(fault.uid, ())
    equipment_uids = {
        fact.subject_uid
        for fact in index.facts_by_object.get(fault.uid, ())
        if fact.predicate == "HAS_FAULT"
    }
    return {
        "uid": fault.uid,
        "canonical_name": fault.canonical_name,
        "fault_name": fault.fault_name,
        "aliases": list(fault.aliases),
        "description": fault.description,
        "severity": fault.severity,
        "symptoms": list(fault.symptoms),
        "maintenance_measures": list(fault.maintenance_measures),
        "evidence_ids": list(fault.evidence_ids),
        "evidence_count": len(fault.evidence_ids),
        "cause_rule_uids": [rule.rule_uid for rule in cause_rules],
        "appears_in_rule_uids": [rule.rule_uid for rule in input_rules],
        "linked_equipment_uids": sorted(equipment_uids),
    }


def _rule_summary(rule: FaultCauseRule) -> dict[str, Any]:
    return {
        "rule_uid": rule.rule_uid,
        "effect_fault_uid": rule.effect_fault_uid,
        "expression": _dump(rule.expression),
        "scenario": rule.scenario,
        "evidence_ids": list(rule.evidence_ids),
        "review_status": rule.review_status,
        "ontology_version": rule.ontology_version,
        "created_at": rule.created_at.isoformat(),
        "created_by": rule.created_by,
    }


def _unique_facts(facts: Iterable[Fact]) -> list[Fact]:
    seen: set[str] = set()
    result: list[Fact] = []
    for fact in facts:
        if fact.fact_id in seen:
            continue
        seen.add(fact.fact_id)
        result.append(fact)
    result.sort(key=lambda item: item.fact_id)
    return result


def _tokenize(text: str) -> tuple[str, set[str]]:
    normalized = text.casefold().strip()
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(normalized):
        tokens.add(raw)
        if _CJK_RE.search(raw):
            for index in range(len(raw) - 1):
                tokens.add(raw[index : index + 2])
    tokens.discard("")
    return normalized, tokens


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(
            INVALID_ARGUMENT,
            f"{name} must be a non-empty string",
            {"parameter": name},
        )
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainError(
            INVALID_ARGUMENT,
            "selector must be a string",
            {"parameter": "selector"},
        )
    text = value.strip()
    return text or None


def _issue(
    code: str,
    severity: str,
    message: str,
    target_id: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        target_id=target_id,
    )


def _pydantic_errors(exc: ValidationError) -> list[dict[str, Any]]:
    result = []
    for error in exc.errors()[:10]:
        result.append(
            {
                "loc": ".".join(str(part) for part in error.get("loc", ())),
                "msg": error.get("msg", ""),
                "type": error.get("type", ""),
            }
        )
    return result


__all__ = ["DomainService", "MAX_SEARCH_LIMIT", "TARGET_TYPES"]
