"""Offline tests for the agent import bundle builder."""

from __future__ import annotations

import copy
import dataclasses

import pytest

from nexent.scripts.agent_bundle import (
    DEFAULT_CONFIG,
    SENTINEL_AGENT_ID,
    AgentBundleError,
    AgentSpec,
    KnowledgeBaseToolSpec,
    build_agent_bundle,
    build_multi_agent_bundle,
    build_tool_configs,
    import_payload,
    load_agent_spec,
    load_agent_specs,
    validate_bundle,
    validate_multi_agent_bundle,
)

MCP_TOOLS = (
    "get_ontology",
    "get_equipment",
    "search_faults",
    "get_fault_tree",
    "trace_evidence",
    "validate_fault_tree",
)

GOVERNANCE_TOOLS = (
    "get_ontology_governance_status",
    "list_ontology_versions",
    "get_ontology_version",
    "list_ontology_changes",
    "get_ontology_change",
    "create_ontology_change_draft",
    "submit_ontology_change_review",
    "review_ontology_change",
    "analyze_ontology_change_impact",
    "publish_ontology_change",
    "rollback_ontology_publication",
    "list_tenant_snapshot_history",
)

KB_PARAMS = ["top_k", "index_names", "search_mode", "rerank"]


def catalog() -> list[dict]:
    entries: list[dict] = [
        {
            "name": name,
            "source": "mcp",
            "description": f"{name} description",
            "description_zh": f"{name} 描述",
            "inputs": '{"type": "object"}',
            "labels": ["fault"],
            "usage": "industrial-fault-domain",
        }
        for name in MCP_TOOLS
    ]
    entries.extend(
        {
            "name": name,
            "source": "mcp",
            "description": f"{name} description",
            "description_zh": f"{name} 描述",
            "inputs": '{"type": "object"}',
            "labels": ["governance"],
            "usage": "ontology-governance",
        }
        for name in GOVERNANCE_TOOLS
    )
    entries.append(
        {
            "name": "knowledge_base_search",
            "source": "local",
            "description": "search the knowledge base",
            "description_zh": "检索知识库",
            "inputs": '{"type": "object"}',
            "labels": None,
            "params": [{"name": name} for name in KB_PARAMS],
        }
    )
    return entries


def spec() -> AgentSpec:
    return AgentSpec(
        name="industrial_fault_agent",
        display_name="工业设备故障诊断助手",
        description="diagnose faults with evidence",
        author="tests",
        max_steps=15,
        prompt="You are a diagnosis agent.",
        skill_names=(
            "fault-diagnosis",
            "evidence-verification",
            "ontology-change-draft",
        ),
        mcp_server_name="industrial-fault-domain",
        mcp_tools=MCP_TOOLS,
        kb_tool=KnowledgeBaseToolSpec(
            class_name="KnowledgeBaseSearchTool",
            name="knowledge_base_search",
            output_type="string",
            default_params={"top_k": 5, "search_mode": "hybrid"},
        ),
        greeting_message="hello",
        example_questions=("question",),
    )


def test_real_config_is_valid():
    loaded = load_agent_spec()

    assert loaded.name == "industrial_fault_agent"
    assert loaded.mcp_tools == MCP_TOOLS + GOVERNANCE_TOOLS
    assert loaded.additional_mcp_server_names == ("ontology-governance",)
    assert loaded.agent_id == 1001
    assert loaded.is_main_agent is True
    assert loaded.managed_agents == (1002, 1003)
    assert len(loaded.skill_names) == 3
    assert loaded.kb_tool is not None
    assert loaded.kb_tool.class_name == "KnowledgeBaseSearchTool"
    assert loaded.prompt.strip()
    assert loaded.max_steps == 18


def test_mcp_tools_get_empty_params_and_name_as_class_name():
    tools = build_tool_configs(spec(), catalog(), kb_index_name="kb-index")

    by_name = {tool["name"]: tool for tool in tools}
    for name in MCP_TOOLS:
        tool = by_name[name]
        assert tool["class_name"] == name
        assert tool["params"] == {}
        assert tool["source"] == "mcp"
        assert tool["output_type"] == "object"
        assert tool["usage"] == "industrial-fault-domain"


def test_knowledge_base_tool_gets_index_name_and_defaults():
    tools = build_tool_configs(spec(), catalog(), kb_index_name="kb-index")
    kb = next(tool for tool in tools if tool["source"] == "local")

    assert kb["class_name"] == "KnowledgeBaseSearchTool"
    assert kb["params"] == {
        "index_names": ["kb-index"],
        "top_k": 5,
        "search_mode": "hybrid",
    }


