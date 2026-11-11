"""Tests for the industrial fault-tree domain pack and its contracts."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from lightrag.fault_tree.contracts import UidExpression
from lightrag.fault_tree.domain_validation import (
    REQUIRED_STANDARDS,
    errors_only,
    load_domain_bundle,
    quote_hash,
    validate_crosswalk,
    validate_domain_bundle,
    validate_schema_manifest,
)
from lightrag.fault_tree.ontology import OntologyRegistry

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "domain_packs" / "industrial_fault"
PROMPT_PROFILE = ROOT / "prompts" / "entity_type" / "industrial_fault.yml"


def _raw_ontology() -> dict[str, Any]:
    return yaml.safe_load((PACK / "ontology.yml").read_text(encoding="utf-8"))


def _bundle():
    return load_domain_bundle(PACK / "seeds" / "demo_bundle.json")


def _codes(bundle) -> set[str]:
    return {issue.code for issue in validate_domain_bundle(bundle, PACK)}


def test_ontology_and_prompt_profile_do_not_drift() -> None:
    ontology = _raw_ontology()
    prompt = yaml.safe_load(PROMPT_PROFILE.read_text(encoding="utf-8"))
    registry = OntologyRegistry.from_file(PACK / "ontology.yml")

    assert prompt["ontology_version"] == ontology["ontology_version"]
    assert registry.version == ontology["ontology_version"]
    assert registry.schema_version == ontology["schema_version"]
    assert set(prompt["classes"]) <= set(ontology["classes"])
    assert set(prompt["relations"]) <= set(ontology["relations"])

    for name in prompt["classes"]:
        assert prompt["classes"][name]["prompt_visible"] == (
            ontology["classes"][name]["prompt_visible"]
        ), f"prompt visibility drifted for class {name}"
    for name in prompt["relations"]:
        assert prompt["relations"][name]["prompt_visible"] == (
            ontology["relations"][name]["prompt_visible"]
        ), f"prompt visibility drifted for relation {name}"

    for name, definition in ontology["classes"].items():
        if not definition.get("abstract"):
            assert definition.get("standards"), (
                f"concrete class {name} has no standards mapping"
            )


def test_demo_bundle_passes_every_validator() -> None:
    bundle = _bundle()
    issues = [
        *validate_domain_bundle(bundle, PACK),
        *validate_crosswalk(PACK),
        *validate_schema_manifest(PACK),
    ]
    assert errors_only(issues) == []
    assert bundle.ontology_version == _raw_ontology()["ontology_version"]
    assert bundle.tree is not None
    assert bundle.tree.validation.valid


def test_checked_in_bad_bundle_is_rejected() -> None:
    bundle = load_domain_bundle(PACK / "seeds" / "bad_bundle_sample.json")
    codes = {issue.code for issue in validate_domain_bundle(bundle, PACK)}

    assert {"evidence-quote-not-verbatim", "evidence-reference-missing"} <= codes


def test_crosswalk_covers_required_standards() -> None:
    raw = yaml.safe_load(
        (PACK / "standards" / "crosswalk.yml").read_text(encoding="utf-8")
    )
    assert REQUIRED_STANDARDS <= set(raw["standards"])


def test_quote_hash_tampering_is_rejected() -> None:
    bundle = _bundle()
    bundle.evidence[0].quote_hash = "sha256:" + "0" * 64

    assert "evidence-quote-hash" in _codes(bundle)


def test_non_verbatim_quote_is_rejected() -> None:
    bundle = _bundle()
    evidence = bundle.evidence[0]
    evidence.quote = "这句话不在任何来源文档中。"
    evidence.quote_hash = quote_hash(evidence.quote)
    evidence.char_start = None
    evidence.char_end = None

    assert "evidence-quote-not-verbatim" in _codes(bundle)


def test_char_span_must_match_quote() -> None:
    bundle = _bundle()
    bundle.evidence[0].char_start = 0
    bundle.evidence[0].char_end = 1

    assert "evidence-span-mismatch" in _codes(bundle)


def test_missing_evidence_breaks_referencing_records() -> None:
    bundle = _bundle()
    bundle.evidence = [
        item for item in bundle.evidence if item.evidence_id != "EV-MANUAL-1"
    ]

    assert "evidence-reference-missing" in _codes(bundle)


def test_causal_cycle_is_rejected() -> None:
    bundle = _bundle()
    assert bundle.tree is not None
    bundle.tree.edges.append(
        bundle.tree.edges[0].model_copy(
            update={
                "source_uid": "FAULT-LOW-PRESSURE",
                "target_uid": "FAULT-FILTER-BLOCKED",
                "relation_code": "CAUSES",
            }
        )
    )

    assert "tree-cycle" in _codes(bundle)


def test_unreachable_tree_node_is_rejected() -> None:
    bundle = _bundle()
    assert bundle.tree is not None
    bundle.tree.nodes.append(
        bundle.tree.nodes[0].model_copy(
            update={"uid": "FAULT-ORPHAN", "label": "孤立故障"}
        )
    )

    assert "tree-unreachable-node" in _codes(bundle)


def test_duplicate_equipment_tag_is_rejected() -> None:
    bundle = _bundle()
    equipment = next(
        item for item in bundle.entities if item.class_code == "Equipment"
    )
    bundle.entities.append(
        equipment.model_copy(update={"uid": "EQ-DUPLICATE", "equipment_tag": "P-101a"})
    )

    assert "duplicate-equipment-tag" in _codes(bundle)


def test_bundle_version_and_tenant_must_match() -> None:
    bundle = _bundle()
    bundle.ontology_version = "industrial-fault-v9.9"
    bundle.entities[0].tenant_id = "tenant-other"
    codes = _codes(bundle)

    assert "bundle-version-mismatch" in codes
    assert "entity-tenant-mismatch" in codes


def test_rule_cannot_list_its_own_effect_as_input() -> None:
    bundle = _bundle()
    rule = bundle.rules[0]
    child = rule.expression.children[0]
    child.event_uid = rule.effect_fault_uid

    assert "rule-input-is-effect" in _codes(bundle)


def test_tree_rule_effect_must_match_its_edge() -> None:
    bundle = _bundle()
    bundle.rules[0].effect_fault_uid = "FAULT-FILTER-BLOCKED"

    assert "tree-rule-effect" in _codes(bundle)
    assert "tree-output-effect" in _codes(bundle)


def test_gate_input_count_must_match_edges() -> None:
    bundle = _bundle()
    assert bundle.tree is not None
    gate = next(
        node for node in bundle.tree.nodes if node.class_code == "LogicGate"
    )
    gate.properties["input_count"] = 3

    assert "gate-input-count" in _codes(bundle)


def test_evidence_target_type_must_match_node_class() -> None:
    bundle = _bundle()
    bundle.evidence[0].targets.append(
        bundle.evidence[0].targets[0].model_copy(
            update={"target_type": "logic_gate", "target_id": "FAULT-LOW-PRESSURE"}
        )
    )

    assert "evidence-target-class" in _codes(bundle)


def test_trace_evidence_references_are_checked() -> None:
    bundle = _bundle()
    assert bundle.trace is not None
    bundle.trace.evidence_ids.append("EV-MISSING")

    assert "evidence-reference-missing" in _codes(bundle)


def test_expression_shape_is_enforced() -> None:
    with pytest.raises(ValidationError):
        UidExpression(op="AND", children=[UidExpression(event_uid="FAULT-A")])
    with pytest.raises(ValidationError):
        UidExpression()

    valid = UidExpression(
        op="OR",
        children=[
            UidExpression(event_uid="FAULT-A"),
            UidExpression(event_uid="FAULT-B"),
        ],
    )
    assert valid.op == "OR"


def test_schema_manifest_detects_tampering(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    shutil.copytree(PACK / "schemas", pack / "schemas")
    schema = pack / "schemas" / "entity.schema.json"
    schema.write_bytes(schema.read_bytes() + b"\n")

    codes = {issue.code for issue in validate_schema_manifest(pack)}
    assert "schema-hash-mismatch" in codes


def test_checked_in_schemas_match_the_contracts(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "export_domain_schemas", ROOT / "scripts" / "export_domain_schemas.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    generated = tmp_path / "schemas"
    module.write_schemas(generated)

    for checked_in in sorted((PACK / "schemas").glob("*.json")):
        fresh = generated / checked_in.name
        assert fresh.is_file(), f"{checked_in.name} is no longer generated"
        assert fresh.read_bytes() == checked_in.read_bytes(), (
            f"{checked_in.name} is stale; run scripts/export_domain_schemas.py"
        )
