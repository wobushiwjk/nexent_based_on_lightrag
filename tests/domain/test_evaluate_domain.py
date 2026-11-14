"""Regression test for the deterministic domain capability benchmark."""

from __future__ import annotations

from scripts.evaluate_domain import evaluate


def test_domain_evaluation_proves_graph_evidence_and_tree_uplift() -> None:
    report = evaluate(storage_dir=None)

    assert report["ok"] is True
    assert report["checks"]["tree_schema_valid"] is True
    assert report["checks"]["full_graph_retrieval"] is True
    assert report["checks"]["full_graph_evidence"] is True
    assert report["checks"]["full_graph_tree_causes"] is True
    assert report["checks"]["full_graph_end_to_end"] is True
    assert report["checks"]["graph_improves_evidence"] is True
    assert report["checks"]["graph_improves_tree"] is True

    baseline = report["variants"]["lexical_no_graph"]
    full = report["variants"]["graph_evidence_full"]
    assert baseline["retrieval_hit"] == 1.0
    assert baseline["evidence"]["coverage"] == 0.0
    assert baseline["tree_structure_match"] is False
    assert full["evidence"]["coverage"] == 1.0
    assert full["evidence"]["hash_valid_rate"] == 1.0
    assert full["tree_structure_match"] is True
