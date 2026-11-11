"""Tests for the evidence-verification skill."""

from __future__ import annotations

import pytest

from skills.evidence_verification.handler import run
from skills.runtime import quote_digest
from tests.skills.fake_tools import FakeToolClient, envelope

pytestmark = pytest.mark.offline


def _evidence_payload(
    *,
    evidence_id: str = "EV-TAMPERED",
    quote: str = "被篡改的引文",
    quote_hash: str | None = None,
    char_start: int = 0,
    char_end: int | None = None,
    target_type: str = "fact",
    target_id: str = "FACT-1",
    review_status: str = "approved",
) -> dict:
    return {
        "evidence_id": evidence_id,
        "asset_id": "FILE-P101A-MANUAL",
        "asset": {
            "uid": "FILE-P101A-MANUAL",
            "file_name": "pump_manual_excerpt.md",
            "modality": "text",
        },
        "quote": quote,
        "quote_hash": quote_hash or quote_digest(quote),
        "page": 12,
        "section_path": "4.2 出口压力不足",
        "char_start": char_start,
        "char_end": (
            char_end if char_end is not None else char_start + len(quote)
        ),
        "review_status": review_status,
        "targets": [
            {
                "target_type": target_type,
                "target_id": target_id,
                "role": "supports",
            }
        ],
        "via": ["record"],
    }


async def test_verifies_fact_and_rule_evidence(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "claims": [
                {
                    "claim_id": "claim-fact-1",
                    "target_type": "fact",
                    "target_id": "FACT-1",
                },
                {
                    "claim_id": "claim-rule-and",
                    "target_type": "rule",
                    "target_id": "RULE-CAVITATION-AND",
                },
            ],
        },
        tools,
    )

    assert response.ok
    assert response.status == "verified"
    assert response.data is not None
    assert response.data.verified_count == 2
    assert response.data.rejected_count == 0
    assert response.data.tree_validation is None

    checks = {item.claim_id: item for item in response.data.claims}
    fact = checks["claim-fact-1"]
    assert fact.exists
    assert fact.supported
    assert fact.evidence_count == 3
    assert fact.checks["all_hashes_match"]
    assert fact.checks["all_spans_match"]
    assert fact.checks["all_assets_present"]
    assert fact.issues == []

    rule = checks["claim-rule-and"]
    assert rule.supported
    assert rule.evidence_count >= 2
    assert {ref.evidence_id for ref in rule.evidence} >= {
        "EV-MANUAL-3",
        "EV-RECORD-2",
    }

    assert set(call.tool for call in response.tool_calls) == {"trace_evidence"}


async def test_tampered_quote_fails_hash_and_rejects_claim(demo_service):
    tampered = envelope(
        data={
            "target_type": "fact",
            "target_id": "FACT-1",
            "target": {"fact_id": "FACT-1"},
            "count": 1,
            "evidence": [
                _evidence_payload(
                    quote="被篡改的引文",
                    quote_hash=quote_digest("原始引文"),
                )
            ],
        }
    )
    tools = FakeToolClient(
        demo_service, overrides={"trace_evidence": tampered}
    )

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "claims": [
                {
                    "claim_id": "claim-tampered",
                    "target_type": "fact",
                    "target_id": "FACT-1",
                }
            ],
        },
        tools,
    )

    assert response.ok
    assert response.status == "rejected"
    assert response.data is not None
    assert response.data.verified_count == 0
    claim = response.data.claims[0]
    assert not claim.supported
    assert not claim.checks["all_hashes_match"]
    assert "EV-TAMPERED:hash_matches" in claim.issues


async def test_unknown_target_is_a_per_claim_rejection(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "claims": [
                {
                    "claim_id": "claim-missing",
                    "target_type": "fact",
                    "target_id": "FACT-404",
                }
            ],
        },
        tools,
    )

    assert response.ok
    assert response.status == "rejected"
    assert response.data is not None
    claim = response.data.claims[0]
    assert not claim.exists
    assert not claim.supported
    assert claim.evidence_count == 0
    assert claim.issues == ["target_not_found"]


async def test_mixed_claims_are_partially_verified(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "claims": [
                {
                    "claim_id": "claim-ok",
                    "target_type": "fact",
                    "target_id": "FACT-1",
                },
                {
                    "claim_id": "claim-missing",
                    "target_type": "fact",
                    "target_id": "FACT-404",
                },
            ],
        },
        tools,
    )

    assert response.ok
    assert response.status == "partially_verified"
    assert response.data is not None
    assert response.data.verified_count == 1
    assert response.data.rejected_count == 1
    assert "1 条声明通过" in response.data.verdict


async def test_draft_only_request_validates_the_tree(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "draft_id": "TREE-P101A-LOW-PRESSURE-v1",
        },
        tools,
    )

    assert response.ok
    assert response.status == "verified"
    assert response.data is not None
    assert response.data.claims == []
    assert response.data.tree_validation is not None
    assert response.data.tree_validation.valid
    assert "校验通过" in response.data.verdict


async def test_unknown_draft_is_reported_as_not_found(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run(
        {
            "tenant_id": "tenant-demo",
            "draft_id": "TREE-NOT-THERE",
        },
        tools,
    )

    assert not response.ok
    assert response.status == "not_found"
    assert response.error is not None
    assert response.error.code == "not_found"


async def test_invalid_tree_is_reported_but_not_a_skill_error(demo_service):
    def invalid(_: dict) -> dict:
        return envelope(
            data={
                "draft_id": "TREE-P101A-LOW-PRESSURE-v1",
                "top_event_uid": "FAULT-LOW-PRESSURE",
                "valid": False,
                "error_count": 1,
                "warning_count": 0,
                "issues": [
                    {
                        "code": "gate-min-inputs",
                        "severity": "error",
                        "message": "AND gate has one input",
                        "target_id": "GATE-AND-CAVITATION",
                    }
                ],
            }
        )

    tools = FakeToolClient(
        demo_service, overrides={"validate_fault_tree": invalid}
    )
    response = await run(
        {
            "tenant_id": "tenant-demo",
            "draft_id": "TREE-P101A-LOW-PRESSURE-v1",
        },
        tools,
    )

    assert response.ok
    assert response.status == "rejected"
    assert response.data is not None
    assert response.data.tree_validation is not None
    assert not response.data.tree_validation.valid
    assert response.data.tree_validation.error_count == 1
    assert any("未通过校验" in w for w in response.warnings)


async def test_request_without_claims_or_draft_is_invalid(demo_service):
    tools = FakeToolClient(demo_service)

    response = await run({"tenant_id": "tenant-demo"}, tools)

    assert not response.ok
    assert response.status == "invalid_input"
    assert response.error is not None
    assert response.error.code == "invalid_argument"
    assert tools.calls == []


async def test_non_not_found_tool_error_stops_the_run(demo_service):
    def broken(_: dict) -> dict:
        return envelope(
            ok=False,
            code="upstream_error",
            message="backend unavailable",
            details={"tool": "trace_evidence"},
        )

    tools = FakeToolClient(
        demo_service, overrides={"trace_evidence": broken}
    )
    response = await run(
        {
            "tenant_id": "tenant-demo",
            "claims": [
                {
                    "claim_id": "claim-fact-1",
                    "target_type": "fact",
                    "target_id": "FACT-1",
                }
            ],
        },
        tools,
    )

    assert not response.ok
    assert response.status == "failed"
    assert response.error is not None
    assert response.error.code == "upstream_error"
    assert response.data is None
