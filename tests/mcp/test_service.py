"""Behavior of the six read-only domain tools."""

from __future__ import annotations

import copy

import pytest

from mcp_server.errors import DomainError
from mcp_server.repository import DomainRepository
from mcp_server.service import DomainService
from tests.mcp.helpers import PACK_DIR, directory_digest, retag_bundle

pytestmark = pytest.mark.offline


def test_get_ontology_exposes_classes_relations_and_constraints(service):
    data = service.get_ontology()

    assert data["ontology_version"] == "industrial-fault-v0.1"
    assert data["schema_version"] == "industrial-fault-schema-v0.1"
    assert data["counts"] == {
        "classes": 9,
        "relations": 11,
        "constraints": 8,
        "standards": 4,
    }
    fault_event = next(item for item in data["classes"] if item["code"] == "FaultEvent")
    assert fault_event["required_fields"] == ["name", "description"]
    assert "IEC 61025" in fault_event["standards"]
    causes = next(item for item in data["relations"] if item["code"] == "CAUSES")
    assert causes["source_types"] == ["FaultEvent"]
    assert causes["target_types"] == ["FaultEvent"]
    assert any(item["id"] == "evidence-verbatim" for item in data["constraints"])


@pytest.mark.parametrize(
    ("equipment_ref", "matched_by"),
    [
        ("P-101A", "equipment_tag"),
        ("EQ-P101A", "uid"),
        ("冷却泵", "name"),
    ],
)
def test_get_equipment_resolves_uid_tag_and_alias(
    service,
    equipment_ref,
    matched_by,
):
    data = service.get_equipment("tenant-demo", equipment_ref)

    assert data["match"]["matched_by"] == matched_by
    assert data["equipment"]["uid"] == "EQ-P101A"
    assert data["equipment"]["equipment_tag"] == "P-101A"
    assert [item["uid"] for item in data["source_files"]] == ["FILE-P101A-MANUAL"]
    assert [item["evidence_id"] for item in data["evidence"]] == ["EV-MANUAL-1"]


def test_get_equipment_unknown_is_not_found(service):
    with pytest.raises(DomainError) as exc_info:
        service.get_equipment("tenant-demo", "P-999")

    assert exc_info.value.code == "not_found"
    assert exc_info.value.details == {"equipment_ref": "P-999"}


def test_search_faults_ranks_the_named_fault_first(service):
    data = service.search_faults("tenant-demo", "出口压力不足", 10)

    assert data["count"] == 2
    assert data["results"][0]["uid"] == "FAULT-LOW-PRESSURE"
    assert data["results"][0]["score"] > data["results"][1]["score"]
    assert "出口压力不足" in data["results"][0]["matched_terms"]


def test_search_faults_matches_aliases_and_uids(service):
    alias_result = service.search_faults("tenant-demo", "压力低", 5)
    uid_result = service.search_faults("tenant-demo", "FAULT-CAVITATION", 5)

    assert alias_result["results"][0]["uid"] == "FAULT-LOW-PRESSURE"
    assert uid_result["results"][0]["uid"] == "FAULT-CAVITATION"


def test_search_faults_returns_an_empty_result_with_a_hint(service):
    data = service.search_faults("tenant-demo", "不存在的故障", 5)

    assert data["count"] == 0
    assert data["results"] == []
    assert "hint" in data


@pytest.mark.parametrize("limit", [0, 51, True, "5"])
def test_search_faults_rejects_invalid_limits(service, limit):
    with pytest.raises(DomainError) as exc_info:
        service.search_faults("tenant-demo", "汽蚀", limit)
    assert exc_info.value.code == "invalid_argument"


def test_search_faults_rejects_an_empty_query(service):
    with pytest.raises(DomainError) as exc_info:
        service.search_faults("tenant-demo", "   ", 5)
    assert exc_info.value.code == "invalid_argument"


