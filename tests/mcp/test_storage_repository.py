"""Tests for the SQLite-backed read-only MCP repository."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from lightrag.fault_tree.evidence_store import EvidenceStore
from lightrag.fault_tree.importer import build_imported_domain, write_bundle_outputs
from mcp_server.config import ServerConfig
from mcp_server.errors import DomainRepositoryError
from mcp_server.service import DomainService
from mcp_server.storage_repository import DomainStorageRepository

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "domain_packs" / "industrial_fault"
EXTRACTION = PACK / "seeds" / "extraction"


@pytest.fixture()
def storage_dir(tmp_path):
    imported = build_imported_domain(PACK, EXTRACTION)
    write_bundle_outputs(imported, tmp_path, prune_tenant=True)
    return tmp_path


@pytest.fixture()
def repository(storage_dir):
    repo = DomainStorageRepository.from_storage_dir(PACK, storage_dir)
    try:
        yield repo
    finally:
        repo.close()


def test_startup_loads_no_quotes(repository) -> None:
    assert repository.tenants == ("tenant-demo",)
    assert repository.quotes_loaded == 0
    index = repository.tenant_index("tenant-demo")
    assert len(index.evidence) == 7
    assert len(index.evidence_by_target[("rule", "RULE-CAVITATION-AND")]) >= 1
    assert repository.quotes_loaded == 0


def test_tools_load_quotes_only_on_demand(repository) -> None:
    service = DomainService(repository)  # type: ignore[arg-type]
    result = service.trace_evidence(
        "tenant-demo", "rule", "RULE-CAVITATION-AND"
    )
    assert result["count"] >= 1
    assert repository.quotes_loaded >= 1
    assert any("quote" in item for item in result["evidence"])

    equipment = service.get_equipment("tenant-demo", "P-101A")
    assert equipment["equipment"]["uid"] == "EQ-P101A"
    tree = service.get_fault_tree(
        "tenant-demo", top_event_uid="FAULT-LOW-PRESSURE"
    )
    assert tree["counts"]["nodes"] == 8
    assert tree["counts"]["edges"] == 8


def test_missing_evidence_database_is_rejected(storage_dir) -> None:
    db_path = _active_run_dir(storage_dir) / "evidence.sqlite3"
    db_path.unlink()
    with pytest.raises(DomainRepositoryError, match="evidence database"):
        DomainStorageRepository.from_storage_dir(PACK, storage_dir)


def test_runtime_bundle_with_quotes_is_rejected(storage_dir) -> None:
    run_dir = _active_run_dir(storage_dir)
    shutil.copyfile(run_dir / "bundle.full.json", run_dir / "bundle.json")
    with pytest.raises(DomainRepositoryError, match="evidence quotes"):
        DomainStorageRepository.from_storage_dir(PACK, storage_dir)


def test_repository_rejects_missing_evidence_rows(storage_dir) -> None:
    runtime_path = _active_run_dir(storage_dir) / "bundle.json"
    runtime = runtime_path.read_text(encoding="utf-8")
    # Point one referenced rule at an evidence ID that is not in SQLite.
    broken = runtime.replace("EV-MANUAL-3", "EV-NOT-STORED")
    runtime_path.write_text(broken, encoding="utf-8")
    with pytest.raises(DomainRepositoryError, match="missing from SQLite"):
        DomainStorageRepository.from_storage_dir(PACK, storage_dir)


def test_active_pointer_is_authoritative(storage_dir) -> None:
    tenant_dir = storage_dir / "tenants" / "tenant-demo"
    active_path = tenant_dir / "active.json"
    active = json.loads(active_path.read_text(encoding="utf-8"))
    active["run_id"] = "run-missing"
    active_path.write_text(
        json.dumps(active), encoding="utf-8"
    )
    with pytest.raises(DomainRepositoryError, match="active run"):
        DomainStorageRepository.from_storage_dir(PACK, storage_dir)


def test_repository_reads_only_the_active_run(storage_dir) -> None:
    imported = build_imported_domain(PACK, EXTRACTION)
    changed = imported.bundle.model_copy(
        update={"bundle_version": "seed-import-v2"}
    )
    write_bundle_outputs(replace(imported, bundle=changed), storage_dir)
    # Break the active run's evidence store; the older run stays complete.
    store = EvidenceStore(
        _active_run_dir(storage_dir) / "evidence.sqlite3"
    )
    try:
        store.delete_tenant("tenant-demo")
    finally:
        store.close()
    with pytest.raises(DomainRepositoryError, match="missing from SQLite"):
        DomainStorageRepository.from_storage_dir(PACK, storage_dir)


def test_legacy_bundle_layout_still_loads(storage_dir) -> None:
    tenant_dir = storage_dir / "tenants" / "tenant-demo"
    run_dir = _active_run_dir(storage_dir)
    shutil.copyfile(run_dir / "bundle.json", tenant_dir / "bundle.json")
    shutil.copyfile(
        run_dir / "evidence.sqlite3", tenant_dir / "evidence.sqlite3"
    )
    (tenant_dir / "active.json").unlink()

    repo = DomainStorageRepository.from_storage_dir(PACK, storage_dir)
    try:
        assert repo.tenants == ("tenant-demo",)
        assert repo.quotes_loaded == 0
    finally:
        repo.close()


def _active_run_dir(storage_dir: Path) -> Path:
    tenant_dir = storage_dir / "tenants" / "tenant-demo"
    active = json.loads(
        (tenant_dir / "active.json").read_text(encoding="utf-8")
    )
    return tenant_dir / "runs" / active["run_id"]


def test_config_reads_storage_dir(monkeypatch, storage_dir) -> None:
    monkeypatch.setenv("MCP_DOMAIN_STORAGE_DIR", str(storage_dir))
    config = ServerConfig.from_env()
    assert config.storage_dir == storage_dir.resolve()
