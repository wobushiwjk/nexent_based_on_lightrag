"""Tests for the fault-diagnosis skill."""

from __future__ import annotations

import pytest

from skills.fault_diagnosis.handler import run
from skills.runtime import READ_ONLY_TOOLS
from tests.skills.fake_tools import FakeToolClient, envelope

pytestmark = pytest.mark.offline


async def test_diagnoses_low_pressure_with_verbatim_evidence(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "equipment_ref": "P-101A",
            "symptom": "出口压力不足",
        },
        tools,
    )

    assert response.ok
    assert response.status == "diagnosed"
    assert response.data is not None
    assert response.data.equipment.uid == "EQ-P101A"
    assert response.data.equipment.equipment_tag == "P-101A"
    assert response.data.query == "出口压力不足"

    top = response.data.candidates[0]
    assert top.fault_uid == "FAULT-LOW-PRESSURE"
    assert top.evidence_complete
    assert top.evidence_count >= 2
    assert all(ref.quote for ref in top.evidence)
    assert all(ref.quote_hash for ref in top.evidence)
    assert any("入口过滤器堵塞" in ref.quote for ref in top.evidence)

    assert response.data.fault_tree is not None
    assert (
        response.data.fault_tree.draft_id
        == "TREE-P101A-LOW-PRESSURE-v1"
    )
    assert response.data.fault_tree.validation is not None
    assert response.data.fault_tree.validation.valid
    assert "MCP 工具" in response.data.conclusion

    tool_names = [call.tool for call in response.tool_calls]
    assert tool_names[:2] == ["get_ontology", "get_equipment"]
    assert set(tool_names) <= READ_ONLY_TOOLS
    assert all(call.ok for call in response.tool_calls)


async def test_unknown_equipment_returns_structured_not_found(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "equipment_ref": "P-999",
            "symptom": "出口压力不足",
        },
        tools,
    )

    assert not response.ok
    assert response.status == "not_found"
    assert response.error is not None
    assert response.error.code == "not_found"
    assert response.data is None


async def test_ambiguous_equipment_is_reported_with_candidates(demo_service):
    def ambiguous(_: dict) -> dict:
        return envelope(
            ok=False,
            code="ambiguous_selector",
            message="equipment reference matches more than one record",
            details={"candidates": ["EQ-1", "EQ-2"]},
        )

    tools = FakeToolClient(
        demo_service, overrides={"get_equipment": ambiguous}
    )
    response = await run(
        {"tenant_id": "tenant-demo", "equipment_ref": "pump"},
        tools,
    )

    assert not response.ok
    assert response.status == "invalid_input"
    assert response.error is not None
    assert response.error.code == "ambiguous_selector"
    assert response.error.details["candidates"] == ["EQ-1", "EQ-2"]


async def test_no_candidate_returns_insufficient_evidence(demo_service):
    def no_matches(arguments: dict) -> dict:
        return envelope(
            data={
                "query": arguments["query"],
                "limit": arguments["limit"],
                "count": 0,
                "total_faults": 4,
                "results": [],
                "hint": "No fault matched the query.",
            }
        )

    tools = FakeToolClient(
        demo_service, overrides={"search_faults": no_matches}
    )
    response = await run(
        {
            "tenant_id": "tenant-demo",
            "equipment_ref": "P-101A",
            "symptom": "从未出现过的症状",
        },
        tools,
    )

    assert not response.ok
    assert response.status == "needs_more_evidence"
    assert response.error is not None
    assert response.error.code == "insufficient_evidence"
    assert response.data is not None
    assert response.data.candidates == []
    assert "未生成诊断结论" in response.data.conclusion


async def test_missing_evidence_marks_candidates_without_fabricating(demo_service):
    def no_evidence(arguments: dict) -> dict:
        return envelope(
            ok=False,
            code="not_found",
            message=(
                f"{arguments['target_type']} {arguments['target_id']} "
                "was not found"
            ),
            details=arguments,
        )

    tools = FakeToolClient(
        demo_service, overrides={"trace_evidence": no_evidence}
    )
    response = await run(
        {
            "tenant_id": "tenant-demo",
            "equipment_ref": "P-101A",
            "symptom": "出口压力不足",
        },
        tools,
    )

    assert not response.ok
    assert response.status == "needs_more_evidence"
    assert response.error is not None
    assert response.error.code == "insufficient_evidence"
    assert response.data is not None
    assert response.data.candidates
    assert all(
        item.evidence_count == 0 for item in response.data.candidates
    )
    assert all(
        not item.evidence_complete for item in response.data.candidates
    )
    assert any("证据查询失败" in message for message in response.warnings)


async def test_missing_tree_is_a_warning_not_a_failure(demo_service):
    def no_tree(_: dict) -> dict:
        return envelope(
            ok=False,
            code="not_found",
            message="tenant has no fault-tree draft",
            details={"tenant_id": "tenant-demo"},
        )

    tools = FakeToolClient(
        demo_service, overrides={"get_fault_tree": no_tree}
    )
    response = await run(
        {
            "tenant_id": "tenant-demo",
            "equipment_ref": "P-101A",
            "symptom": "出口压力不足",
        },
        tools,
    )

    assert response.ok
    assert response.status == "diagnosed"
    assert response.data is not None
    assert response.data.fault_tree is None
    assert any("没有与候选故障匹配的故障树" in w for w in response.warnings)


async def test_invalid_request_returns_structured_error(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run({"tenant_id": "tenant-demo"}, tools)

    assert not response.ok
    assert response.status == "invalid_input"
    assert response.error is not None
    assert response.error.code == "invalid_argument"
    assert response.tool_calls == []
    assert tools.calls == []


async def test_candidate_order_prefers_linked_equipment_faults(demo_service):
    """A HAS_FAULT link outranks a lexical-only search hit."""

    tools = FakeToolClient(demo_service)
    equipment_override = envelope(
        data={
            "match": {"input": "P-101A", "matched_by": "equipment_tag"},
            "equipment": {
                "uid": "EQ-P101A",
                "canonical_name": "冷却水泵",
                "equipment_tag": "P-101A",
                "equipment_name": "冷却水泵",
            },
            "departments": [],
            "faults": [
                {
                    "uid": "FAULT-INLET-LEAK",
                    "canonical_name": "吸入管法兰漏气",
                    "severity": "medium",
                    "evidence_ids": ["EV-MANUAL-3"],
                    "evidence_count": 1,
                    "cause_rule_uids": [],
                }
            ],
            "facts": [],
            "source_files": [],
            "evidence": [],
        }
    )
    tools.overrides["get_equipment"] = equipment_override

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "equipment_ref": "P-101A",
            "symptom": "出口压力不足",
            "include_tree": False,
        },
        tools,
    )

    assert response.ok
    assert response.data is not None
    assert response.data.candidates[0].fault_uid == "FAULT-INLET-LEAK"
    assert response.data.candidates[0].linked_by_equipment