def test_get_fault_tree_by_draft_id_and_top_event(service):
    by_draft = service.get_fault_tree(
        "tenant-demo",
        draft_id="TREE-P101A-LOW-PRESSURE-v1",
    )
    by_top = service.get_fault_tree(
        "tenant-demo",
        top_event_uid="FAULT-LOW-PRESSURE",
    )

    assert by_draft["draft"]["draft_id"] == by_top["draft"]["draft_id"]
    assert by_draft["counts"] == {
        "nodes": 8,
        "edges": 8,
        "gates": 2,
        "faults": 4,
        "rules": 2,
    }
    assert by_draft["top_event"]["uid"] == "FAULT-LOW-PRESSURE"
    assert {item["rule_uid"] for item in by_draft["rule_expressions"]} == {
        "RULE-LOW-PRESSURE-OR",
        "RULE-CAVITATION-AND",
    }


def test_get_fault_tree_with_no_selector_returns_the_only_tree(service):
    data = service.get_fault_tree("tenant-demo")
    assert data["draft"]["draft_id"] == "TREE-P101A-LOW-PRESSURE-v1"


def test_get_fault_tree_rejects_both_selectors(service):
    with pytest.raises(DomainError) as exc_info:
        service.get_fault_tree(
            "tenant-demo",
            draft_id="TREE-P101A-LOW-PRESSURE-v1",
            top_event_uid="FAULT-LOW-PRESSURE",
        )
    assert exc_info.value.code == "invalid_argument"


def test_get_fault_tree_unknown_is_not_found(service):
    with pytest.raises(DomainError) as exc_info:
        service.get_fault_tree("tenant-demo", draft_id="TREE-MISSING")
    assert exc_info.value.code == "not_found"


def test_trace_evidence_returns_cross_document_quotes(service):
    data = service.trace_evidence("tenant-demo", "entity", "FAULT-FILTER-BLOCKED")

    assert data["target"]["uid"] == "FAULT-FILTER-BLOCKED"
    assert [item["evidence_id"] for item in data["evidence"]] == [
        "EV-TABLE-1",
        "EV-RECORD-1",
        "EV-MANUAL-1",
        "EV-MANUAL-3",
    ]
    assert {item["asset"]["file_name"] for item in data["evidence"]} == {
        "pump_manual_excerpt.md",
        "maintenance_record.md",
        "inspection_table.md",
    }
    assert data["evidence"][0]["via"] == ["record", "target"]


def test_trace_evidence_for_a_rule(service):
    data = service.trace_evidence(
        "tenant-demo",
        "rule",
        "RULE-CAVITATION-AND",
    )
    assert [item["evidence_id"] for item in data["evidence"]] == [
        "EV-RECORD-2",
        "EV-MANUAL-3",
    ]


def test_trace_evidence_for_a_logic_gate_uses_its_rule(service):
    data = service.trace_evidence(
        "tenant-demo",
        "logic_gate",
        "GATE-AND-CAVITATION",
    )

    assert [item["evidence_id"] for item in data["evidence"]] == [
        "EV-RECORD-2",
        "EV-MANUAL-3",
    ]
    assert all("rule:RULE-CAVITATION-AND" in item["via"] for item in data["evidence"])


def test_trace_evidence_for_a_decision_step(service):
    data = service.trace_evidence("tenant-demo", "decision_step", "step-3")

    assert data["target"]["trace_id"] == "TRACE-P101A-001"
    assert data["target"]["step"]["step_type"] == "verify"
    assert [item["evidence_id"] for item in data["evidence"]] == [
        "EV-RECORD-2",
        "EV-MANUAL-3",
    ]


def test_trace_evidence_unknown_target_is_not_found(service):
    with pytest.raises(DomainError) as exc_info:
        service.trace_evidence("tenant-demo", "fact", "FACT-MISSING")
    assert exc_info.value.code == "not_found"


def test_trace_evidence_rejects_an_unknown_target_type(service):
    with pytest.raises(DomainError) as exc_info:
        service.trace_evidence("tenant-demo", "equipment", "EQ-P101A")
    assert exc_info.value.code == "invalid_argument"


