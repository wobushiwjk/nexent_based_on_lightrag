"""Offline tests for the eight-step Nexent import flow.

A stub client stands in for Nexent; the only real work is Skill packaging,
which is deterministic and writes under ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from nexent.scripts import import_to_nexent as flow
from nexent.scripts.agent_bundle import load_agent_specs
from nexent.scripts.nexent_client import NexentConfigError

MCP_TOOLS = (
    "get_ontology",
    "get_equipment",
    "search_faults",
    "get_fault_tree",
    "trace_evidence",
    "validate_fault_tree",
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
    "list_uploaded_documents",
    "read_uploaded_document",
    "ingest_uploaded_document",
    "get_upload_ingestion_status",
)

GOVERNANCE_TOOLS = {
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
INGESTION_TOOLS = {
    "list_uploaded_documents",
    "read_uploaded_document",
    "ingest_uploaded_document",
    "get_upload_ingestion_status",
}


def catalog() -> list[dict]:
    entries = [
        {
            "name": name,
            "source": "mcp",
            "description": name,
            "description_zh": name,
            "inputs": "{}",
            "labels": None,
            "usage": (
                "ontology-governance"
                if name in GOVERNANCE_TOOLS
                else (
                    "industrial-fault-ingestion"
                    if name in INGESTION_TOOLS
                    else "industrial-fault-domain"
                )
            ),
        }
        for name in MCP_TOOLS
    ]
    entries.append(
        {
            "name": "knowledge_base_search",
            "source": "local",
            "description": "kb",
            "description_zh": "kb",
            "inputs": "{}",
            "labels": None,
            "params": [
                {"name": "top_k"},
                {"name": "index_names"},
                {"name": "search_mode"},
                {"name": "rerank"},
                {"name": "rerank_model_name"},
            ],
        }
    )
    return entries


class StubClient:
    """Stateful stand-in for :class:`NexentClient`."""

    def __init__(self) -> None:
        self.mcp: dict | None = None
        self.governance_mcp: dict | None = None
        self.ingestion_mcp: dict | None = None
        self.skills = [{"name": "fault-diagnosis"}]
        self.processed = False
        self.uploaded: list[Path] = []
        self.agent: dict | None = None
        self.agents: dict[str, dict] = {}
        self.agent_relations: dict[int, list[int]] = {}
        self.calls: list[str] = []
        self.imported_payload: dict | None = None
        self.index_status = "available"
        self.reconciled_index: dict | None = None
        self.embedding_available = True
        self.embedding_failures_remaining = 0
        self.visible_models = [
            {
                "model_id": 3,
                "display_name": "bge-m3",
                "model_type": "embedding",
            }
        ]
        self.connection_ok = True

    # identity
    def signin(self, email, password):
        self.calls.append("signin")
        return {}

    def current_user(self):
        return {"user_id": "u-1", "tenant_id": "tenant-1", "role": "SU"}

    # MCP
    def test_mcp_connection(self, url):
        return {
            "success": self.connection_ok,
            "error": None if self.connection_ok else "refused",
        }

    def find_mcp(self, name):
        if self.mcp and self.mcp["remote_mcp_server_name"] == name:
            return dict(self.mcp)
        if (
            self.governance_mcp
            and self.governance_mcp["remote_mcp_server_name"] == name
        ):
            return dict(self.governance_mcp)
        if self.ingestion_mcp and self.ingestion_mcp["remote_mcp_server_name"] == name:
            return dict(self.ingestion_mcp)
        return None

    def add_mcp(self, name, url, *, description="", tags=(), enabled=True):
        self.calls.append("add_mcp")
        record = {
            "remote_mcp_server_name": name,
            "remote_mcp_server": url,
            "mcp_id": (
                12
                if name == "ontology-governance"
                else (13 if name == "industrial-fault-ingestion" else 11)
            ),
            "enabled": enabled,
        }
        if name == "ontology-governance":
            self.governance_mcp = record
        elif name == "industrial-fault-ingestion":
            self.ingestion_mcp = record
        else:
            self.mcp = record

    def update_mcp(self, mcp_id, name, url, *, description="", tags=()):
        self.calls.append("update_mcp")
        record = (
            self.governance_mcp
            if self.governance_mcp and self.governance_mcp["mcp_id"] == mcp_id
            else (
                self.ingestion_mcp
                if self.ingestion_mcp and self.ingestion_mcp["mcp_id"] == mcp_id
                else self.mcp
            )
        )
        assert record is not None
        record["remote_mcp_server"] = url

    def enable_mcp(self, mcp_id):
        self.calls.append("enable_mcp")
        record = (
            self.governance_mcp
            if self.governance_mcp and self.governance_mcp["mcp_id"] == mcp_id
            else (
                self.ingestion_mcp
                if self.ingestion_mcp and self.ingestion_mcp["mcp_id"] == mcp_id
                else self.mcp
            )
        )
        assert record is not None
        record["enabled"] = True

    # tools
    def scan_tools(self):
        self.calls.append("scan_tools")

    def list_tools(self):
        return catalog()

    # skills
    def list_skills(self):
        return list(self.skills)

    def delete_skill(self, name):
        self.calls.append(f"delete_skill:{name}")
        self.skills = [skill for skill in self.skills if skill["name"] != name]

    # knowledge base
    def check_index(self, name):
        return self.index_status

    def resolve_embedding_model(self, preferred=None):
        if self.embedding_failures_remaining > 0:
            self.embedding_failures_remaining -= 1
            raise NexentConfigError("Nexent has no embedding model configured")
        if not self.embedding_available:
            raise NexentConfigError("Nexent has no embedding model configured")
        return {"model_id": 3, "display_name": "bge-m3"}

    def list_models(self):
        return [dict(item) for item in self.visible_models]

    def create_index(self, name, embedding_model_id):
        self.calls.append("create_index")
        return {"id": "kb-index", "name": name}

    def find_index_by_display_name(self, name):
        if self.reconciled_index is not None:
            return dict(self.reconciled_index)
        if self.index_status == "exists_in_tenant":
            return {"name": "kb-index", "display_name": name}
        return None

    def list_index_files(self, index_name):
        if not self.processed:
            return []
        return [{"file": path.name, "status": "COMPLETED"} for path in self.uploaded]

    def upload_files(self, index_name, paths, **kwargs):
        self.calls.append("upload_files")
        self.uploaded = list(paths)
        names = [path.name for path in paths]
        return {
            "uploaded_file_paths": [f"kb/{name}" for name in names],
            "uploaded_filenames": names,
            "file_records": [
                {"object_name": f"kb/{name}", "file_id": f"file-{index}"}
                for index, name in enumerate(names)
            ],
        }

    def process_files(self, index_name, files, *, chunking_strategy="basic"):
        self.calls.append("process_files")
        self.processed = True
        return {"status": "ok"}

    # agents
    def resolve_llm_model(self, preferred=None):
        return {"model_id": 9, "display_name": "qwen"}

    def get_agent_by_name(self, name):
        if name in self.agents:
            return dict(self.agents[name])
        if self.agent and name == "industrial_fault_agent":
            return dict(self.agent)
        return None

    def delete_agent(self, agent_id):
        self.calls.append(f"delete_agent:{agent_id}")
        self.agent = None
        self.agents = {
            name: item
            for name, item in self.agents.items()
            if item.get("agent_id") != agent_id
        }
        self.agent_relations.pop(agent_id, None)

    def import_agent(self, payload):
        self.calls.append("import_agent")
        self.imported_payload = payload
        bundle = payload["agent_info"]
        mapping = {
            str(source_id): 5000 + index
            for index, source_id in enumerate(bundle["agent_info"], start=1)
        }
        self.agents = {
            info["name"]: {
                "agent_id": mapping[source_id],
                "latest_version_no": 1,
            }
            for source_id, info in bundle["agent_info"].items()
        }
        root_id = int(bundle["agent_id"])
        root = bundle["agent_info"][str(root_id)]
        self.agent_relations = {
            mapping[str(root_id)]: [
                mapping[str(child_id)] for child_id in root["managed_agents"]
            ]
        }
        self.agent = dict(self.agents[root["name"]])
        return {
            "agent_id": mapping[str(root_id)],
            "agent_id_mapping": mapping,
        }

    def get_agent_call_relationship(self, agent_id):
        self.calls.append(f"agent_relationship:{agent_id}")
        return {
            "agent_id": str(agent_id),
            "sub_agents": [
                {"agent_id": str(child_id)}
                for child_id in self.agent_relations.get(agent_id, [])
            ],
        }

    def publish_agent_version(self, agent_id, *, version_name, release_note):
        self.calls.append(f"publish_agent_version:{agent_id}")
        for item in self.agents.values():
            if item.get("agent_id") == agent_id:
                item["latest_version_no"] = int(item.get("latest_version_no", 1)) + 1
                if self.agent and self.agent.get("agent_id") == agent_id:
                    self.agent["latest_version_no"] = item["latest_version_no"]
                return {
                    "id": 200 + item["latest_version_no"],
                    "version_no": item["latest_version_no"],
                    "message": "Version published successfully",
                }
        raise AssertionError(f"unknown agent {agent_id}")

    def get_agent_version_detail(self, agent_id, version_no):
        return {
            "version_no": version_no,
            "sub_agent_relations": [
                {
                    "agent_id": child_id,
                    "version_no": 1,
                }
                for child_id in self.agent_relations.get(agent_id, [])
            ],
        }


def options(**overrides):
    base = flow.ImportOptions(
        seed="never",
        wait_kb_seconds=1,
        poll_interval=0.0,
        report_path=Path("unused.json"),
    )
    return dataclasses.replace(base, **overrides)


def test_step_seed_passes_storage_and_tenant(tmp_path, monkeypatch):
    recorded = {}

    class Completed:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(flow.subprocess, "run", fake_run)

    result = flow.step_seed(options(seed="always", seed_storage=tmp_path), "tenant-1")

    assert result["action"] == "imported"
    command = recorded["command"]
    assert command[1] == str(flow.SEED_SCRIPT)
    assert command[command.index("--tenant") + 1] == "tenant-1"
    assert command[command.index("--storage") + 1] == str(tmp_path)
    assert "--verify" in command


def test_step_seed_skips_when_active_pointer_exists(tmp_path):
    active = tmp_path / "tenants" / "tenant-1" / "active.json"
    active.parent.mkdir(parents=True)
    active.write_text("{}", encoding="utf-8")

    result = flow.step_seed(options(seed_storage=tmp_path), "tenant-1")

    assert result["action"] == "skipped"


def test_step_seed_reports_failure(tmp_path, monkeypatch):
    class Completed:
        returncode = 1
        stdout = ""
        stderr = "locked"

    monkeypatch.setattr(flow.subprocess, "run", lambda *a, **k: Completed())

    with pytest.raises(flow.ImportStepError) as info:
        flow.step_seed(options(seed="always", seed_storage=tmp_path), "tenant-1")

    assert "MCP server stopped" in str(info.value)


def test_step_mcp_adds_and_records_id():
    client = StubClient()

    details = flow.step_mcp(
        client,
        {"name": "industrial-fault-domain", "description": "d", "tags": []},
        "http://host.docker.internal:8765/mcp",
        dry_run=False,
    )

    assert details["action"] == "added"
    assert details["mcp_id"] == 11
    assert "add_mcp" in client.calls


def test_step_mcp_updates_url_and_enables():
    client = StubClient()
    client.mcp = {
        "remote_mcp_server_name": "industrial-fault-domain",
        "remote_mcp_server": "http://old:8765/mcp",
        "mcp_id": 7,
        "enabled": False,
    }

    details = flow.step_mcp(
        client,
        {"name": "industrial-fault-domain", "description": "d", "tags": []},
        "http://new:8765/mcp",
        dry_run=False,
    )

    assert details["action"] == "updated,enabled"
    assert client.mcp["remote_mcp_server"] == "http://new:8765/mcp"
    assert client.mcp["enabled"] is True


def test_step_mcp_rejects_unreachable_server():
    client = StubClient()
    client.connection_ok = False

    with pytest.raises(flow.ImportStepError) as info:
        flow.step_mcp(
            client,
            {"name": "industrial-fault-domain", "description": "d", "tags": []},
            "http://host.docker.internal:8765/mcp",
            dry_run=False,
        )

    assert "not reachable" in str(info.value)


def test_catalog_reports_missing_tools():
    client = StubClient()
    client.list_tools = lambda: [
        entry for entry in catalog() if entry["name"] != "get_equipment"
    ]

    with pytest.raises(flow.ImportStepError) as info:
        flow.step_catalog(client, load_agent_specs(), dry_run=False)

    assert "get_equipment" in str(info.value)


def test_catalog_dry_run_warns_instead_of_raising():
    client = StubClient()
    client.list_tools = lambda: [
        entry for entry in catalog() if entry["name"] != "get_equipment"
    ]

    _, details = flow.step_catalog(client, load_agent_specs(), dry_run=True)

    assert details["action"] == "warning"


def test_skills_refresh_deletes_before_recreating(tmp_path):
    client = StubClient()

    payload, resolutions, details = flow.step_skills(
        client,
        flow.DEFAULT_SKILLS_CONFIG,
        tmp_path,
        "industrial-fault-domain",
        refresh=True,
        dry_run=False,
    )

    assert "delete_skill:fault-diagnosis" in client.calls
    assert len(payload) == 3
    assert resolutions == []
    assert details["removed"] == ["fault-diagnosis"]
    assert (tmp_path / "fault-diagnosis.zip").is_file()


def test_skills_without_refresh_reuses_existing(tmp_path):
    client = StubClient()

    _, resolutions, details = flow.step_skills(
        client,
        flow.DEFAULT_SKILLS_CONFIG,
        tmp_path,
        "industrial-fault-domain",
        refresh=False,
        dry_run=False,
    )

    assert resolutions == [{"skill_name": "fault-diagnosis", "action": "use_existing"}]
    assert details["reused"] == ["fault-diagnosis"]


def test_resolve_kb_creates_uploads_and_waits():
    client = StubClient()
    config = {
        "display_name": "industrial-fault-kb",
        "files": [
            "nexent/kb/01-diagnosis-report-guide.md",
            "nexent/kb/02-pump-fault-terminology.md",
        ],
    }

    details = flow.resolve_kb(
        client, config, dry_run=False, poll_interval=0.0, wait_seconds=1
    )

    assert details["action"] == "created"
    assert details["index_name"] == "kb-index"
    assert details["uploaded"] == [
        "01-diagnosis-report-guide.md",
        "02-pump-fault-terminology.md",
    ]
    assert details["statuses"] == {
        "01-diagnosis-report-guide.md": "COMPLETED",
        "02-pump-fault-terminology.md": "COMPLETED",
    }
    assert "process_files" in client.calls


def test_resolve_kb_dry_run_does_not_create():
    client = StubClient()
    config = {
        "display_name": "industrial-fault-kb",
        "files": ["nexent/kb/01-diagnosis-report-guide.md"],
    }

    details = flow.resolve_kb(
        client, config, dry_run=True, poll_interval=0.0, wait_seconds=1
    )

    assert details["action"] == "would_create"
    assert "create_index" not in client.calls


def test_resolve_kb_reuses_index_when_check_exist_disagrees():
    client = StubClient()
    client.reconciled_index = {
        "name": "kb-index",
        "display_name": "industrial-fault-kb",
    }
    client.embedding_available = False
    config = {
        "display_name": "industrial-fault-kb",
        "files": ["nexent/kb/01-diagnosis-report-guide.md"],
    }

    details = flow.resolve_kb(
        client,
        config,
        dry_run=True,
        poll_interval=0.0,
        wait_seconds=1,
    )

    assert details["action"] == "reused"
    assert details["index_name"] == "kb-index"
    assert details["check_reconciled_from"] == "available"
    assert "create_index" not in client.calls


def test_resolve_kb_retries_transient_empty_model_list(monkeypatch):
    client = StubClient()
    client.embedding_failures_remaining = 2
    monkeypatch.setattr(flow.time, "sleep", lambda _seconds: None)
    config = {
        "display_name": "industrial-fault-kb",
        "files": ["nexent/kb/01-diagnosis-report-guide.md"],
    }

    details = flow.resolve_kb(
        client,
        config,
        dry_run=True,
        poll_interval=0.0,
        wait_seconds=1,
    )

    assert details["action"] == "would_create"
    assert details["embedding_model"] == "bge-m3"


def test_resolve_kb_reports_tenant_and_visible_models_when_embedding_is_absent(
    monkeypatch,
):
    client = StubClient()
    client.embedding_available = False
    monkeypatch.setattr(flow.time, "sleep", lambda _seconds: None)
    client.visible_models = [
        {"model_id": 9, "display_name": "qwen", "model_type": "llm"}
    ]
    config = {
        "display_name": "industrial-fault-kb",
        "files": ["nexent/kb/01-diagnosis-report-guide.md"],
    }

    with pytest.raises(flow.ImportStepError) as info:
        flow.resolve_kb(
            client,
            config,
            dry_run=True,
            poll_interval=0.0,
            wait_seconds=1,
        )

    message = str(info.value)
    assert "tenant-1" in message
    assert "qwen" in message
    assert "embedding" in message


def test_resolve_kb_rejects_name_taken_by_another_tenant():
    client = StubClient()
    client.index_status = "exists_in_other_tenant"

    with pytest.raises(flow.ImportStepError) as info:
        flow.resolve_kb(
            client,
            {
                "display_name": "industrial-fault-kb",
                "files": ["nexent/kb/01-diagnosis-report-guide.md"],
            },
            dry_run=False,
            poll_interval=0.0,
            wait_seconds=1,
        )

    assert "another tenant" in str(info.value)


def test_wait_for_ingestion_raises_on_failed_status():
    class Failing(StubClient):
        def list_index_files(self, index_name):
            return [{"file": "a.md", "status": "PROCESS_FAILED"}]

    with pytest.raises(flow.ImportStepError) as info:
        flow._wait_for_ingestion(
            Failing(), "kb-index", ["a.md"], wait_seconds=0, poll_interval=0.0
        )

    assert "PROCESS_FAILED" in str(info.value)


def test_wait_for_ingestion_times_out():
    class Pending(StubClient):
        def list_index_files(self, index_name):
            return [{"file": "a.md", "status": "PROCESSING"}]

    with pytest.raises(flow.ImportStepError) as info:
        flow._wait_for_ingestion(
            Pending(), "kb-index", ["a.md"], wait_seconds=0, poll_interval=0.0
        )

    assert "did not finish" in str(info.value)


def test_wait_for_ingestion_returns_completed_statuses():
    class Completed(StubClient):
        def list_index_files(self, index_name):
            return [{"file": "a.md", "status": "COMPLETED"}]

    statuses = flow._wait_for_ingestion(
        Completed(), "kb-index", ["a.md"], wait_seconds=0, poll_interval=0.0
    )

    assert statuses == {"a.md": "COMPLETED"}


def test_step_agent_refuses_to_replace_by_default():
    client = StubClient()
    client.agent = {"agent_id": 5, "latest_version_no": 2}

    with pytest.raises(flow.ImportStepError) as info:
        flow.step_agent(
            client,
            load_agent_specs(),
            catalog(),
            kb_index_name="kb-index",
            mcp_url="http://mcp",
            additional_mcp_server_urls={
                "ontology-governance": "http://governance",
                "industrial-fault-ingestion": "http://ingestion",
            },
            skills_payload=(),
            skill_resolutions=(),
            replace=False,
            dry_run=False,
        )

    assert "already exists" in str(info.value)


def test_step_agent_replaces_and_imports_bundle():
    client = StubClient()
    client.agent = {"agent_id": 5, "latest_version_no": 2}

    details = flow.step_agent(
        client,
        load_agent_specs(),
        catalog(),
        kb_index_name="kb-index",
        mcp_url="http://host.docker.internal:8765/mcp",
        additional_mcp_server_urls={
            "ontology-governance": "http://host.docker.internal:8766/mcp",
            "industrial-fault-ingestion": "http://host.docker.internal:8767/mcp",
        },
        skills_payload=(),
        skill_resolutions=(),
        replace=True,
        dry_run=False,
    )

    assert "industrial_fault_agent:replaced" in details["action"]
    assert "evidence_review_agent:created" in details["action"]
    assert "ontology_expert_agent:created" in details["action"]
    assert "delete_agent:5" in client.calls
    payload = client.imported_payload
    assert payload is not None
    info = payload["agent_info"]["agent_info"]["1001"]
    assert info["name"] == "industrial_fault_agent"
    assert all(
        tool["params"] == {} for tool in info["tools"] if tool["source"] == "mcp"
    )


def test_run_import_end_to_end(tmp_path, monkeypatch):
    client = StubClient()
    monkeypatch.setattr(flow, "DEFAULT_SKILLS_OUT", tmp_path / "skills")
    report_path = tmp_path / "report.json"

    report = flow.run_import(
        options(
            report_path=report_path,
            governance_root=tmp_path / "governance",
        ),
        client=client,
        out=lambda line: None,
    )

    assert report["ok"] is True
    assert report["tenant_id"] == "tenant-1"
    assert set(report["steps"]) == {
        "identity",
        "seed",
        "mcp",
        "governance_store",
        "governance_mcp",
        "ingestion_mcp",
        "scan",
        "catalog",
        "skills",
        "knowledge_base",
        "agent",
        "verify",
    }
    assert report["steps"]["agent"]["agent_id"] == 5001
    assert report["steps"]["verify"]["latest_version_no"] == 2
    assert report["steps"]["verify"]["sub_agent_ids"] == ["5002", "5003"]

    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["ok"] is True
    assert "password" not in json.dumps(written)


def test_run_import_writes_report_on_failure(tmp_path):
    client = StubClient()
    client.connection_ok = False
    report_path = tmp_path / "report.json"

    report = flow.run_import(
        options(report_path=report_path),
        client=client,
        out=lambda line: None,
    )

    assert report["ok"] is False
    assert "not reachable" in report["error"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["ok"] is False
