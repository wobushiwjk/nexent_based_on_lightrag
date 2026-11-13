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
from nexent.scripts.agent_bundle import load_agent_spec

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
)

GOVERNANCE_TOOLS = {
    "get_ontology_governance_status",
    "list_ontology_versions",
    "get_ontology_version",
    "list_ontology_changes",
    "get_ontology_change",
    "create_ontology_change_draft",
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
                else "industrial-fault-domain"
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
        self.skills = [{"name": "fault-diagnosis"}]
        self.processed = False
        self.uploaded: list[Path] = []
        self.agent: dict | None = None
        self.calls: list[str] = []
        self.imported_payload: dict | None = None
        self.index_status = "available"
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
        return None

    def add_mcp(self, name, url, *, description="", tags=(), enabled=True):
        self.calls.append("add_mcp")
        record = {
            "remote_mcp_server_name": name,
            "remote_mcp_server": url,
            "mcp_id": 12 if name == "ontology-governance" else 11,
            "enabled": enabled,
        }
        if name == "ontology-governance":
            self.governance_mcp = record
        else:
            self.mcp = record

    def update_mcp(self, mcp_id, name, url, *, description="", tags=()):
        self.calls.append("update_mcp")
        record = (
            self.governance_mcp
            if self.governance_mcp and self.governance_mcp["mcp_id"] == mcp_id
            else self.mcp
        )
        assert record is not None
        record["remote_mcp_server"] = url

    def enable_mcp(self, mcp_id):
        self.calls.append("enable_mcp")
        record = (
            self.governance_mcp
            if self.governance_mcp and self.governance_mcp["mcp_id"] == mcp_id
            else self.mcp
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
        return {"model_id": 3, "display_name": "bge-m3"}

    def create_index(self, name, embedding_model_id):
        self.calls.append("create_index")
        return {"id": "kb-index", "name": name}

    def find_index_by_display_name(self, name):
        return {"name": "kb-index", "display_name": name}

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
        return dict(self.agent) if self.agent else None

    def delete_agent(self, agent_id):
        self.calls.append(f"delete_agent:{agent_id}")
        self.agent = None

    def import_agent(self, payload):
        self.calls.append("import_agent")
        self.imported_payload = payload
        self.agent = {"agent_id": 55, "latest_version_no": 1}
        return {"agent_id": 55, "agent_id_mapping": {"1001": 55}}


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
        flow.step_catalog(client, load_agent_spec(), dry_run=False)

    assert "get_equipment" in str(info.value)


def test_catalog_dry_run_warns_instead_of_raising():
    client = StubClient()
    client.list_tools = lambda: [
        entry for entry in catalog() if entry["name"] != "get_equipment"
    ]

    _, details = flow.step_catalog(client, load_agent_spec(), dry_run=True)

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
            load_agent_spec(),
            catalog(),
            kb_index_name="kb-index",
            mcp_url="http://mcp",
            additional_mcp_server_urls={"ontology-governance": "http://governance"},
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
        load_agent_spec(),
        catalog(),
        kb_index_name="kb-index",
        mcp_url="http://host.docker.internal:8765/mcp",
        additional_mcp_server_urls={
            "ontology-governance": "http://host.docker.internal:8766/mcp"
        },
        skills_payload=(),
        skill_resolutions=(),
        replace=True,
        dry_run=False,
    )

    assert details["action"] == "replaced"
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
        "scan",
        "catalog",
        "skills",
        "knowledge_base",
        "agent",
        "verify",
    }
    assert report["steps"]["agent"]["agent_id"] == 55
    assert report["steps"]["verify"]["latest_version_no"] == 1

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
