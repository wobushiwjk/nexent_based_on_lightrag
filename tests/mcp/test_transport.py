"""End-to-end stdio transport test, without Nexent or a network."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.mcp.helpers import DEMO_BUNDLE, PACK_DIR, REPO_ROOT

pytestmark = pytest.mark.offline


def _structured(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return json.loads(result.content[0].text)


async def test_stdio_client_can_list_and_call_every_tool():
    env = os.environ.copy()
    env["MCP_DOMAIN_PACK"] = str(PACK_DIR)
    env["MCP_DOMAIN_BUNDLES"] = str(DEMO_BUNDLE)
    env["MCP_LOG_LEVEL"] = "WARNING"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        cwd=str(REPO_ROOT),
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            listed = await asyncio.wait_for(session.list_tools(), timeout=30)
            assert {
                "get_ontology",
                "get_equipment",
                "search_faults",
                "get_fault_tree",
                "trace_evidence",
                "validate_fault_tree",
            } == {tool.name for tool in listed.tools}

            calls = [
                ("get_ontology", {}),
                (
                    "get_equipment",
                    {"tenant_id": "tenant-demo", "equipment_ref": "P-101A"},
                ),
                (
                    "search_faults",
                    {"tenant_id": "tenant-demo", "query": "出口压力不足"},
                ),
                (
                    "get_fault_tree",
                    {"tenant_id": "tenant-demo", "top_event_uid": "FAULT-LOW-PRESSURE"},
                ),
                (
                    "trace_evidence",
                    {
                        "tenant_id": "tenant-demo",
                        "target_type": "entity",
                        "target_id": "FAULT-FILTER-BLOCKED",
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
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments),
                    timeout=30,
                )
                payload = _structured(result)
                assert result.isError is False, name
                assert payload["ok"] is True, name
                if name == "get_equipment":
                    # MCP JSON must survive the stdio pipe as UTF-8, not as
                    # the parent process's console code page.
                    equipment = payload["data"]["equipment"]
                    assert equipment["canonical_name"] == "冷却水泵"
                    assert payload["data"]["evidence"][0]["quote"].startswith(
                        "入口过滤器堵塞"
                    )


async def test_stdio_transport_reports_cross_tenant_lookup_as_envelope():
    env = os.environ.copy()
    env["MCP_DOMAIN_PACK"] = str(PACK_DIR)
    env["MCP_DOMAIN_BUNDLES"] = str(DEMO_BUNDLE)
    env["MCP_LOG_LEVEL"] = "WARNING"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        cwd=str(REPO_ROOT),
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            result = await asyncio.wait_for(
                session.call_tool(
                    "get_equipment",
                    {"tenant_id": "tenant-other", "equipment_ref": "P-101A"},
                ),
                timeout=30,
            )

    payload = _structured(result)
    assert result.isError is False
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
