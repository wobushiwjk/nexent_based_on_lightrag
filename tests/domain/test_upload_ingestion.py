"""End-to-end tests for the user-facing upload ingestion path."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import lightrag.fault_tree.ingestion as ingestion_module
from lightrag.fault_tree.importer import build_imported_domain, write_bundle_outputs
from lightrag.fault_tree.ingestion import (
    DocumentIngestionService,
    OpenAICompatibleStudentExtractor,
)
from lightrag.fault_tree.schemas import FaultCandidateBatch
from mcp_server.service import DomainService
from mcp_server.storage_repository import DomainStorageRepository

SOURCE_PACK = Path(__file__).resolve().parents[2] / "domain_packs" / "industrial_fault"


class FakeExtractor:
    def extract(
        self,
        *,
        chunk_id: str,
        asset_id: str,
        chunk_text: str,
        file_name: str,
        page: int | None = None,
        section_path: str | None = None,
        modality: str = "text",
    ) -> FaultCandidateBatch:
        assert file_name == "new-record.txt"
        return FaultCandidateBatch(
            chunk_id=chunk_id,
            chunk_text=chunk_text,
            asset_id=asset_id,
            entities=[
                {
                    "local_id": "wear",
                    "class": "FaultEvent",
                    "uid": "FAULT-BEARING-WEAR",
                    "name": "轴承磨损",
                    "description": "轴承磨损导致出口压力不足",
                    "evidence": [{"quote": "P-101A 轴承磨损会导致出口压力不足"}],
                }
            ],
            relations=[],
        )


def _pack_copy(tmp_path: Path) -> Path:
    target = tmp_path / "pack"
    shutil.copytree(
        SOURCE_PACK,
        target,
        ignore=shutil.ignore_patterns("storage", ".ingestion", "__pycache__"),
    )
    return target


def test_uploaded_document_is_visible_to_a_running_domain_service(
    tmp_path: Path,
) -> None:
    pack = _pack_copy(tmp_path)
    storage = pack / "storage"
    imported = build_imported_domain(pack, pack / "seeds" / "extraction")
    write_bundle_outputs(imported, storage, prune_tenant=True)

    repository = DomainStorageRepository.from_storage_dir(pack, storage)
    service = DomainService(repository)
    assert (
        service.search_faults(imported.bundle.tenant_id, "轴承磨损", 5)["results"] == []
    )

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    upload = inbox / "new-record.txt"
    upload.write_text("P-101A 轴承磨损会导致出口压力不足。", encoding="utf-8")
    ingestion = DocumentIngestionService(
        pack_dir=pack,
        storage_dir=storage,
        upload_dirs=[inbox],
        extractor=FakeExtractor(),
    )

    result = ingestion.ingest_uploaded_document(
        tenant_id=imported.bundle.tenant_id,
        file_name="new-record.txt",
    )

    assert result["projected_entities"] == 1
    assert result["projected_evidence"] == 1
    assert result["associated_equipment_uid"] == "EQ-P101A"
    assert result["associated_faults"] == ["FAULT-BEARING-WEAR"]
    assert result["run_id"] != "run-" + "0" * 12
    found = service.search_faults(imported.bundle.tenant_id, "轴承磨损", 5)["results"]
    assert [item["uid"] for item in found] == ["FAULT-BEARING-WEAR"]
    equipment = service.get_equipment(imported.bundle.tenant_id, "P-101A")
    assert "FAULT-BEARING-WEAR" in {item["uid"] for item in equipment["faults"]}
    assert repository.tenants == (imported.bundle.tenant_id,)
    repository.close()


def test_reingesting_same_document_keeps_one_content_addressed_run(
    tmp_path: Path,
) -> None:
    pack = _pack_copy(tmp_path)
    storage = pack / "storage"
    imported = build_imported_domain(pack, pack / "seeds" / "extraction")
    write_bundle_outputs(imported, storage, prune_tenant=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "new-record.txt").write_text(
        "P-101A 轴承磨损会导致出口压力不足。",
        encoding="utf-8",
    )
    ingestion = DocumentIngestionService(
        pack_dir=pack,
        storage_dir=storage,
        upload_dirs=[inbox],
        extractor=FakeExtractor(),
    )

    first = ingestion.ingest_uploaded_document(
        tenant_id=imported.bundle.tenant_id,
        file_name="new-record.txt",
    )
    second = ingestion.ingest_uploaded_document(
        tenant_id=imported.bundle.tenant_id,
        file_name="new-record.txt",
    )

    assert first["run_id"] == second["run_id"]
    runs = list((storage / "tenants" / imported.bundle.tenant_id / "runs").iterdir())
    assert len(runs) == 2


def test_standard_document_can_be_read_without_publishing_an_ontology_change(
    tmp_path: Path,
) -> None:
    pack = _pack_copy(tmp_path)
    storage = pack / "storage"
    imported = build_imported_domain(pack, pack / "seeds" / "extraction")
    write_bundle_outputs(imported, storage, prune_tenant=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "standard.md").write_text(
        "条件监测系统应识别安装在旋转设备上的传感器。",
        encoding="utf-8",
    )
    ingestion = DocumentIngestionService(
        pack_dir=pack,
        storage_dir=storage,
        upload_dirs=[inbox],
    )

    result = ingestion.read_uploaded_document(file_name="standard.md")

    assert result["source_text"] == "条件监测系统应识别安装在旋转设备上的传感器。"
    assert result["chunks"][0]["section_path"] == "text-1"
    active = (
        storage / "tenants" / imported.bundle.tenant_id / "active.json"
    ).read_text(encoding="utf-8")
    assert imported.bundle.bundle_version in active


def test_student_extractor_retries_a_contract_error(
    monkeypatch,
) -> None:
    extractor = OpenAICompatibleStudentExtractor(
        "http://student/v1",
        "industrial-extractor",
        max_attempts=2,
    )
    outputs = iter(
        [
            "not json",
            json.dumps(
                {
                    "entities": [
                        {
                            "local_id": "filter",
                            "class": "FaultEvent",
                            "name": "过滤器堵塞",
                            "evidence": [{"quote": "过滤器堵塞"}],
                        }
                    ],
                    "relations": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    monkeypatch.setattr(
        extractor,
        "_request_content",
        lambda payload, headers: next(outputs),
    )
    monkeypatch.setattr(ingestion_module.time, "sleep", lambda _: None)

    batch = extractor.extract(
        chunk_id="chunk-1",
        asset_id="FILE-1",
        chunk_text="过滤器堵塞",
        file_name="manual.txt",
    )

    assert [item.model_extra["name"] for item in batch.entities] == ["过滤器堵塞"]
