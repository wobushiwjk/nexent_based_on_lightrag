"""Tests for the ontology-change-draft skill."""

from __future__ import annotations

import pytest

from skills.ontology_change_draft.handler import run
from tests.skills.fake_tools import FakeToolClient, envelope

pytestmark = pytest.mark.offline


async def test_add_class_returns_a_reviewable_draft(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "巡检表需要单独记录传感器，现有类无法表达。",
            "change": {
                "change_type": "add_class",
                "code": "Sensor",
                "label": "传感器",
                "parent": "Asset",
                "required_fields": ["uid", "sensor_type"],
                "standards": ["ISO 13374"],
            },
        },
        tools,
    )

    assert response.ok
    assert response.status == "draft"
    assert response.data is not None
    assert response.data.current_version == "industrial-fault-v0.1"
    assert response.data.proposed_version == "industrial-fault-v0.2"
    assert response.data.change["change_type"] == "add_class"

    diff = response.data.diff
    assert [entry.code for entry in diff.added] == ["Sensor"]
    assert diff.added[0].kind == "class"
    assert diff.removed == []
    assert diff.modified == []

    assert response.data.conflicts == []
    assert response.data.impact.affected_classes == ["Sensor"]
    assert response.data.impact.affected_schemas == [
        "schemas/entity.schema.json#/$defs/Sensor"
    ]
    assert not response.data.impact.instance_migration_required

    review = response.data.review
    assert review.required
    assert not review.applied
    assert review.next_workflow == "W8"
    assert any("schema" in item for item in review.checklist)

    assert [call.tool for call in response.tool_calls] == ["get_ontology"]


async def test_duplicate_class_is_rejected(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "重复定义用于验证冲突检测。",
            "change": {
                "change_type": "add_class",
                "code": "Equipment",
                "label": "重复的设备类",
            },
        },
        tools,
    )

    assert response.ok
    assert response.status == "rejected"
    assert response.data is not None
    blocking = [
        item for item in response.data.conflicts if item.blocking
    ]
    assert [item.code for item in blocking] == ["class_exists"]
    assert not response.data.review.applied


async def test_unknown_parent_class_is_rejected(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "验证父类存在性检查。",
            "change": {
                "change_type": "add_class",
                "code": "Sensor",
                "label": "传感器",
                "parent": "PhysicalThing",
                "required_fields": ["uid"],
            },
        },
        tools,
    )

    assert response.status == "rejected"
    assert response.data is not None
    assert "unknown_parent" in {
        item.code for item in response.data.conflicts if item.blocking
    }


async def test_relation_with_unknown_endpoint_is_rejected(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "传感器需要关联到设备。",
            "change": {
                "change_type": "add_relation",
                "code": "MONITORS",
                "source_types": ["Sensor"],
                "target_types": ["Equipment"],
            },
        },
        tools,
    )

    assert response.status == "rejected"
    assert response.data is not None
    assert [entry.kind for entry in response.data.diff.added] == [
        "relation"
    ]
    assert "unknown_endpoint" in {
        item.code for item in response.data.conflicts if item.blocking
    }


async def test_relation_without_evidence_is_a_warning(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "验证非阻断警告。",
            "change": {
                "change_type": "add_relation",
                "code": "MONITORS",
                "source_types": ["Equipment"],
                "target_types": ["FaultEvent"],
                "evidence_required": False,
            },
        },
        tools,
    )

    assert response.ok
    assert response.status == "draft"
    assert response.data is not None
    conflicts = {item.code: item for item in response.data.conflicts}
    assert conflicts["evidence_not_required"].blocking is False


async def test_duplicate_constraint_is_rejected(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "重复约束用于验证冲突检测。",
            "change": {
                "change_type": "add_constraint",
                "id": "gate-min-inputs",
                "applies_to": ["LogicGate"],
                "rule": "每个门至少两个输入。",
            },
        },
        tools,
    )

    assert response.status == "rejected"
    assert response.data is not None
    assert "constraint_exists" in {
        item.code for item in response.data.conflicts if item.blocking
    }


async def test_constraint_on_unknown_class_is_rejected(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "约束目标类尚不存在。",
            "change": {
                "change_type": "add_constraint",
                "id": "sensor-evidence-required",
                "applies_to": ["Sensor"],
                "rule": "传感器必须关联证据。",
            },
        },
        tools,
    )

    assert response.status == "rejected"
    assert response.data is not None
    assert "unknown_constraint_target" in {
        item.code for item in response.data.conflicts if item.blocking
    }