def test_validate_stored_tree_is_valid(service):
    data = service.validate_fault_tree(
        "tenant-demo",
        draft_id="TREE-P101A-LOW-PRESSURE-v1",
    )

    assert data["valid"] is True
    assert data["error_count"] == 0
    assert data["warning_count"] == 0
    assert data["issues"] == []


def test_validate_inline_tree_reports_a_broken_gate(service):
    tree = service.get_fault_tree("tenant-demo")["draft"]
    broken = copy.deepcopy(tree)
    broken["edges"] = [
        edge
        for edge in broken["edges"]
        if not (
            edge["source_uid"] == "FAULT-INLET-LEAK"
            and edge["relation_code"] == "INPUT_OF"
        )
    ]

    data = service.validate_fault_tree("tenant-demo", draft=broken)

    assert data["valid"] is False
    codes = {issue["code"] for issue in data["issues"]}
    assert "gate-too-few-inputs" in codes
    assert "gate-input-count" in codes


def test_validate_inline_tree_reports_an_unknown_fault(service):
    tree = service.get_fault_tree("tenant-demo")["draft"]
    broken = copy.deepcopy(tree)
    broken["nodes"].append(
        {
            "uid": "FAULT-UNKNOWN",
            "class_code": "FaultEvent",
            "label": "未知故障",
            "properties": {},
        }
    )

    data = service.validate_fault_tree("tenant-demo", draft=broken)

    assert data["valid"] is False
    codes = {issue["code"] for issue in data["issues"]}
    assert "tree-node-entity-missing" in codes
    assert "tree-unreachable-node" in codes


def test_validate_inline_tree_rejects_another_tenant(service):
    tree = service.get_fault_tree("tenant-demo")["draft"]
    tree = copy.deepcopy(tree)
    tree["tenant_id"] = "tenant-other"

    with pytest.raises(DomainError) as exc_info:
        service.validate_fault_tree("tenant-demo", draft=tree)

    assert exc_info.value.code == "invalid_argument"
    assert exc_info.value.details == {"tenant_id": "tenant-other"}


def test_validate_inline_tree_rejects_a_malformed_draft(service):
    with pytest.raises(DomainError) as exc_info:
        service.validate_fault_tree("tenant-demo", draft={"draft_id": "broken"})

    assert exc_info.value.code == "invalid_argument"
    assert exc_info.value.details["errors"]


def test_validate_requires_exactly_one_selector(service):
    with pytest.raises(DomainError) as exc_info:
        service.validate_fault_tree("tenant-demo")
    assert exc_info.value.code == "invalid_argument"

    with pytest.raises(DomainError) as exc_info:
        service.validate_fault_tree(
            "tenant-demo",
            draft_id="TREE-P101A-LOW-PRESSURE-v1",
            draft={},
        )
    assert exc_info.value.code == "invalid_argument"


def test_tools_do_not_modify_the_domain_pack(service):
    before = directory_digest(PACK_DIR)

    service.get_ontology()
    service.get_equipment("tenant-demo", "P-101A")
    service.search_faults("tenant-demo", "出口压力不足", 5)
    tree = service.get_fault_tree("tenant-demo")["draft"]
    service.trace_evidence("tenant-demo", "entity", "FAULT-FILTER-BLOCKED")
    service.validate_fault_tree("tenant-demo", draft=tree)

    after = directory_digest(PACK_DIR)
    assert after == before
    assert set(after) == set(before)


def test_tenants_are_isolated(demo_bundle):
    other = retag_bundle(
        demo_bundle,
        "tenant-other",
        rename_fault_uid="FAULT-CAVITATION",
        canonical_name="其他租户专用故障",
    )
    repository = DomainRepository(PACK_DIR, [demo_bundle, other])
    service = DomainService(repository)

    demo_result = service.search_faults("tenant-demo", "其他租户专用故障", 5)
    other_result = service.search_faults("tenant-other", "其他租户专用故障", 5)
    equipment = service.get_equipment("tenant-other", "P-101A")

    assert demo_result["count"] == 0
    assert other_result["count"] == 1
    assert other_result["results"][0]["uid"] == "FAULT-CAVITATION"
    assert equipment["equipment"]["tenant_id"] == "tenant-other"
