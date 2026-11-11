"""Tests for extraction-result projection into the fault-tree domain."""

from __future__ import annotations

from pathlib import Path

import pytest

from lightrag.fault_tree.contracts import UidExpression
from lightrag.fault_tree.domain_validation import validate_fault_tree_draft
from lightrag.fault_tree.graph import project_domain_graph
from lightrag.fault_tree.importer import build_imported_domain, compare_tree
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.fault_tree.projection import (
    CandidateBatchInput,
    ProjectionError,
    SourceChunk,
    project_domain,
)
from lightrag.fault_tree.tree_builder import build_fault_tree_draft

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "domain_packs" / "industrial_fault"
EXTRACTION = PACK / "seeds" / "extraction"
GOLD = PACK / "seeds" / "demo_bundle.json"


@pytest.fixture()
def imported():
    return build_imported_domain(PACK, EXTRACTION)


def test_seed_projection_rebuilds_the_gold_tree(imported) -> None:
    assert compare_tree(imported.tree, GOLD) == []
    assert imported.tree.validation.valid
    assert len(imported.tree.nodes) == 8
    assert len(imported.tree.edges) == 8


def test_imported_records_carry_evidence_and_targets(imported) -> None:
    evidence_ids = {
        item.evidence_id for item in imported.projected.evidence
    }
    assert evidence_ids
    for fact in imported.projected.facts:
        assert fact.evidence_ids
        assert set(fact.evidence_ids) <= evidence_ids
    for rule in imported.projected.rules:
        assert rule.evidence_ids
        assert set(rule.evidence_ids) <= evidence_ids
    for item in imported.projected.evidence:
        assert item.targets
        assert item.quote_hash.startswith("sha256:")


def test_projection_rejects_duplicate_chunk_ids(imported) -> None:
    batch = imported.workspace.batches[0]
    with pytest.raises(ProjectionError, match="duplicate chunk"):
        project_domain(
            OntologyRegistry.from_file(PACK / "ontology.yml"),
            tenant_id=imported.bundle.tenant_id,
            batches=[batch, batch],
            assets=imported.workspace.assets,
            schema_version=imported.bundle.schema_version,
        )


def test_projection_rejects_unknown_asset(imported) -> None:
    batch = imported.workspace.batches[0]
    unknown = SourceChunk(
        asset_id="FILE-NOT-LOADED",
        chunk_id=batch.chunk.chunk_id,
        text=batch.chunk.text,
    )
    with pytest.raises(ProjectionError, match="unknown asset"):
        project_domain(
            OntologyRegistry.from_file(PACK / "ontology.yml"),
            tenant_id=imported.bundle.tenant_id,
            batches=[CandidateBatchInput(chunk=unknown, batch=batch.batch)],
            assets=imported.workspace.assets,
            schema_version=imported.bundle.schema_version,
        )


def test_rule_without_grounded_evidence_is_dropped(imported) -> None:
    payload = next(
        entity
        for batch in imported.workspace.batches
        for entity in batch.batch.entities
        if entity.local_id == "lp"
    )
    target_rule = next(
        rule
        for rule in imported.projected.rules
        if rule.rule_uid == "RULE-LOW-PRESSURE-OR"
    )
    for candidate in payload.evidence:
        candidate.quote = "这句话不在原文中"
        candidate.char_start = None
        candidate.char_end = None

    projected = project_domain(
        OntologyRegistry.from_file(PACK / "ontology.yml"),
        tenant_id=imported.bundle.tenant_id,
        batches=imported.workspace.batches,
        assets=imported.workspace.assets,
        schema_version=imported.bundle.schema_version,
    )

    assert any("dropped" in warning for warning in projected.warnings)
    assert target_rule.rule_uid not in {
        rule.rule_uid for rule in projected.rules
    }
    remaining_targets = {
        (target.target_type, target.target_id)
        for item in projected.evidence
        for target in item.targets
    }
    assert ("rule", target_rule.rule_uid) not in remaining_targets


def _build_nested_tree(imported):
    rules = list(imported.projected.rules)
    top_rule = next(
        rule
        for rule in rules
        if rule.effect_fault_uid == "FAULT-LOW-PRESSURE"
    )
    top_rule.expression = UidExpression(
        op="AND",
        children=[
            top_rule.expression.model_copy(deep=True),
            UidExpression(event_uid="FAULT-CAVITATION"),
        ],
    )

    tree = build_fault_tree_draft(
        tenant_id=imported.bundle.tenant_id,
        ontology_version=imported.bundle.ontology_version,
        entities=imported.projected.entities,
        rules=rules,
        top_event_uid="FAULT-LOW-PRESSURE",
        draft_id="TREE-NESTED",
    )
    return tree, rules


def test_nested_gate_tree_projects_gate_to_gate_output(imported) -> None:
    tree, _rules = _build_nested_tree(imported)

    assert tree.validation.valid, tree.validation.issues
    gates = [node for node in tree.nodes if node.class_code == "LogicGate"]
    assert len(gates) == 3
    root_uid = next(
        edge.source_uid
        for edge in tree.edges
        if edge.relation_code == "OUTPUT_TO"
        and edge.target_uid == "FAULT-LOW-PRESSURE"
    )
    root = next(gate for gate in gates if gate.uid == root_uid)
    nested_or = next(
        gate
        for gate in gates
        if gate.properties.get("expression_path") == "0"
    )
    assert root.properties["gate_type"] == "AND"
    assert nested_or.properties["gate_type"] == "OR"
    assert root.properties["input_count"] == 2
    assert nested_or.properties["input_count"] == 2

    output_edges = {
        (edge.source_uid, edge.target_uid)
        for edge in tree.edges
        if edge.relation_code == "OUTPUT_TO"
    }
    assert (root.uid, "FAULT-LOW-PRESSURE") in output_edges
    assert (nested_or.uid, root.uid) in output_edges
    for gate in gates:
        assert (
            sum(
                1
                for edge in tree.edges
                if edge.relation_code == "OUTPUT_TO"
                and edge.source_uid == gate.uid
            )
            == 1
        )


def test_nested_gate_output_must_stay_in_its_rule(imported) -> None:
    tree, rules = _build_nested_tree(imported)
    nested_or = next(
        node
        for node in tree.nodes
        if node.class_code == "LogicGate"
        and node.properties.get("expression_path") == "0"
    )
    nested_or.properties["rule_uid"] = "RULE-FOREIGN"

    issues = validate_fault_tree_draft(
        tree,
        {rule.rule_uid: rule for rule in rules},
        {entity.uid: entity for entity in imported.projected.entities},
    )

    assert "tree-output-gate-rule" in {issue.code for issue in issues}


def test_nested_tree_projects_gate_to_gate_edges_to_the_graph(imported) -> None:
    tree, rules = _build_nested_tree(imported)
    bundle = imported.bundle.model_copy(update={"tree": tree, "rules": rules})

    projection = project_domain_graph(bundle)
    gate_uids = {
        node["uid"]
        for node in projection.nodes
        if node["label"] == "LogicGate"
    }
    output_edges = [
        edge for edge in projection.edges if edge["type"] == "OUTPUT_TO"
    ]
    assert len(output_edges) == 3
    assert (
        sum(
            1
            for edge in output_edges
            if edge["source_uid"] in gate_uids
            and edge["target_uid"] in gate_uids
        )
        == 1
    )
