"""Deterministic impact analysis for ontology change drafts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from lightrag.fault_tree.governance.models import OntologyChangeSet


def analyze_ontology_impact(
    change: OntologyChangeSet,
    *,
    ontology_document: Mapping[str, Any],
    domain_storage_root: str | Path | None,
) -> dict[str, Any]:
    """Report affected classes, relations, instances, trees, and test suites."""

    classes, relations = _affected_codes(change, ontology_document)
    constraints = sorted(
        operation.code
        for operation in change.operations
        if operation.kind == "constraint"
    )
    tenant_reports: list[dict[str, Any]] = []
    storage = Path(domain_storage_root).resolve() if domain_storage_root else None
    if storage is not None:
        for active_path in sorted((storage / "tenants").glob("*/active.json")):
            tenant_reports.append(
                _analyze_tenant(
                    active_path,
                    classes=classes,
                    relations=relations,
                )
            )

    return {
        "change_id": change.change_id,
        "base_version": change.base_version,
        "proposed_version": change.proposed_version,
        "diff": [
            {
                "kind": operation.kind,
                "action": operation.action,
                "code": operation.code,
                "before": operation.before,
                "after": operation.after,
                "evidence_count": len(operation.evidence),
            }
            for operation in change.operations
        ],
        "affected": {
            "classes": classes,
            "relations": relations,
            "constraints": constraints,
            "schemas": [
                "entity.schema.json",
                "fact.schema.json",
                "fault_rule.schema.json",
            ],
        },
        "tenants": tenant_reports,
        "summary": {
            "tenant_count": len(tenant_reports),
            "affected_entity_count": sum(
                item["counts"]["entities"] for item in tenant_reports
            ),
            "affected_fact_count": sum(
                item["counts"]["facts"] for item in tenant_reports
            ),
            "affected_rule_count": sum(
                item["counts"]["rules"] for item in tenant_reports
            ),
            "affected_tree_count": sum(
                item["counts"]["trees"] for item in tenant_reports
            ),
        },
        "tests_to_run": [
            "tests/domain",
            "tests/mcp",
            "tests/governance_mcp",
            "tests/nexent",
        ],
        "applied": False,
    }


def _affected_codes(
    change: OntologyChangeSet,
    ontology_document: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    classes: set[str] = set()
    relations: set[str] = set()
    ontology_classes = ontology_document.get("classes")
    ontology_classes = ontology_classes if isinstance(ontology_classes, Mapping) else {}
    ontology_relations = ontology_document.get("relations")
    ontology_relations = (
        ontology_relations if isinstance(ontology_relations, Mapping) else {}
    )
    for operation in change.operations:
        if operation.kind == "class":
            classes.add(operation.code)
        elif operation.kind == "relation":
            relations.add(operation.code)
            for key in ("source_types", "target_types"):
                classes.update(_string_list(operation.after.get(key)))
            current = ontology_relations.get(operation.code)
            if isinstance(current, Mapping):
                for key in ("source", "target"):
                    classes.update(_string_list(current.get(key)))
        elif operation.kind == "constraint":
            classes.update(_string_list(operation.after.get("applies_to")))
    for code in list(classes):
        definition = ontology_classes.get(code)
        if isinstance(definition, Mapping):
            parent = definition.get("parent")
            if isinstance(parent, str) and parent:
                classes.add(parent)
    return sorted(classes), sorted(relations)


def _analyze_tenant(
    active_path: Path,
    *,
    classes: list[str],
    relations: list[str],
) -> dict[str, Any]:
    tenant_id = active_path.parent.name
    report: dict[str, Any] = {
        "tenant_id": tenant_id,
        "run_id": None,
        "counts": {
            "entities": 0,
            "facts": 0,
            "rules": 0,
            "trees": 0,
        },
        "samples": {
            "entities": [],
            "facts": [],
            "rules": [],
            "trees": [],
        },
        "error": None,
    }
    try:
        pointer = json.loads(active_path.read_text(encoding="utf-8"))
        run_id = str(pointer.get("run_id") or "")
        if not run_id or Path(run_id).name != run_id:
            raise ValueError("invalid active run id")
        bundle_path = active_path.parent / "runs" / run_id / "bundle.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report["error"] = str(exc)
        return report

    report["run_id"] = run_id
    class_set = set(classes)
    relation_set = set(relations)
    entities = [
        item
        for item in bundle.get("entities") or []
        if isinstance(item, Mapping) and item.get("class_code") in class_set
    ]
    entity_uids = {entity.get("uid") for entity in entities}
    facts = [
        item
        for item in bundle.get("facts") or []
        if isinstance(item, Mapping)
        and (
            item.get("predicate") in relation_set
            or item.get("subject_uid") in entity_uids
            or item.get("object_uid") in entity_uids
        )
    ]
    rules = [
        item
        for item in bundle.get("rules") or []
        if isinstance(item, Mapping)
        and (
            item.get("effect_fault_uid") in entity_uids
            or bool(_expression_uids(item.get("expression")) & entity_uids)
        )
    ]
    tree = bundle.get("tree")
    trees = []
    if isinstance(tree, Mapping):
        node_uids = {
            node.get("uid")
            for node in tree.get("nodes") or []
            if isinstance(node, Mapping)
        }
        edge_relations = {
            edge.get("relation_code")
            for edge in tree.get("edges") or []
            if isinstance(edge, Mapping)
        }
        if node_uids & entity_uids or edge_relations & relation_set:
            trees.append(tree)

    report["counts"] = {
        "entities": len(entities),
        "facts": len(facts),
        "rules": len(rules),
        "trees": len(trees),
    }
    report["samples"] = {
        "entities": [str(item.get("uid")) for item in entities[:10] if item.get("uid")],
        "facts": [
            str(item.get("fact_id")) for item in facts[:10] if item.get("fact_id")
        ],
        "rules": [
            str(item.get("rule_uid")) for item in rules[:10] if item.get("rule_uid")
        ],
        "trees": [
            str(item.get("draft_id")) for item in trees[:10] if item.get("draft_id")
        ],
    }
    return report


def _expression_uids(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    result: set[str] = set()
    event_uid = value.get("event_uid")
    if isinstance(event_uid, str):
        result.add(event_uid)
    for child in value.get("children") or []:
        result.update(_expression_uids(child))
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


__all__ = ["analyze_ontology_impact"]
