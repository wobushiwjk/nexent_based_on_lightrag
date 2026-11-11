"""End-to-end tests for ``scripts/run_skill.py`` over real MCP stdio."""

from __future__ import annotations

import json

import pytest

from scripts.run_skill import main, run
from tests.mcp.helpers import DEMO_BUNDLE, PACK_DIR

pytestmark = pytest.mark.offline


async def test_fault_diagnosis_runs_over_mcp_stdio():
    response, code = await run(
        skill="fault_diagnosis",
        payload={
            "tenant_id": "tenant-demo",
            "equipment_ref": "P-101A",
            "symptom": "出口压力不足",
        },
        pack=PACK_DIR,
        bundle=DEMO_BUNDLE,
        storage_dir=None,
    )

    assert code == 0
    assert response.ok
    assert response.status == "diagnosed"
    assert response.data is not None
    assert response.data.candidates[0].fault_uid == "FAULT-LOW-PRESSURE"
    assert response.meta.ontology_version == "industrial-fault-v0.1"
    assert all(call.ok for call in response.tool_calls)


async def test_evidence_verification_runs_over_mcp_stdio():
    response, code = await run(
        skill="evidence_verification",
        payload={
            "tenant_id": "tenant-demo",
            "claims": [
                {
                    "claim_id": "claim-fact-1",
                    "target_type": "fact",
                    "target_id": "FACT-1",
                }
            ],
        },
        pack=PACK_DIR,
        bundle=DEMO_BUNDLE,
        storage_dir=None,
    )

    assert code == 0
    assert response.ok
    assert response.status == "verified"
    assert response.data is not None
    assert response.data.verified_count == 1


async def test_ontology_draft_runs_over_mcp_stdio():
    response, code = await run(
        skill="ontology_change_draft",
        payload={
            "tenant_id": "tenant-demo",
            "rationale": "冷却水泵需要单独记录振动传感器。",
            "change": {
                "change_type": "add_class",
                "code": "Sensor",
                "label": "传感器",
                "parent": "Asset",
                "required_fields": ["uid"],
            },
        },
        pack=PACK_DIR,
        bundle=DEMO_BUNDLE,
        storage_dir=None,
    )

    assert code == 0
    assert response.ok
    assert response.status == "draft"
    assert response.data is not None
    assert response.data.proposed_version == "industrial-fault-v0.2"


async def test_business_failure_returns_exit_code_one():
    response, code = await run(
        skill="fault_diagnosis",
        payload={"tenant_id": "tenant-demo", "equipment_ref": "P-999"},
        pack=PACK_DIR,
        bundle=DEMO_BUNDLE,
        storage_dir=None,
    )

    assert code == 1
    assert not response.ok
    assert response.status == "not_found"


def test_main_prints_the_envelope_and_returns_the_skill_exit_code(tmp_path, capsys):
    payload_path = tmp_path / "request.json"
    payload_path.write_text(
        json.dumps(
            {
                "tenant_id": "tenant-demo",
                "claims": [
                    {
                        "claim_id": "claim-missing",
                        "target_type": "fact",
                        "target_id": "FACT-404",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "--skill",
            "evidence_verification",
            "--input",
            str(payload_path),
            "--pack",
            str(PACK_DIR),
            "--bundle",
            str(DEMO_BUNDLE),
        ]
    )

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert code == 0
    assert envelope["status"] == "rejected"
    assert envelope["meta"]["skill"] == "evidence_verification"


def test_main_rejects_unreadable_input(tmp_path, capsys):
    missing = tmp_path / "missing.json"

    code = main(
        [
            "--skill",
            "fault_diagnosis",
            "--input",
            str(missing),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "cannot read skill input" in captured.err


def test_main_rejects_non_object_input(tmp_path, capsys):
    array_path = tmp_path / "array.json"
    array_path.write_text("[1, 2, 3]", encoding="utf-8")

    code = main(
        [
            "--skill",
            "fault_diagnosis",
            "--input",
            str(array_path),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "JSON object" in captured.err
