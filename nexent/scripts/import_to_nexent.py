"""Register the repository's domain services inside a running Nexent instance.

This is the W5 import flow. It is idempotent: every step converges the Nexent
installation to the configuration under ``nexent/``, re-running changes nothing
that is already correct, and the one destructive step (replacing the agent)
is explicit and reversible (see ``nexent/ROLLBACK.md``).

Step order, and why it cannot be reordered:

1. Sign in and read the tenant id. Nexent injects the session tenant into every
   MCP tool call, so the seed data must be imported under that same id.
2. Import the seed domain pack (skipped when the tenant's ``active.json``
   exists). The MCP server refuses to start on a dangling active pointer, so
   this precedes it.
3. Start-side prerequisites for MCP: the server must already be running; this
   script only registers its URL and checks reachability.
4. Add or update the MCP service, scan the tool catalog, and require every
   tool the agent will use to be present.
5. Package the Skills as deterministic ZIPs. ``--refresh-skills`` deletes the
   existing rows first, because Nexent's ``use_existing`` resolution reuses the
   stored skill *content* and would otherwise mask edits to ``SKILL.md``.
6. Create or reuse the knowledge base and ingest the guide documents.
7. Import the agent bundle (delete-and-recreate by name unless
   ``--no-replace-agent``), which also creates the skill instances.
8. Verify the agent is published and the MCP tools are still catalogued.

Run from the repository root::

    .venv/Scripts/python.exe -m nexent.scripts.import_to_nexent --dry-run
    .venv/Scripts/python.exe -m nexent.scripts.import_to_nexent
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from nexent.scripts import build_skills
from nexent.scripts.agent_bundle import (
    AgentBundleError,
    AgentSpec,
    build_agent_bundle,
    build_multi_agent_bundle,
    import_payload,
    load_agent_specs,
    validate_bundle,
    validate_multi_agent_bundle,
)
from nexent.scripts.nexent_client import (
    DEFAULT_CONFIG_URL,
    NexentClient,
    NexentConfigError,
    NexentError,
    redact,
)
from governance_mcp_server.bootstrap import bootstrap_ontology_governance

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AGENT_CONFIG = REPO_ROOT / "nexent" / "config" / "agents.json"
DEFAULT_MCP_CONFIG = REPO_ROOT / "nexent" / "config" / "mcp.json"
DEFAULT_GOVERNANCE_MCP_CONFIG = REPO_ROOT / "nexent" / "config" / "governance_mcp.json"
DEFAULT_KB_CONFIG = REPO_ROOT / "nexent" / "config" / "knowledge_base.json"
DEFAULT_SKILLS_CONFIG = REPO_ROOT / "nexent" / "config" / "skills.json"
DEFAULT_SKILLS_OUT = REPO_ROOT / "nexent" / "build" / "skills"
DEFAULT_REPORT = REPO_ROOT / "nexent" / "build" / "import-report.json"
DEFAULT_SEED_STORAGE = REPO_ROOT / "domain_packs" / "industrial_fault" / "storage"
DEFAULT_GOVERNANCE_ROOT = DEFAULT_SEED_STORAGE / "governance"
DEFAULT_GOVERNANCE_ONTOLOGY = (
    REPO_ROOT / "domain_packs" / "industrial_fault" / "ontology.yml"
)
SEED_SCRIPT = REPO_ROOT / "scripts" / "import_seed_domain.py"
GOVERNANCE_TOOLS = frozenset(
    {
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
    }
)

DEFAULT_EMAIL = "suadmin@nexent.com"
DEFAULT_PASSWORD = "Nexent@123"
PENDING_STATUSES = {
    "WAIT_FOR_PROCESSING",
    "PROCESSING",
    "FORWARDING",
}
FAILED_STATUSES = {"PROCESS_FAILED", "FORWARD_FAILED", "FAILED"}


class ImportStepError(RuntimeError):
    """One import step cannot proceed; the report records it and stops."""


def _as_specs(
    specs: AgentSpec | Sequence[AgentSpec],
) -> tuple[AgentSpec, ...]:
    if isinstance(specs, AgentSpec):
        return (specs,)
    values = tuple(specs)
    if not values:
        raise ImportStepError("at least one Agent specification is required")
    return values


@dataclass(frozen=True)
class ImportOptions:
    """Everything the import flow can vary between runs."""

    config_url: str = DEFAULT_CONFIG_URL
    email: str = DEFAULT_EMAIL
    password: str = DEFAULT_PASSWORD
    mcp_url: str | None = None
    governance_mcp_url: str | None = None
    governance_root: Path = DEFAULT_GOVERNANCE_ROOT
    governance_ontology: Path = DEFAULT_GOVERNANCE_ONTOLOGY
    skip_governance_mcp: bool = False
    seed: str = "auto"
    seed_storage: Path = DEFAULT_SEED_STORAGE
    seed_timeout: int = 600
    refresh_skills: bool = False
    skip_kb: bool = False
    replace_agent: bool = True
    dry_run: bool = False
    wait_kb_seconds: int = 180
    poll_interval: float = 3.0
    report_path: Path = DEFAULT_REPORT
    python: str = sys.executable


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportStepError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ImportStepError(f"{path} must contain a JSON object")
    return data


def _repo_path(relative: str) -> Path:
    path = (REPO_ROOT / relative).resolve()
    if path != REPO_ROOT and REPO_ROOT not in path.parents:
        raise ImportStepError(f"configured path escapes the repository: {relative}")
    return path


def _tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def step_seed(options: ImportOptions, tenant_id: str) -> dict[str, Any]:
    """Import the seed domain pack for ``tenant_id`` unless it is present."""
    active = options.seed_storage / "tenants" / tenant_id / "active.json"
    command = [
        options.python,
        str(SEED_SCRIPT),
        "--tenant",
        tenant_id,
        "--storage",
        str(options.seed_storage),
        "--verify",
    ]
    if options.seed == "never":
        return {"action": "skipped", "reason": "--seed never"}
    if options.seed == "auto" and active.is_file():
        return {"action": "skipped", "reason": f"{active} already exists"}
    if options.dry_run:
        return {"action": "would_import", "command": command}

    try:
        process = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=options.seed_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImportStepError(
            f"seed import timed out after {options.seed_timeout}s"
        ) from exc
    if process.returncode != 0:
        raise ImportStepError(
            "seed import failed; is the MCP server stopped? "
            f"stderr: {_tail(process.stderr)}"
        )
    return {
        "action": "imported",
        "command": command,
        "stdout_tail": _tail(process.stdout, 2000),
    }


def step_mcp(
    client: NexentClient,
    mcp_config: Mapping[str, Any],
    server_url: str,
    *,
    dry_run: bool,
    startup_help: str = (
        "Start it with `python -m mcp_server --transport streamable-http "
        "--host 0.0.0.0 --port 8765 --storage-dir "
        "domain_packs/industrial_fault/storage`"
    ),
) -> dict[str, Any]:
    """Converge the MCP service registration to ``server_url``."""
    name = str(mcp_config["name"])
    probe = client.test_mcp_connection(server_url)
    if not probe.get("success"):
        raise ImportStepError(
            f"MCP server {server_url} is not reachable from Nexent: "
            f"{probe.get('error') or 'no detail'}. Start it with "
            f"{startup_help}, "
            "use host.docker.internal when Nexent runs in Docker Desktop, and "
            "allow the port through the firewall. Then rerun this import; the "
            "seed step skips itself on the second pass."
        )

    details: dict[str, Any] = {"name": name, "url": server_url, "probe": probe}
    existing = client.find_mcp(name)
    if existing is None:
        details["action"] = "would_add" if dry_run else "added"
        if not dry_run:
            client.add_mcp(
                name,
                server_url,
                description=str(mcp_config.get("description") or ""),
                tags=[str(tag) for tag in mcp_config.get("tags") or ()],
            )
            # ``add`` accepts ``enabled`` but the row is only trusted after a
            # re-read; some versions create it disabled and expect /mcp/enable.
            added = client.find_mcp(name)
            if added is not None:
                # Record the id so ROLLBACK.md can name the row to delete.
                details["mcp_id"] = added.get("mcp_id")
                if not added.get("enabled"):
                    client.enable_mcp(int(added["mcp_id"]))
                    details["action"] = "added,enabled"
        return details

    details["mcp_id"] = existing.get("mcp_id")
    details["previous_url"] = existing.get("remote_mcp_server")
    actions: list[str] = []
    if existing.get("remote_mcp_server") != server_url:
        actions.append("would_update" if dry_run else "updated")
        if not dry_run:
            client.update_mcp(
                int(existing["mcp_id"]),
                name,
                server_url,
                description=str(mcp_config.get("description") or ""),
                tags=[str(tag) for tag in mcp_config.get("tags") or ()],
            )
    if not existing.get("enabled"):
        actions.append("would_enable" if dry_run else "enabled")
        if not dry_run:
            client.enable_mcp(int(existing["mcp_id"]))
    details["action"] = ",".join(actions) if actions else "unchanged"
    return details


def step_governance_store(options: ImportOptions) -> dict[str, Any]:
    """Bootstrap the canonical ontology only when governance is empty."""

    if options.skip_governance_mcp:
        return {"action": "skipped", "reason": "--skip-governance-mcp"}
    active = options.governance_root / "active.json"
    if active.is_file():
        return {"action": "skipped", "reason": f"{active} already exists"}
    if options.dry_run:
        installed = bool(
            any(
                path.is_dir()
                for path in (options.governance_root / "versions").glob("*")
            )
        )
        if installed:
            return {
                "action": "would_validate",
                "reason": "installed versions require an explicit active pointer",
            }
        return {
            "action": "would_bootstrap",
            "ontology": str(options.governance_ontology),
            "root": str(options.governance_root),
        }
    try:
        installed = bootstrap_ontology_governance(
            governance_root=options.governance_root,
            ontology_path=options.governance_ontology,
        )
    except Exception as exc:
        raise ImportStepError(f"ontology governance bootstrap failed: {exc}") from exc
    if installed is None:
        return {
            "action": "skipped",
            "reason": "active ontology already exists",
        }
    return {
        "action": "bootstrapped",
        "version": installed.ontology_version,
        "root": str(options.governance_root),
    }


def step_scan_tools(client: NexentClient, *, dry_run: bool) -> dict[str, Any]:
    """Refresh the tool catalog so the MCP tools become importable."""
    if dry_run:
        return {"action": "would_scan"}
    client.scan_tools()
    return {"action": "scanned"}


def step_catalog(
    client: NexentClient,
    specs: AgentSpec | Sequence[AgentSpec],
    *,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the live catalog and a coverage report for the agent's tools."""
    specs = _as_specs(specs)
    catalog = client.list_tools()
    present = {
        (tool.get("name"), tool.get("source"))
        for tool in catalog
        if isinstance(tool, dict)
    }
    missing_mcp = [
        name
        for name in sorted({tool for spec in specs for tool in spec.mcp_tools})
        if (name, "mcp") not in present
    ]
    missing_local = sorted(
        {
            spec.kb_tool.name
            for spec in specs
            if spec.kb_tool is not None and (spec.kb_tool.name, "local") not in present
        }
    )
    details: dict[str, Any] = {
        "tool_count": len(catalog),
        "missing_mcp_tools": missing_mcp,
        "missing_local_tools": missing_local,
    }
    if missing_mcp or missing_local:
        message = (
            "tools are missing from the Nexent catalog after scan: "
            f"{missing_mcp + missing_local}"
        )
        if dry_run:
            details["action"] = "warning"
            details["warning"] = message
        else:
            raise ImportStepError(
                message + "; check `GET /mcp/healthcheck` for the service and the MCP "
                "server log, then rerun"
            )
    else:
        details["action"] = "ok"
    return catalog, details


