"""FastMCP registration and the common response envelope."""

from __future__ import annotations

import json

import pytest

from mcp_server.server import build_server

pytestmark = pytest.mark.offline

TOOL_NAMES = {
    "get_ontology",
    "get_equipment",
    "search_faults",
    "get_fault_tree",
    "trace_evidence",
    "validate_fault_tree",
}


async def _call(server, name, arguments):
    result = await server.call_tool(name, arguments)
    assert isinstance(result, tuple)
    content, structured = result
    assert isinstance(structured, dict)
    return structured, content


async def test_every_tool_is_registered_read_only(service):
    server = build_server(service)
    tools = await server.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == TOOL_NAMES
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False

    assert by_name["get_ontology"].inputSchema["properties"] == {}
    assert by_name["get_equipment"].inputSchema["required"] == [
        "tenant_id",
        "equipment_ref",
    ]
    limit_schema = by_name["search_faults"].inputSchema["properties"]["limit"]
    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 50


async def test_every_tool_returns_a_success_envelope(service):
    server = build_server(service)
    calls = [
        ("get_ontology", {}),
        ("get_equipment", {"tenant_id": "tenant-demo", "equipment_ref": "P-101A"}),
        ("search_faults", {"tenant_id": "tenant-demo", "query": "汽蚀"}),
        (
            "get_fault_tree",
            {
                "tenant_id": "tenant-demo",
                "draft_id": "TREE-P101A-LOW-PRESSURE-v1",
            },
        ),
        (
            "trace_evidence",
            {
                "tenant_id": "tenant-demo",
                "target_type": "rule",
                "target_id": "RULE-CAVITATION-AND",
            },
        ),
        (
            "validate_fault_tree",
            {
                "tenant_id": "tenant-demo",
                "draft_id": "TREE-P101A-LOW-PRESSURE-v1",
            },
        ),
    ]

    for name, arguments in calls:
        structured, content = await _call(server, name, arguments)
        assert structured["ok"] is True, name
        assert structured["error"] is None, name
        assert structured["meta"]["read_only"] is True, name
        assert structured["meta"]["ontology_version"] == "industrial-fault-v0.1"
        assert content
        assert json.loads(content[0].text)["ok"] is True


async def test_business_errors_use_the_envelope(service):
    server = build_server(service)
    structured, content = await _call(
        server,
        "get_equipment",
        {"tenant_id": "tenant-other", "equipment_ref": "P-101A"},
    )

    assert structured["ok"] is False
    assert structured["data"] is None
    assert structured["error"]["code"] == "not_found"
    assert structured["error"]["details"] == {"tenant_id": "tenant-other"}
    assert structured["meta"]["tenant_id"] == "tenant-other"
    assert json.loads(content[0].text)["error"]["code"] == "not_found"


async def test_internal_errors_do_not_leak_details(service, monkeypatch):
    def boom():
        raise RuntimeError("secret implementation detail")

    monkeypatch.setattr(service, "get_ontology", boom)
    server = build_server(service)
    structured, content = await _call(server, "get_ontology", {})

    assert structured["ok"] is False
    assert structured["error"]["code"] == "internal_error"
    assert "secret implementation detail" not in json.dumps(structured)
    assert "secret implementation detail" not in content[0].text
