"""Build the Nexent agent import bundle from the repository's declarative spec.

``nexent/config/agent.json`` names the agent and the tools it may use;
``nexent/agent/system_prompt.md`` holds the prompt. This module turns those
plus a *live* ``GET /tool/list`` catalog into the
``ExportAndImportDataFormat`` body that ``POST /agent/import`` accepts.

Why the catalog is an input rather than a constant: Nexent validates every
imported tool against its own tool table (``validate_import_tool_params``),
and that table is populated by a scan of the running MCP server. A bundle
built from a stale copy of the tool list would fail import with "Cannot find
tool ...". Building from the live catalog also copies the MCP tool's
``inputs`` string and the knowledge-base tool's parameter names exactly as
Nexent recorded them.

Two rules that are easy to get wrong and are enforced here:

* MCP tools must carry empty ``params``. Nexent's catalog lists no init
  parameters for them, and any name we add is rejected at import time.
* MCP tools use their tool name as ``class_name``; Nexent derives both from
  ``_sanitize_function_name`` when it discovers them. Local tools such as
  ``knowledge_base_search`` need the SDK class name, which is declared in
  the config.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "nexent" / "config" / "agents.json"

# Nexent resolves the imported agent id through a mapping, so any value works;
# a constant keeps the bundle byte-comparable between runs.
SENTINEL_AGENT_ID = 1001


class AgentBundleError(RuntimeError):
    """The agent bundle cannot be built from the configuration and catalog."""


@dataclass(frozen=True)
class KnowledgeBaseToolSpec:
    """The built-in Nexent knowledge-base retrieval tool."""

    class_name: str
    name: str
    output_type: str
    default_params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSpec:
    """Everything the bundle needs that does not come from the live catalog."""

    name: str
    display_name: str
    description: str
    author: str
    max_steps: int
    prompt: str
    skill_names: tuple[str, ...]
    mcp_server_name: str
    mcp_tools: tuple[str, ...]
    kb_tool: KnowledgeBaseToolSpec | None
    greeting_message: str
    example_questions: tuple[str, ...]
    additional_mcp_server_names: tuple[str, ...] = ()
    agent_id: int = SENTINEL_AGENT_ID
    is_main_agent: bool = True
    is_a2a: bool = False
    enable_protocol_repair_retry: bool = True
    managed_agents: tuple[int, ...] = ()
    provide_run_summary: bool = True
    enabled: bool = True
    model_id: int | None = None
    model_name: str | None = None
    business_logic_model_id: int | None = None
    business_logic_model_name: str | None = None


def load_agent_spec(config_path: Path = DEFAULT_CONFIG) -> AgentSpec:
    """Load ``nexent/config/agent.json`` and its prompt file.

    ``prompt_file`` is resolved against the repository root, so the config can
    be read from anywhere.
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentBundleError(f"cannot read {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentBundleError(f"{config_path} must contain a JSON object")

    agent = data.get("agent")
    if agent is None:
        raw_agents = data.get("agents")
        if isinstance(raw_agents, list) and raw_agents:
            agent = raw_agents[0]
    if not isinstance(agent, dict):
        raise AgentBundleError(f"{config_path} has no 'agent' or main 'agents'")

    return _agent_spec_from_data(data, agent, config_path)


def load_agent_specs(
    config_path: Path = DEFAULT_CONFIG,
) -> tuple[AgentSpec, ...]:
    """Load either the legacy single-Agent config or an ``agents`` list."""

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentBundleError(f"cannot read {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentBundleError(f"{config_path} must contain a JSON object")
    raw_agents = data.get("agents")
    if raw_agents is None:
        agent = data.get("agent")
        if not isinstance(agent, dict):
            raise AgentBundleError(f"{config_path} has no 'agent' or 'agents'")
        return (_agent_spec_from_data(data, agent, config_path),)
    if not isinstance(raw_agents, list) or not raw_agents:
        raise AgentBundleError(f"{config_path}.agents must be a non-empty list")
    if any(not isinstance(item, dict) for item in raw_agents):
        raise AgentBundleError(f"{config_path}.agents entries must be objects")
    return tuple(
        _agent_spec_from_data(data, agent, config_path) for agent in raw_agents
    )


def _agent_spec_from_data(
    data: Mapping[str, Any],
    agent: Mapping[str, Any],
    config_path: Path,
) -> AgentSpec:
    prompt_file = agent.get("prompt_file")
    if not isinstance(prompt_file, str) or not prompt_file:
        raise AgentBundleError("agent.prompt_file is required")
    prompt_path = (REPO_ROOT / prompt_file).resolve()
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AgentBundleError(f"cannot read prompt {prompt_path}: {exc}") from exc
    if not prompt:
        raise AgentBundleError(f"prompt {prompt_path} is empty")

    mcp_tools = _string_tuple(agent.get("mcp_tools"), "agent.mcp_tools")
    if not mcp_tools:
        raise AgentBundleError("agent.mcp_tools must list at least one MCP tool")

    raw_kb = data.get("knowledge_base_tool")
    kb_tool: KnowledgeBaseToolSpec | None = None
    if raw_kb is not None:
        if not isinstance(raw_kb, dict):
            raise AgentBundleError("knowledge_base_tool must be an object")
        params = raw_kb.get("params") or {}
        if not isinstance(params, dict):
            raise AgentBundleError("knowledge_base_tool.params must be an object")
        kb_tool = KnowledgeBaseToolSpec(
            class_name=_required_str(raw_kb, "class_name", "knowledge_base_tool"),
            name=_required_str(raw_kb, "name", "knowledge_base_tool"),
            output_type=str(raw_kb.get("output_type") or "string"),
            default_params=dict(params),
        )

    use_knowledge_base = bool(agent.get("use_knowledge_base", True))
    return AgentSpec(
        name=_required_str(agent, "name", "agent"),
        display_name=_required_str(agent, "display_name", "agent"),
        description=_required_str(agent, "description", "agent"),
        author=str(agent.get("author") or "lightrag"),
        max_steps=int(agent.get("max_steps") or 15),
        prompt=prompt,
        skill_names=_string_tuple(agent.get("skills"), "agent.skills"),
        mcp_server_name=_required_str(agent, "mcp_server_name", "agent"),
        mcp_tools=mcp_tools,
        kb_tool=kb_tool if use_knowledge_base else None,
        greeting_message=str(agent.get("greeting_message") or ""),
        example_questions=_string_tuple(
            agent.get("example_questions"), "agent.example_questions"
        ),
        additional_mcp_server_names=_string_tuple(
            agent.get("additional_mcp_servers"),
            "agent.additional_mcp_servers",
        ),
        agent_id=int(agent.get("agent_id") or SENTINEL_AGENT_ID),
        is_main_agent=bool(agent.get("is_main_agent", True)),
        is_a2a=bool(agent.get("is_a2a", False)),
        enable_protocol_repair_retry=bool(
            agent.get("enable_protocol_repair_retry", True)
        ),
        managed_agents=_int_tuple(
            agent.get("managed_agents"),
            "agent.managed_agents",
        ),
        provide_run_summary=bool(agent.get("provide_run_summary", True)),
        enabled=bool(agent.get("enabled", True)),
        model_id=agent.get("model_id"),
        model_name=agent.get("model_name"),
        business_logic_model_id=agent.get("business_logic_model_id"),
        business_logic_model_name=agent.get("business_logic_model_name"),
    )


def _required_str(mapping: Mapping[str, Any], key: str, owner: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentBundleError(f"{owner}.{key} must be a non-empty string")
    return value.strip()


def _string_tuple(value: Any, owner: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AgentBundleError(f"{owner} must be a list of strings")
    return tuple(item for item in value if item)


def _int_tuple(value: Any, owner: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise AgentBundleError(f"{owner} must be a list of integers")
    return tuple(value)


def _catalog_index(
    catalog: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    for entry in catalog:
        name = entry.get("name")
        source = entry.get("source")
        if isinstance(name, str) and isinstance(source, str):
            index[(name, source)] = entry
    return index


def build_tool_configs(
    spec: AgentSpec,
    catalog: Sequence[Mapping[str, Any]],
    *,
    kb_index_name: str | None,
) -> list[dict[str, Any]]:
    """Build the ``tools`` array for the agent bundle.

    ``kb_index_name`` is the internal index name returned by Nexent's knowledge
    base creation. Passing ``None`` omits the knowledge-base tool entirely,
    which is what ``--skip-kb`` imports.

    Raises:
        AgentBundleError: a declared tool is missing from the live catalog, or
            the knowledge-base tool declares a parameter Nexent will reject.
    """
    by_key = _catalog_index(catalog)
    tools: list[dict[str, Any]] = []

    missing = [
        tool_name for tool_name in spec.mcp_tools if (tool_name, "mcp") not in by_key
    ]
    if missing:
        raise AgentBundleError(
            "MCP tools are missing from the Nexent catalog: "
            f"{missing}. Are all declared MCP servers registered, enabled and "
            "scanned?"
        )

    for tool_name in spec.mcp_tools:
        entry = by_key[(tool_name, "mcp")]
        tools.append(
            {
                "class_name": tool_name,
                "name": tool_name,
                "description": entry.get("description") or "",
                "description_zh": entry.get("description_zh"),
                "inputs": entry.get("inputs") or "{}",
                "output_type": "object",
                # MCP tools have no user-settable init parameters. Nexent
                # rejects any name it does not list in its catalog entry.
                "params": {},
                "source": "mcp",
                "usage": entry.get("usage") or spec.mcp_server_name,
                "metadata": {},
                "labels": entry.get("labels"),
            }
        )

    if spec.kb_tool is not None and kb_index_name is not None:
        entry = by_key.get((spec.kb_tool.name, "local"))
        if entry is None:
            raise AgentBundleError(
                f"local tool {spec.kb_tool.name!r} is missing from the Nexent "
                "catalog; run tool scan first"
            )
        params = {
            "index_names": [kb_index_name],
            **dict(spec.kb_tool.default_params),
        }
        allowed = {
            param.get("name")
            for param in entry.get("params") or []
            if isinstance(param, dict)
        }
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise AgentBundleError(
                f"parameters {unknown} are not accepted by {spec.kb_tool.name!r}; "
                f"allowed: {sorted(name for name in allowed if name)}"
            )
        tools.append(
            {
                "class_name": spec.kb_tool.class_name,
                "name": spec.kb_tool.name,
                "description": entry.get("description") or "",
                "description_zh": entry.get("description_zh"),
                "inputs": entry.get("inputs") or "{}",
                "output_type": spec.kb_tool.output_type,
                "params": params,
                "source": "local",
                "usage": None,
                "metadata": {},
                "labels": entry.get("labels"),
            }
        )

    return tools


def build_agent_bundle(
    spec: AgentSpec,
    catalog: Sequence[Mapping[str, Any]],
    *,
    kb_index_name: str | None,
    mcp_server_url: str,
    additional_mcp_server_urls: Mapping[str, str] | None = None,
    agent_id: int = SENTINEL_AGENT_ID,
) -> dict[str, Any]:
    """Build the ``ExportAndImportDataFormat`` body for ``POST /agent/import``."""
    info = build_agent_info(
        spec,
        catalog,
        kb_index_name=kb_index_name,
        agent_id=agent_id,
    )
    mcp_info = _mcp_info(
        spec,
        mcp_server_url=mcp_server_url,
        additional_mcp_server_urls=additional_mcp_server_urls,
    )
    return {
        "agent_id": agent_id,
        "agent_info": {str(agent_id): info},
        "mcp_info": mcp_info,
    }


def build_agent_info(
    spec: AgentSpec,
    catalog: Sequence[Mapping[str, Any]],
    *,
    kb_index_name: str | None,
    agent_id: int | None = None,
) -> dict[str, Any]:
    """Build one ``agent_info`` entry for a single or managed Agent."""

    tools = build_tool_configs(spec, catalog, kb_index_name=kb_index_name)
    effective_id = spec.agent_id if agent_id is None else agent_id
    return {
        "agent_id": effective_id,
        "tenant_id": None,
        "name": spec.name,
        "display_name": spec.display_name,
        "description": spec.description,
        "author": spec.author,
        "max_steps": spec.max_steps,
        "requested_output_tokens": None,
        "is_main_agent": spec.is_main_agent,
        "provide_run_summary": spec.provide_run_summary,
        "allow_chat_metadata": False,
        "is_a2a": spec.is_a2a,
        "enable_protocol_repair_retry": spec.enable_protocol_repair_retry,
        "verification_config": None,
        "context_policy": None,
        "prompt": spec.prompt,
        "enabled": spec.enabled,
        "tools": tools,
        "managed_agents": list(spec.managed_agents),
        "model_ids": [spec.model_id] if spec.model_id is not None else [],
        "model_names": [spec.model_name] if spec.model_name else [],
        "business_logic_model_id": spec.business_logic_model_id,
        "business_logic_model_name": spec.business_logic_model_name,
        "skill_names": list(spec.skill_names),
        "model_params_override": None,
        "greeting_message": spec.greeting_message,
        "example_questions": list(spec.example_questions),
    }


def build_multi_agent_bundle(
    specs: Sequence[AgentSpec],
    catalog: Sequence[Mapping[str, Any]],
    *,
    kb_index_name: str | None,
    mcp_server_url: str,
    additional_mcp_server_urls: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build one Nexent bundle containing a main Agent and managed children."""

    if not specs:
        raise AgentBundleError("at least one Agent specification is required")
    validate_agent_spec_graph(specs)
    primary = specs[0]
    infos = {
        str(spec.agent_id): build_agent_info(
            spec,
            catalog,
            kb_index_name=kb_index_name,
        )
        for spec in specs
    }
    bundle = {
        "agent_id": primary.agent_id,
        "agent_info": infos,
        "mcp_info": _mcp_info(
            primary,
            mcp_server_url=mcp_server_url,
            additional_mcp_server_urls=additional_mcp_server_urls,
        ),
    }
    validate_multi_agent_bundle(bundle, specs)
    return bundle


def _mcp_info(
    spec: AgentSpec,
    *,
    mcp_server_url: str,
    additional_mcp_server_urls: Mapping[str, str] | None,
) -> list[dict[str, str]]:
    additional_urls = dict(additional_mcp_server_urls or {})
    missing_urls = sorted(set(spec.additional_mcp_server_names) - set(additional_urls))
    if missing_urls:
        raise AgentBundleError(
            "additional MCP servers have no URL: " + ", ".join(missing_urls)
        )
    unexpected_urls = sorted(
        set(additional_urls) - set(spec.additional_mcp_server_names)
    )
    if unexpected_urls:
        raise AgentBundleError(
            "URLs were supplied for undeclared MCP servers: "
            + ", ".join(unexpected_urls)
        )
    return [
        {
            "mcp_server_name": spec.mcp_server_name,
            "mcp_url": mcp_server_url,
        },
        *[
            {
                "mcp_server_name": name,
                "mcp_url": additional_urls[name],
            }
            for name in spec.additional_mcp_server_names
        ],
    ]


def import_payload(
    bundle: Mapping[str, Any],
    *,
    skills: Sequence[Mapping[str, str]] = (),
    skill_resolutions: Sequence[Mapping[str, Any]] = (),
    force_import: bool = False,
) -> dict[str, Any]:
    """Wrap a bundle into the ``POST /agent/import`` request body.

    ``skills`` entries are ``{"skill_name": ..., "skill_zip_base64": ...}``.
    Every bundled skill gets a ``use_existing`` resolution when it is already
    present; Nexent ignores that resolution for names that do not exist, so
    the same request is safe on a fresh and an existing installation.
    """
    payload: dict[str, Any] = {
        "agent_info": dict(bundle),
        "force_import": force_import,
    }
    if skills:
        payload["skills"] = [dict(entry) for entry in skills]
    if skill_resolutions:
        payload["skill_resolutions"] = [dict(entry) for entry in skill_resolutions]
    return payload


def validate_bundle(bundle: Mapping[str, Any], spec: AgentSpec) -> None:
    """Reject a bundle that would fail Nexent's import validation.

    Kept separate from ``build_agent_bundle`` so tests can prove the checks
    fire on hand-written bundles too.
    """
    agent_info = bundle.get("agent_info")
    if not isinstance(agent_info, dict) or not agent_info:
        raise AgentBundleError("bundle.agent_info must be a non-empty object")
    for key, info in agent_info.items():
        if not isinstance(info, dict):
            raise AgentBundleError(f"bundle.agent_info[{key!r}] must be an object")
        name = info.get("name")
        if name != spec.name:
            raise AgentBundleError(
                f"bundle agent name {name!r} does not match spec {spec.name!r}"
            )
        if int(info.get("max_steps") or 0) <= 0:
            raise AgentBundleError("bundle max_steps must be positive")
        tool_names = set()
        for tool in info.get("tools") or []:
            if not isinstance(tool, dict):
                raise AgentBundleError("every tool entry must be an object")
            tool_name = tool.get("name")
            if tool_name in tool_names:
                raise AgentBundleError(f"duplicate tool in bundle: {tool_name!r}")
            tool_names.add(tool_name)
            if tool.get("source") == "mcp" and tool.get("params"):
                raise AgentBundleError(f"MCP tool {tool_name!r} must have empty params")
            if not tool.get("class_name"):
                raise AgentBundleError(f"tool {tool_name!r} has no class_name")
        declared = set(info.get("skill_names") or [])
        expected = set(spec.skill_names)
        if declared != expected:
            raise AgentBundleError(
                f"bundle skill_names {sorted(declared)} != spec {sorted(expected)}"
            )
    mcp_info = bundle.get("mcp_info")
    if not isinstance(mcp_info, list):
        raise AgentBundleError("bundle.mcp_info must be a list")
    names = [item.get("mcp_server_name") for item in mcp_info if isinstance(item, dict)]
    if len(names) != len(mcp_info) or any(
        not isinstance(name, str) or not name for name in names
    ):
        raise AgentBundleError("every bundle.mcp_info entry needs mcp_server_name")
    if len(set(names)) != len(names):
        raise AgentBundleError("bundle.mcp_info contains duplicate servers")
    expected_servers = {
        spec.mcp_server_name,
        *spec.additional_mcp_server_names,
    }
    if set(names) != expected_servers:
        raise AgentBundleError(
            f"bundle MCP servers {sorted(names)} != spec {sorted(expected_servers)}"
        )


def validate_agent_spec_graph(specs: Sequence[AgentSpec]) -> None:
    """Validate a main Agent and its managed child specifications."""

    ids = [spec.agent_id for spec in specs]
    names = [spec.name for spec in specs]
    if len(set(ids)) != len(ids):
        raise AgentBundleError("Agent ids must be unique")
    if len(set(names)) != len(names):
        raise AgentBundleError("Agent names must be unique")
    mains = [spec for spec in specs if spec.is_main_agent]
    if len(mains) != 1:
        raise AgentBundleError("exactly one Agent must be the main Agent")
    main = mains[0]
    if main is not specs[0]:
        raise AgentBundleError("the main Agent must be the bundle root")
    expected_children = {spec.agent_id for spec in specs if spec is not main}
    if set(main.managed_agents) != expected_children:
        raise AgentBundleError(
            "main Agent managed_agents must list every child Agent exactly once"
        )
    for child in specs:
        if child is main:
            continue
        if child.managed_agents:
            raise AgentBundleError(
                f"child Agent {child.name!r} cannot manage other Agents"
            )
        if (
            child.mcp_server_name != main.mcp_server_name
            or child.additional_mcp_server_names != main.additional_mcp_server_names
        ):
            raise AgentBundleError(
                f"child Agent {child.name!r} must use the same MCP servers"
            )


def validate_multi_agent_bundle(
    bundle: Mapping[str, Any],
    specs: Sequence[AgentSpec],
) -> None:
    """Validate every Agent entry and the persisted parent-child relation."""

    validate_agent_spec_graph(specs)
    infos = bundle.get("agent_info")
    if not isinstance(infos, dict):
        raise AgentBundleError("bundle.agent_info must be an object")
    expected_ids = {str(spec.agent_id) for spec in specs}
    if set(infos) != expected_ids:
        raise AgentBundleError(
            f"bundle Agent ids {sorted(infos)} != {sorted(expected_ids)}"
        )
    for spec in specs:
        key = str(spec.agent_id)
        info = infos[key]
        if not isinstance(info, dict):
            raise AgentBundleError(f"agent_info[{key!r}] must be an object")
        if info.get("agent_id") != spec.agent_id:
            raise AgentBundleError(
                f"agent_info[{key!r}].agent_id does not match its key"
            )
        if info.get("is_main_agent") is not spec.is_main_agent:
            raise AgentBundleError(
                f"agent_info[{key!r}].is_main_agent does not match the spec"
            )
        if info.get("managed_agents") != list(spec.managed_agents):
            raise AgentBundleError(
                f"agent_info[{key!r}].managed_agents does not match the spec"
            )
        validate_bundle(
            {"agent_info": {key: info}, "mcp_info": bundle.get("mcp_info")},
            spec,
        )
    if bundle.get("agent_id") != specs[0].agent_id:
        raise AgentBundleError("bundle.agent_id must identify the main Agent")