def step_skills(
    client: NexentClient,
    skills_config: Path,
    skills_out: Path,
    mcp_server_name: str,
    *,
    refresh: bool,
    dry_run: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    """Build the Skill ZIPs and decide which of them Nexent must create."""
    try:
        build = build_skills.build_all(
            config_path=skills_config,
            out_dir=skills_out,
            mcp_server_name=mcp_server_name,
        )
    except build_skills.SkillPackagingError as exc:
        raise ImportStepError(f"skill packaging failed: {exc}") from exc

    configured = [entry["name"] for entry in build["skills"]]
    existing = {
        skill.get("name")
        for skill in client.list_skills()
        if isinstance(skill, dict) and skill.get("name")
    }

    removed: list[str] = []
    if refresh:
        for name in configured:
            if name in existing:
                removed.append(name)
                if not dry_run:
                    client.delete_skill(name)
        if removed:
            existing -= set(removed)

    skills_payload: list[dict[str, str]] = []
    for entry in build["skills"]:
        archive = (skills_out / entry["path"]).read_bytes()
        skills_payload.append(
            {
                "skill_name": entry["name"],
                "skill_zip_base64": base64.b64encode(archive).decode("ascii"),
            }
        )

    resolutions = [
        {"skill_name": name, "action": "use_existing"}
        for name in configured
        if name in existing
    ]
    details = {
        "action": "would_delete_and_recreate"
        if refresh and dry_run
        else ("deleted_and_recreated" if removed else "built"),
        "skills": configured,
        "removed": removed,
        "reused": sorted(set(configured) & existing),
        "zips": [entry["path"] for entry in build["skills"]],
    }
    return skills_payload, resolutions, details


def resolve_kb(
    client: NexentClient,
    kb_config: Mapping[str, Any],
    *,
    dry_run: bool,
    poll_interval: float,
    wait_seconds: int,
) -> dict[str, Any]:
    """Create or reuse the knowledge base and ingest its documents."""
    display_name = kb_config.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ImportStepError("knowledge_base.json has no display_name")
    display_name = display_name.strip()

    files = [_repo_path(str(relative)) for relative in kb_config.get("files") or ()]
    if not files:
        raise ImportStepError("knowledge_base.json lists no files")
    for path in files:
        if not path.is_file():
            raise ImportStepError(f"knowledge base document not found: {path}")

    status = client.check_index(display_name)
    details: dict[str, Any] = {"display_name": display_name, "check": status}
    if status == "exists_in_other_tenant":
        raise ImportStepError(
            f"knowledge base name {display_name!r} is taken by another tenant; "
            "change knowledge_base.display_name in nexent/config/knowledge_base.json"
        )

    if status == "available":
        # ``check_exist`` and ``GET /indices`` have disagreed on Nexent 2.8
        # after a configuration-service restart. Prefer an index that is
        # actually visible to the signed-in tenant before requiring a model.
        existing_info = client.find_index_by_display_name(display_name)
        if existing_info is not None:
            details["action"] = "reused"
            details["index_name"] = existing_info.get("name")
            details["check_reconciled_from"] = "available"
            model = None
        else:
            model = None
            last_error: NexentConfigError | None = None
            for attempt in range(3):
                try:
                    model = client.resolve_embedding_model(
                        kb_config.get("embedding_model_display_name")
                    )
                    break
                except NexentConfigError as exc:
                    last_error = exc
                    if attempt == 2:
                        break
                    time.sleep(1)
                    existing_info = client.find_index_by_display_name(display_name)
                    if existing_info is not None:
                        details["action"] = "reused"
                        details["index_name"] = existing_info.get("name")
                        details["check_reconciled_from"] = "available"
                        break
            if model is None and existing_info is None:
                user = client.current_user()
                models = client.list_models()
                visible = [
                    {
                        "model_id": item.get("model_id"),
                        "name": item.get("display_name") or item.get("model_name"),
                        "model_type": item.get("model_type"),
                    }
                    for item in models[:20]
                ]
                raise ImportStepError(
                    f"knowledge base {display_name!r} does not exist for tenant "
                    f"{user.get('tenant_id')!r} and Nexent exposes no embedding "
                    f"model; visible models: {visible}. Confirm the same tenant "
                    "is selected, or configure an embedding model in Nexent."
                ) from last_error
        details["embedding_model"] = (
            model.get("display_name") or model.get("model_name")
            if model is not None
            else None
        )
        if dry_run:
            if model is None:
                details["files"] = [path.name for path in files]
                return details
            details.update(
                {
                    "action": "would_create",
                    "index_name": "(created-by-import)",
                    "files": [path.name for path in files],
                }
            )
            return details
        if model is not None:
            created = client.create_index(display_name, int(model["model_id"]))
            details["action"] = "created"
            details["index_name"] = created["id"]
    elif status == "exists_in_tenant":
        info = client.find_index_by_display_name(display_name)
        if info is None:
            raise ImportStepError(
                f"Nexent reports {display_name!r} exists but it is absent from "
                "`GET /indices`; retry, and check the knowledge table if it persists"
            )
        details["action"] = "reused"
        details["index_name"] = info.get("name")
    else:
        raise ImportStepError(
            f"unexpected check_exist status {status!r} for {display_name!r}"
        )

    index_name = details["index_name"]
    if dry_run:
        details["files"] = [path.name for path in files]
        return details

    indexed = {
        _file_basename(item)
        for item in client.list_index_files(index_name)
        if isinstance(item, dict)
    }
    pending = [path for path in files if path.name not in indexed]
    details["already_indexed"] = sorted(
        path.name for path in files if path.name in indexed
    )
    details["uploaded"] = [path.name for path in pending]
    if pending:
        upload = client.upload_files(index_name, pending)
        if upload.get("errors"):
            raise ImportStepError(f"knowledge base upload failed: {upload['errors']}")
        records = {
            str(record.get("object_name")): record
            for record in upload.get("file_records") or []
            if isinstance(record, dict)
        }
        process_files: list[dict[str, Any]] = []
        for path, object_name, filename in zip(
            pending,
            upload.get("uploaded_file_paths") or [],
            upload.get("uploaded_filenames") or [],
        ):
            entry: dict[str, Any] = {
                "path_or_url": object_name,
                "filename": filename or path.name,
            }
            record = records.get(str(object_name))
            if record and record.get("file_id"):
                entry["file_id"] = record["file_id"]
            process_files.append(entry)
        if not process_files:
            raise ImportStepError(
                "knowledge base upload returned no uploaded_file_paths"
            )
        client.process_files(
            index_name,
            process_files,
            chunking_strategy=str(kb_config.get("chunking_strategy") or "basic"),
        )
        details["processed"] = [entry["filename"] for entry in process_files]

    details["statuses"] = _wait_for_ingestion(
        client,
        index_name,
        [path.name for path in files],
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
    )
    return details


def _file_basename(item: Mapping[str, Any]) -> str:
    """Return the filename of one file-list entry, whatever form Nexent used."""
    return Path(str(item.get("file") or item.get("path_or_url") or "")).name


def _wait_for_ingestion(
    client: NexentClient,
    index_name: str,
    expected: Sequence[str],
    *,
    wait_seconds: int,
    poll_interval: float,
) -> dict[str, str]:
    """Poll ``GET /indices/{name}/files`` until every expected file completes.

    Nexent reports the celery states directly: ``WAIT_FOR_PROCESSING``,
    ``PROCESSING``, ``FORWARDING``, ``COMPLETED``, ``PROCESS_FAILED`` and
    ``FORWARD_FAILED``. A zero budget skips the wait and reports the states
    observed once.
    """
    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        statuses: dict[str, str] = {}
        for item in client.list_index_files(index_name):
            if not isinstance(item, dict):
                continue
            name = _file_basename(item)
            statuses[name] = str(item.get("status") or "").upper()
        failed = {
            name: statuses.get(name)
            for name in expected
            if statuses.get(name) in FAILED_STATUSES
        }
        if failed:
            raise ImportStepError(
                f"knowledge base ingestion failed for {failed}; check the "
                f"`{index_name}` file list for error details"
            )
        done = all(statuses.get(name) == "COMPLETED" for name in expected)
        if done:
            return {name: statuses[name] for name in expected}
        if time.monotonic() >= deadline:
            pending = {name: statuses.get(name, "MISSING") for name in expected}
            raise ImportStepError(
                "knowledge base ingestion did not finish within "
                f"{wait_seconds}s: {pending}. It continues in the background; "
                "rerun with --wait-kb-seconds to keep waiting."
            )
        time.sleep(poll_interval)


def step_agent(
    client: NexentClient,
    specs: AgentSpec | Sequence[AgentSpec],
    catalog: Sequence[Mapping[str, Any]],
    *,
    kb_index_name: str | None,
    mcp_url: str,
    additional_mcp_server_urls: Mapping[str, str] | None = None,
    skills_payload: Sequence[Mapping[str, str]],
    skill_resolutions: Sequence[Mapping[str, str]],
    replace: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Import the main Agent and all managed child Agents."""
    specs = _as_specs(specs)
    resolved_specs: list[AgentSpec] = []
    for spec in specs:
        llm = client.resolve_llm_model(spec.model_name)
        resolved_specs.append(
            dataclasses.replace(
                spec,
                model_id=int(llm["model_id"]),
                model_name=str(llm.get("display_name") or llm.get("model_name") or ""),
                business_logic_model_id=int(llm["model_id"]),
                business_logic_model_name=str(
                    llm.get("display_name") or llm.get("model_name") or ""
                ),
            )
        )
    primary = resolved_specs[0]
    details: dict[str, Any] = {
        "name": primary.name,
        "model": primary.model_name,
        "model_id": primary.model_id,
        "agents": [
            {
                "name": spec.name,
                "agent_id": spec.agent_id,
                "is_main_agent": spec.is_main_agent,
                "managed_agents": list(spec.managed_agents),
                "model": spec.model_name,
                "model_id": spec.model_id,
            }
            for spec in resolved_specs
        ],
    }
    actions: list[str] = []
    # Delete children before their manager so no dangling relation remains.
    for spec in reversed(resolved_specs):
        existing = client.get_agent_by_name(spec.name)
        if existing is None:
            actions.append(f"{spec.name}:{'would_create' if dry_run else 'created'}")
            continue
        if not replace:
            raise ImportStepError(
                f"agent {spec.name!r} already exists "
                f"(id={existing.get('agent_id')}); pass --replace-agent to "
                "delete and re-import the Agent graph"
            )
        actions.append(f"{spec.name}:{'would_replace' if dry_run else 'replaced'}")
        if not dry_run:
            client.delete_agent(int(existing["agent_id"]))
    details["action"] = ",".join(actions)

    if dry_run:
        return details

    if len(resolved_specs) == 1:
        bundle = build_agent_bundle(
            primary,
            catalog,
            kb_index_name=kb_index_name,
            mcp_server_url=mcp_url,
            additional_mcp_server_urls=additional_mcp_server_urls,
            agent_id=primary.agent_id,
        )
        validate_bundle(bundle, primary)
    else:
        bundle = build_multi_agent_bundle(
            resolved_specs,
            catalog,
            kb_index_name=kb_index_name,
            mcp_server_url=mcp_url,
            additional_mcp_server_urls=additional_mcp_server_urls,
        )
        validate_multi_agent_bundle(bundle, resolved_specs)
    payload = import_payload(
        bundle,
        skills=skills_payload,
        skill_resolutions=skill_resolutions,
    )
    result = client.import_agent(payload)
    details["agent_id"] = result.get("agent_id")
    details["agent_id_mapping"] = result.get("agent_id_mapping")
    if len(resolved_specs) > 1:
        main_agent_id = int(result["agent_id"])
        republished = client.publish_agent_version(
            main_agent_id,
            version_name="V2-managed-agents",
            release_note=(
                "Republish after importing managed Agent relations so the "
                "runtime snapshot contains all child Agents."
            ),
        )
        details["published_version_no"] = republished["version_no"]
    return details


def step_verify(
    client: NexentClient,
    specs: AgentSpec | Sequence[AgentSpec],
) -> dict[str, Any]:
    """Confirm every Agent is published, scoped, and linked to its manager."""
    specs = _as_specs(specs)
    found_agents: dict[str, dict[str, Any]] = {}
    for spec in specs:
        found = client.get_agent_by_name(spec.name)
        if found is None:
            raise ImportStepError(f"agent {spec.name!r} is missing after import")
        if not found.get("latest_version_no"):
            raise ImportStepError(f"agent {spec.name!r} has no published version")
        found_agents[spec.name] = found

    present = {
        (tool.get("name"), tool.get("source"))
        for tool in client.list_tools()
        if isinstance(tool, dict)
    }
    missing = [
        name
        for name in sorted({tool for spec in specs for tool in spec.mcp_tools})
        if (name, "mcp") not in present
    ]
    if missing:
        raise ImportStepError(f"MCP tools disappeared after import: {missing}")
    main = specs[0]
    main_found = found_agents[main.name]
    relation = client.get_agent_call_relationship(int(main_found["agent_id"]))
    child_ids = {
        str(found_agents[spec.name]["agent_id"])
        for spec in specs
        if not spec.is_main_agent
    }
    actual_child_ids = {
        str(item.get("agent_id"))
        for item in relation.get("sub_agents") or []
        if isinstance(item, dict)
    }
    if actual_child_ids != child_ids:
        raise ImportStepError(
            "imported Agent graph does not match the declared relationships: "
            f"expected {sorted(child_ids)}, got {sorted(actual_child_ids)}"
        )
    if int(main_found.get("latest_version_no") or 0) <= 0:
        raise ImportStepError("main Agent has no published version")
    published_detail = client.get_agent_version_detail(
        int(main_found["agent_id"]),
        int(main_found["latest_version_no"]),
    )
    published_child_ids = {
        str(item.get("agent_id"))
        for item in published_detail.get("sub_agent_relations") or []
        if isinstance(item, dict) and item.get("agent_id") is not None
    }
    if len(specs) > 1 and published_child_ids != child_ids:
        raise ImportStepError(
            "published Agent snapshot does not contain the managed Agent "
            f"relations: expected {sorted(child_ids)}, got "
            f"{sorted(published_child_ids)}"
        )
    details: dict[str, Any] = {
        "agent_id": main_found.get("agent_id"),
        "latest_version_no": main_found.get("latest_version_no"),
        "agents": [
            {
                "name": spec.name,
                "agent_id": found_agents[spec.name].get("agent_id"),
                "latest_version_no": found_agents[spec.name].get("latest_version_no"),
                "is_main_agent": spec.is_main_agent,
            }
            for spec in specs
        ],
        "sub_agent_ids": sorted(child_ids),
        "published_relation_ids": sorted(published_child_ids),
    }
    details["mcp_tools"] = sorted({tool for spec in specs for tool in spec.mcp_tools})
    return details


def run_import(
    options: ImportOptions,
    *,
    client: NexentClient | None = None,
    out: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run every step and return the report (never raises past this point)."""
    report: dict[str, Any] = {
        "generated_by": "nexent/scripts/import_to_nexent.py",
        "generated_at": _utc_now(),
        "config_url": options.config_url,
        "dry_run": options.dry_run,
        "steps": {},
        "ok": True,
    }
    active = client or NexentClient(options.config_url)
    try:
        out("[1/8] sign in")
        active.signin(options.email, options.password)
        user = active.current_user()
        tenant_id = str(user.get("tenant_id") or "")
        if not tenant_id:
            raise ImportStepError("current user has no tenant_id")
        report["tenant_id"] = tenant_id
        report["steps"]["identity"] = {
            "user_id": user.get("user_id"),
            "tenant_id": tenant_id,
            "role": user.get("role"),
        }
        out(f"      tenant {tenant_id}")

        out("[2/8] seed domain pack")
        report["steps"]["seed"] = step_seed(options, tenant_id)
        out(f"      {report['steps']['seed']['action']}")

        mcp_config = _load_json(DEFAULT_MCP_CONFIG)
        server_url = options.mcp_url or str(mcp_config.get("server_url") or "")
        if not server_url:
            raise ImportStepError("no MCP url: set --mcp-url or config/mcp.json")
        mcp_name = str(mcp_config["name"])
        governance_config = _load_json(DEFAULT_GOVERNANCE_MCP_CONFIG)
        governance_url = options.governance_mcp_url or str(
            governance_config.get("server_url") or ""
        )
        governance_name = str(governance_config["name"])

        out("[3/8] MCP registration")
        report["steps"]["mcp"] = step_mcp(
            active, mcp_config, server_url, dry_run=options.dry_run
        )
        out(f"      {report['steps']['mcp']['action']}")
        if options.skip_governance_mcp:
            report["steps"]["governance_store"] = {
                "action": "skipped",
                "reason": "--skip-governance-mcp",
            }
            report["steps"]["governance_mcp"] = {"action": "skipped"}
            out("      governance MCP skipped")
        else:
            if not governance_url:
                raise ImportStepError(
                    "no governance MCP url: set --governance-mcp-url or "
                    "config/governance_mcp.json"
                )
            report["steps"]["governance_store"] = step_governance_store(options)
            out(f"      {report['steps']['governance_store']['action']}")
            report["steps"]["governance_mcp"] = step_mcp(
                active,
                governance_config,
                governance_url,
                dry_run=options.dry_run,
                startup_help=(
                    "Start it with `python -m governance_mcp_server "
                    "--bootstrap-if-empty --transport streamable-http "
                    "--host 0.0.0.0 --port 8766`"
                ),
            )
            out(f"      {report['steps']['governance_mcp']['action']}")

        out("[4/8] tool catalog")
        report["steps"]["scan"] = step_scan_tools(active, dry_run=options.dry_run)
        specs = load_agent_specs(DEFAULT_AGENT_CONFIG)
        if options.skip_governance_mcp:
            specs = tuple(
                dataclasses.replace(
                    spec,
                    mcp_tools=tuple(
                        name for name in spec.mcp_tools if name not in GOVERNANCE_TOOLS
                    ),
                    additional_mcp_server_names=(),
                )
                for spec in specs
            )
        catalog, coverage = step_catalog(active, specs, dry_run=options.dry_run)
        report["steps"]["catalog"] = coverage
        out(f"      {coverage['action']} ({coverage['tool_count']} tools)")

        out("[5/8] skills")
        skills_payload, resolutions, skill_details = step_skills(
            active,
            DEFAULT_SKILLS_CONFIG,
            DEFAULT_SKILLS_OUT,
            mcp_name,
            refresh=options.refresh_skills,
            dry_run=options.dry_run,
        )
        report["steps"]["skills"] = skill_details
        out(f"      {skill_details['action']} {skill_details['skills']}")

        out("[6/8] knowledge base")
        kb_config = _load_json(DEFAULT_KB_CONFIG)
        if options.skip_kb:
            report["steps"]["knowledge_base"] = {"action": "skipped"}
            kb_index_name = None
            out("      skipped")
        else:
            kb_details = resolve_kb(
                active,
                kb_config,
                dry_run=options.dry_run,
                poll_interval=options.poll_interval,
                wait_seconds=options.wait_kb_seconds,
            )
            report["steps"]["knowledge_base"] = kb_details
            kb_index_name = str(kb_details["index_name"])
            out(f"      {kb_details['action']} {kb_index_name}")

        out("[7/8] agent import")
        report["steps"]["agent"] = step_agent(
            active,
            specs,
            catalog,
            kb_index_name=kb_index_name,
            mcp_url=server_url,
            additional_mcp_server_urls=(
                {} if options.skip_governance_mcp else {governance_name: governance_url}
            ),
            skills_payload=skills_payload,
            skill_resolutions=resolutions,
            replace=options.replace_agent,
            dry_run=options.dry_run,
        )
        out(f"      {report['steps']['agent']['action']}")

        out("[8/8] verify")
        if options.dry_run:
            report["steps"]["verify"] = {"action": "skipped (dry run)"}
            out("      skipped")
        else:
            report["steps"]["verify"] = step_verify(active, specs)
            out(
                "      agent "
                f"{report['steps']['verify']['agent_id']} "
                f"v{report['steps']['verify']['latest_version_no']}"
            )
    except (
        ImportStepError,
        AgentBundleError,
        NexentError,
        NexentConfigError,
    ) as exc:
        report["ok"] = False
        report["error"] = str(exc)
        out(f"[error] {exc}")
    except Exception as exc:  # unexpected: keep the report, then surface it
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        out(f"[error] {report['error']}")

    _write_report(options.report_path, report)
    report["report_path"] = str(options.report_path)
    return report


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact(dict(report)), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register the LightRAG industrial fault domain in Nexent.",
    )
    parser.add_argument(
        "--config-url", default=os.environ.get("NEXENT_CONFIG_URL", DEFAULT_CONFIG_URL)
    )
    parser.add_argument(
        "--email", default=os.environ.get("NEXENT_EMAIL", DEFAULT_EMAIL)
    )
    parser.add_argument(
        "--password", default=os.environ.get("NEXENT_PASSWORD", DEFAULT_PASSWORD)
    )
    parser.add_argument("--mcp-url", default=os.environ.get("NEXENT_MCP_URL"))
    parser.add_argument(
        "--governance-mcp-url",
        default=os.environ.get("NEXENT_GOVERNANCE_MCP_URL"),
    )
    parser.add_argument(
        "--skip-governance-mcp",
        action="store_true",
        help="Import only the original industrial domain MCP.",
    )
    parser.add_argument(
        "--governance-root",
        default=str(DEFAULT_GOVERNANCE_ROOT),
    )
    parser.add_argument(
        "--governance-ontology",
        default=str(DEFAULT_GOVERNANCE_ONTOLOGY),
    )
    parser.add_argument(
        "--seed",
        choices=("auto", "always", "never"),
        default="auto",
        help="Seed import policy: auto skips when the tenant's active.json exists.",
    )
    parser.add_argument("--seed-storage", default=str(DEFAULT_SEED_STORAGE))
    parser.add_argument("--seed-timeout", type=int, default=600)
    parser.add_argument(
        "--refresh-skills",
        action="store_true",
        help="Delete existing Skills first so edited SKILL.md content propagates.",
    )
    parser.add_argument("--skip-kb", action="store_true")
    parser.add_argument(
        "--no-replace-agent",
        action="store_false",
        dest="replace_agent",
        help="Fail instead of deleting an existing agent with the same name.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait-kb-seconds", type=int, default=180)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    options = ImportOptions(
        config_url=args.config_url,
        email=args.email,
        password=args.password,
        mcp_url=args.mcp_url,
        governance_mcp_url=args.governance_mcp_url,
        governance_root=Path(args.governance_root).expanduser().resolve(),
        governance_ontology=Path(args.governance_ontology).expanduser().resolve(),
        skip_governance_mcp=args.skip_governance_mcp,
        seed=args.seed,
        seed_storage=Path(args.seed_storage).expanduser().resolve(),
        seed_timeout=args.seed_timeout,
        refresh_skills=args.refresh_skills,
        skip_kb=args.skip_kb,
        replace_agent=args.replace_agent,
        dry_run=args.dry_run,
        wait_kb_seconds=args.wait_kb_seconds,
        poll_interval=args.poll_interval,
        report_path=Path(args.report).expanduser().resolve(),
    )
    report = run_import(options)
    print(json.dumps({"ok": report["ok"], "report": str(report["report_path"])}))
    if not report["ok"]:
        print(f"[error] {report.get('error')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