def test_knowledge_base_tool_is_omitted_without_an_index():
    tools = build_tool_configs(spec(), catalog(), kb_index_name=None)

    assert [tool["name"] for tool in tools] == list(MCP_TOOLS)


def test_unknown_knowledge_base_parameter_is_rejected():
    custom = dataclasses.replace(
        spec(),
        kb_tool=KnowledgeBaseToolSpec(
            class_name="KnowledgeBaseSearchTool",
            name="knowledge_base_search",
            output_type="string",
            default_params={"unknown_param": 1},
        ),
    )

    with pytest.raises(AgentBundleError) as info:
        build_tool_configs(custom, catalog(), kb_index_name="kb-index")

    assert "unknown_param" in str(info.value)


def test_missing_mcp_tool_is_rejected():
    reduced = [entry for entry in catalog() if entry["name"] != "trace_evidence"]

    with pytest.raises(AgentBundleError) as info:
        build_tool_configs(spec(), reduced, kb_index_name="kb-index")

    assert "trace_evidence" in str(info.value)


def test_bundle_shape_matches_import_contract():
    bundle = build_agent_bundle(
        spec(),
        catalog(),
        kb_index_name="kb-index",
        mcp_server_url="http://host.docker.internal:8765/mcp",
    )

    assert bundle["agent_id"] == SENTINEL_AGENT_ID
    info = bundle["agent_info"][str(SENTINEL_AGENT_ID)]
    assert info["name"] == "industrial_fault_agent"
    assert info["is_main_agent"] is True
    assert info["managed_agents"] == []
    assert info["skill_names"] == [
        "fault-diagnosis",
        "evidence-verification",
        "ontology-change-draft",
    ]
    assert [tool["params"] for tool in info["tools"] if tool["source"] == "mcp"] == [
        {} for _ in MCP_TOOLS
    ]
    assert bundle["mcp_info"] == [
        {
            "mcp_server_name": "industrial-fault-domain",
            "mcp_url": "http://host.docker.internal:8765/mcp",
        }
    ]

    validate_bundle(bundle, spec())


def test_bundle_carries_multiple_mcp_servers():
    configured = dataclasses.replace(
        spec(),
        mcp_tools=MCP_TOOLS + GOVERNANCE_TOOLS,
        additional_mcp_server_names=("ontology-governance",),
    )
    bundle = build_agent_bundle(
        configured,
        catalog(),
        kb_index_name="kb-index",
        mcp_server_url="http://host.docker.internal:8765/mcp",
        additional_mcp_server_urls={
            "ontology-governance": "http://host.docker.internal:8766/mcp"
        },
    )

    assert bundle["mcp_info"] == [
        {
            "mcp_server_name": "industrial-fault-domain",
            "mcp_url": "http://host.docker.internal:8765/mcp",
        },
        {
            "mcp_server_name": "ontology-governance",
            "mcp_url": "http://host.docker.internal:8766/mcp",
        },
    ]
    validate_bundle(bundle, configured)


def test_additional_mcp_server_requires_url():
    configured = dataclasses.replace(
        spec(),
        additional_mcp_server_names=("ontology-governance",),
    )

    with pytest.raises(AgentBundleError) as info:
        build_agent_bundle(
            configured,
            catalog(),
            kb_index_name="kb-index",
            mcp_server_url="http://mcp",
        )

    assert "no URL" in str(info.value)


def test_multi_agent_bundle_contains_main_and_two_managed_children():
    main = dataclasses.replace(
        spec(),
        agent_id=1001,
        managed_agents=(1002, 1003),
    )
    evidence = dataclasses.replace(
        spec(),
        agent_id=1002,
        name="evidence_review_agent",
        display_name="Evidence reviewer",
        is_main_agent=False,
        managed_agents=(),
        kb_tool=None,
        skill_names=("evidence-verification",),
        mcp_tools=(
            "get_equipment",
            "get_fault_tree",
            "trace_evidence",
            "validate_fault_tree",
        ),
    )
    expert = dataclasses.replace(
        spec(),
        agent_id=1003,
        name="ontology_expert_agent",
        display_name="Ontology expert",
        is_main_agent=False,
        managed_agents=(),
        kb_tool=None,
        skill_names=("ontology-change-draft",),
        mcp_tools=(
            "get_ontology",
            "trace_evidence",
            "validate_fault_tree",
        ),
    )
    specs = (main, evidence, expert)

    bundle = build_multi_agent_bundle(
        specs,
        catalog(),
        kb_index_name="kb-index",
        mcp_server_url="http://domain",
    )

    assert set(bundle["agent_info"]) == {"1001", "1002", "1003"}
    assert bundle["agent_id"] == 1001
    assert bundle["agent_info"]["1001"]["managed_agents"] == [1002, 1003]
    assert bundle["agent_info"]["1002"]["is_main_agent"] is False
    assert bundle["agent_info"]["1003"]["managed_agents"] == []
    validate_multi_agent_bundle(bundle, specs)


