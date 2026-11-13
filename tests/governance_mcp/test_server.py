"""Offline tests for the ontology governance MCP tools and stdio transport."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from governance_mcp_server.bootstrap import bootstrap_ontology_governance
from governance_mcp_server.server import build_server
from governance_mcp_server.service import GovernanceService

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "domain_packs" / "industrial_fault"
ONTOLOGY = PACK / "ontology.yml"

TOOL_NAMES = {
    "get_ontology_governance_status",
    "list_ontology_versions",
    "get_ontology_version",
    "list_ontology_changes",
    "get_ontology_change",
    "create_ontology_change_draft",
}


@pytest.fixture()
def governance_root(tmp_path: Path) -> Path:
    root = tmp_path / "governance"
    bootstrap_ontology_governance(
        governance_root=root,
        ontology_path=ONTOLOGY,
    )
    return root


async def _call(server, name, arguments):
    content, structured = await server.call_tool(name, arguments)
    assert isinstance(structured, dict)
    return structured, content


async def test_every_tool_is_registered_read_only(governance_root: Path) -> None:
    server = build_server(GovernanceService(governance_root))
    tools = await server.list_tools()

    assert {tool.name for tool in tools} == TOOL_NAMES
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is (
            tool.name != "create_ontology_change_draft"
        )
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False


async def test_status_and_versions_are_visible(governance_root: Path) -> None:
    server = build_server(GovernanceService(governance_root))

    status, _ = await _call(server, "get_ontology_governance_status", {})
    versions, _ = await _call(server, "list_ontology_versions", {})
    detail, _ = await _call(
        server,
        "get_ontology_version",
        {"version": "industrial-fault-v0.1"},
    )

    assert status["ok"] is True
    assert status["data"]["initialized"] is True
    assert status["data"]["active_version"] == "industrial-fault-v0.1"
    assert status["meta"]["ontology_version"] == "industrial-fault-v0.1"
    assert versions["data"]["count"] == 1
    assert versions["data"]["versions"][0]["active"] is True
    assert detail["data"]["active"] is True
    assert detail["data"]["counts"]["classes"] > 0
    assert detail["data"]["counts"]["relations"] > 0


async def test_standard_excerpt_creates_a_visible_draft(
    governance_root: Path,
) -> None:
    server = build_server(GovernanceService(governance_root))
    source = (
        "Condition monitoring systems shall identify sensors installed on "
        "rotating equipment."
    )
    created, _ = await _call(
        server,
        "create_ontology_change_draft",
        {
            "change_id": "CHG-MCP-1",
            "proposed_version": "industrial-fault-v0.2",
            "author": "engineer",
            "rationale": "Align condition monitoring concepts.",
            "source_name": "ISO 13374 excerpt",
            "source_text": source,
            "operations": [
                {
                    "kind": "class",
                    "action": "add",
                    "code": "Sensor",
                    "after": {"parent": "Asset"},
                    "evidence": [
                        {
                            "quote": source,
                            "source_name": "ISO 13374 excerpt",
                            "page": 2,
                        }
                    ],
                },
                {
                    "kind": "relation",
                    "action": "add",
                    "code": "HAS_SENSOR",
                    "after": {
                        "source_types": ["Equipment"],
                        "target_types": ["Sensor"],
                    },
                    "evidence": [
                        {
                            "quote": source,
                            "source_name": "ISO 13374 excerpt",
                        }
                    ],
                },
                {
                    "kind": "constraint",
                    "action": "add",
                    "code": "sensor-mounted-on-rotating-equipment",
                    "after": {
                        "applies_to": ["Sensor"],
                        "rule": "Sensor is mounted on rotating Equipment",
                    },
                    "evidence": [
                        {
                            "quote": source,
                            "source_name": "ISO 13374 excerpt",
                        }
                    ],
                },
            ],
        },
    )
    listed, _ = await _call(server, "list_ontology_changes", {})
    loaded, _ = await _call(
        server,
        "get_ontology_change",
        {"change_id": "CHG-MCP-1", "include_source": True},
    )
    status, _ = await _call(server, "get_ontology_governance_status", {})

    assert created["ok"] is True
    assert created["data"]["status"] == "draft"
    assert created["data"]["applied"] is False
    assert created["data"]["operation_count"] == 3
    assert created["data"]["evidence_quote_count"] == 3
    assert created["data"]["source"]["source_name"] == "ISO 13374 excerpt"
    assert listed["data"]["changes"][0]["change_id"] == "CHG-MCP-1"
    assert loaded["data"]["source_text"] == source
    assert loaded["data"]["review_events"] == []
    assert status["data"]["active_version"] == "industrial-fault-v0.1"


async def test_invalid_draft_payload_is_invalid_argument(
    governance_root: Path,
) -> None:
    server = build_server(GovernanceService(governance_root))

    invalid, _ = await _call(
        server,
        "create_ontology_change_draft",
        {
            "change_id": "CHG-INVALID",
            "proposed_version": "industrial-fault-v0.2",
            "author": "engineer",
            "rationale": "invalid",
            "source_name": "source",
            "source_text": "source text",
            "operations": [
                {
                    "kind": "class",
                    "action": "add",
                    "code": "Sensor",
                    "after": {"parent": "Asset"},
                }
            ],
        },
    )

    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "invalid_argument"
    assert "evidence" in str(invalid["error"]["details"])


async def test_uninitialized_store_is_explicit(tmp_path: Path) -> None:
    server = build_server(GovernanceService(tmp_path / "empty"))

    status, _ = await _call(server, "get_ontology_governance_status", {})
    versions, _ = await _call(server, "list_ontology_versions", {})

    assert status["ok"] is True
    assert status["data"]["initialized"] is False
    assert status["data"]["active_version"] is None
    assert versions["ok"] is True
    assert versions["data"]["count"] == 0


async def test_invalid_pointer_is_reported_without_fallback(
    governance_root: Path,
) -> None:
    (governance_root / "active.json").write_text("{bad json", encoding="utf-8")
    server = build_server(GovernanceService(governance_root))

    status, content = await _call(server, "get_ontology_governance_status", {})

    assert status["ok"] is False
    assert status["error"]["code"] == "state_invalid"
    assert "active ontology governance pointer" in status["error"]["message"]
    assert json.loads(content[0].text)["ok"] is False


async def test_stdio_client_can_list_and_call_tools(
    governance_root: Path,
) -> None:
    env = os.environ.copy()
    env["ONTOLOGY_GOVERNANCE_ROOT"] = str(governance_root)
    env["ONTOLOGY_GOVERNANCE_LOG_LEVEL"] = "WARNING"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "governance_mcp_server"],
        cwd=str(ROOT),
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            listed = await asyncio.wait_for(session.list_tools(), timeout=30)
            assert {tool.name for tool in listed.tools} == TOOL_NAMES
            result = await asyncio.wait_for(
                session.call_tool(
                    "get_ontology_governance_status",
                    {},
                ),
                timeout=30,
            )

    structured = getattr(result, "structuredContent", None)
    payload = (
        structured
        if isinstance(structured, dict)
        else json.loads(result.content[0].text)
    )
    assert result.isError is False
    assert payload["ok"] is True
    assert payload["data"]["active_version"] == "industrial-fault-v0.1"