async def test_deprecating_a_parent_class_is_blocked_by_children(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "资产抽象层需要重构。",
            "change": {
                "change_type": "deprecate_class",
                "code": "Asset",
            },
        },
        tools,
    )

    assert response.status == "rejected"
    assert response.data is not None
    assert [entry.code for entry in response.data.diff.modified] == [
        "Asset"
    ]
    conflict = next(
        item
        for item in response.data.conflicts
        if item.code == "child_classes_present"
    )
    assert conflict.blocking
    assert "Department" in conflict.message
    assert response.data.impact.instance_migration_required


async def test_deprecating_a_leaf_class_reports_relation_impact(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "部门模型将迁移到外部 CMDB。",
            "change": {
                "change_type": "deprecate_class",
                "code": "Department",
                "replacement": None,
            },
        },
        tools,
    )

    assert response.ok
    assert response.status == "draft"
    assert response.data is not None
    assert set(response.data.impact.affected_relations) == {
        "HAS_EQUIPMENT",
        "HAS_SUB_DEPARTMENT",
        "OWNS_FILE",
    }
    warning = next(
        item
        for item in response.data.conflicts
        if item.code == "relations_reference_class"
    )
    assert not warning.blocking
    assert response.data.impact.instance_migration_required


async def test_self_replacement_is_rejected(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "验证替换类检查。",
            "change": {
                "change_type": "deprecate_class",
                "code": "Department",
                "replacement": "Department",
            },
        },
        tools,
    )

    assert response.status == "rejected"
    assert response.data is not None
    assert "self_replacement" in {
        item.code for item in response.data.conflicts if item.blocking
    }


async def test_source_targets_attach_verbatim_evidence(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "维修记录支持新增传感器类。",
            "change": {
                "change_type": "add_class",
                "code": "Sensor",
                "label": "传感器",
                "parent": "Asset",
                "required_fields": ["uid"],
            },
            "source_targets": [
                {"target_type": "fact", "target_id": "FACT-1"}
            ],
        },
        tools,
    )

    assert response.status == "draft"
    assert response.data is not None
    assert len(response.data.source_evidence) == 3
    assert {
        ref.evidence_id for ref in response.data.source_evidence
    } == {"EV-MANUAL-1", "EV-RECORD-1", "EV-TABLE-1"}
    assert all(ref.quote for ref in response.data.source_evidence)
    assert [call.tool for call in response.tool_calls] == [
        "get_ontology",
        "trace_evidence",
    ]


async def test_missing_source_target_is_a_warning_not_a_failure(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "来源记录可能尚未导入。",
            "change": {
                "change_type": "add_class",
                "code": "Sensor",
                "label": "传感器",
                "parent": "Asset",
                "required_fields": ["uid"],
            },
            "source_targets": [
                {"target_type": "fact", "target_id": "FACT-404"}
            ],
        },
        tools,
    )

    assert response.ok
    assert response.status == "draft"
    assert response.data is not None
    assert response.data.source_evidence == []
    assert any("无法追溯" in w for w in response.warnings)


async def test_invalid_request_returns_structured_error(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "change": {
                "change_type": "add_class",
                "code": "Sensor",
                "label": "传感器",
            },
        },
        tools,
    )

    assert not response.ok
    assert response.status == "invalid_input"
    assert response.error is not None
    assert response.error.code == "invalid_argument"
    assert tools.calls == []


async def test_ontology_failure_is_reported(demo_service):
    def broken(_: dict) -> dict:
        return envelope(
            ok=False,
            code="upstream_error",
            message="ontology store unavailable",
            details={"tool": "get_ontology"},
        )

    tools = FakeToolClient(
        demo_service, overrides={"get_ontology": broken}
    )
    response = await run(
        {
            "tenant_id": "tenant-demo",
            "rationale": "验证上游错误传播。",
            "change": {
                "change_type": "add_class",
                "code": "Sensor",
                "label": "传感器",
                "required_fields": ["uid"],
            },
        },
        tools,
    )

    assert not response.ok
    assert response.status == "failed"
    assert response.error is not None
    assert response.error.code == "upstream_error"