def test_real_multi_agent_config_has_expected_graph():
    specs = load_agent_specs(DEFAULT_CONFIG)
    assert [spec.agent_id for spec in specs] == [1001, 1002, 1003]
    assert [spec.is_main_agent for spec in specs] == [True, False, False]
    assert [spec.is_a2a for spec in specs] == [False, False, False]
    assert [spec.enable_protocol_repair_retry for spec in specs] == [
        True,
        True,
        True,
    ]
    assert specs[0].managed_agents == (1002, 1003)
    assert specs[1].managed_agents == ()
    assert specs[2].managed_agents == ()

    bundle = build_multi_agent_bundle(
        specs,
        catalog(),
        kb_index_name="kb-index",
        mcp_server_url="http://domain",
        additional_mcp_server_urls={"ontology-governance": "http://governance"},
    )
    assert bundle["agent_info"]["1001"]["managed_agents"] == [1002, 1003]
    assert bundle["agent_info"]["1001"]["is_a2a"] is False
    assert bundle["agent_info"]["1001"]["enable_protocol_repair_retry"] is True
    assert bundle["agent_info"]["1002"]["is_main_agent"] is False
    assert bundle["agent_info"]["1003"]["is_main_agent"] is False


def test_validate_bundle_rejects_mcp_params():
    bundle = build_agent_bundle(
        spec(), catalog(), kb_index_name="kb-index", mcp_server_url="http://mcp"
    )
    info = bundle["agent_info"][str(SENTINEL_AGENT_ID)]
    info["tools"][0]["params"] = {"wrong": 1}

    with pytest.raises(AgentBundleError) as error:
        validate_bundle(bundle, spec())

    assert "empty params" in str(error.value)


def test_validate_bundle_rejects_duplicate_tools():
    bundle = build_agent_bundle(
        spec(), catalog(), kb_index_name="kb-index", mcp_server_url="http://mcp"
    )
    info = bundle["agent_info"][str(SENTINEL_AGENT_ID)]
    info["tools"].append(copy.deepcopy(info["tools"][0]))

    with pytest.raises(AgentBundleError) as error:
        validate_bundle(bundle, spec())

    assert "duplicate tool" in str(error.value)


def test_validate_bundle_rejects_missing_mcp_server():
    configured = dataclasses.replace(
        spec(),
        additional_mcp_server_names=("ontology-governance",),
    )
    bundle = build_agent_bundle(
        configured,
        catalog(),
        kb_index_name="kb-index",
        mcp_server_url="http://mcp",
        additional_mcp_server_urls={"ontology-governance": "http://governance"},
    )
    bundle["mcp_info"] = bundle["mcp_info"][:1]

    with pytest.raises(AgentBundleError) as error:
        validate_bundle(bundle, configured)

    assert "MCP servers" in str(error.value)


def test_validate_bundle_rejects_skill_mismatch():
    bundle = build_agent_bundle(
        spec(), catalog(), kb_index_name="kb-index", mcp_server_url="http://mcp"
    )
    info = bundle["agent_info"][str(SENTINEL_AGENT_ID)]
    info["skill_names"] = ["fault-diagnosis"]

    with pytest.raises(AgentBundleError) as error:
        validate_bundle(bundle, spec())

    assert "skill_names" in str(error.value)


def test_import_payload_carries_skills_and_resolutions():
    bundle = build_agent_bundle(
        spec(), catalog(), kb_index_name="kb-index", mcp_server_url="http://mcp"
    )
    skills = [{"skill_name": "fault-diagnosis", "skill_zip_base64": "abc"}]

    payload = import_payload(
        bundle,
        skills=skills,
        skill_resolutions=[{"skill_name": "fault-diagnosis", "action": "use_existing"}],
        force_import=True,
    )

    assert payload["agent_info"] == bundle
    assert payload["skills"] == skills
    assert payload["skill_resolutions"][0]["action"] == "use_existing"
    assert payload["force_import"] is True


def test_missing_prompt_file_is_reported(tmp_path):
    config = tmp_path / "agent.json"
    config.write_text(
        '{"agent": {"name": "a", "display_name": "a", "description": "a", '
        '"max_steps": 1, "prompt_file": "does/not/exist.md", '
        '"mcp_server_name": "s", "mcp_tools": ["t"]}}',
        encoding="utf-8",
    )

    with pytest.raises(AgentBundleError) as info:
        load_agent_spec(config)

    assert "cannot read prompt" in str(info.value)
