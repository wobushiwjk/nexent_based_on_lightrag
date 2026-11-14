"""End-to-end tests for ontology publication, migration, and rollback."""

from __future__ import annotations

import json
from pathlib import Path

from governance_mcp_server.bootstrap import bootstrap_ontology_governance
from governance_mcp_server.service import GovernanceService
from lightrag.fault_tree.governance import OntologyStandardDraftRequest
from lightrag.fault_tree.importer import build_imported_domain, write_bundle_outputs
from mcp_server.storage_repository import DomainStorageRepository

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "domain_packs" / "industrial_fault"
EXTRACTION = PACK / "seeds" / "extraction"


def _prepare_storage(tmp_path: Path) -> tuple[Path, Path, str]:
    storage = tmp_path / "storage"
    imported = build_imported_domain(PACK, EXTRACTION)
    write_bundle_outputs(imported, storage, prune_tenant=True)
    governance_root = storage / "governance"
    bootstrap_ontology_governance(
        governance_root=governance_root,
        ontology_path=PACK / "ontology.yml",
    )
    return storage, governance_root, imported.bundle.tenant_id


def _request() -> OntologyStandardDraftRequest:
    source = (
        "Condition monitoring systems shall identify sensors installed on "
        "rotating equipment."
    )
    return OntologyStandardDraftRequest.model_validate(
        {
            "change_id": "CHG-EVOLUTION-1",
            "proposed_version": "industrial-fault-v0.2",
            "author": "engineer",
            "rationale": "Add condition-monitoring sensor concepts.",
            "source_name": "ISO 13374 excerpt",
            "source_text": source,
            "operations": [
                {
                    "kind": "class",
                    "action": "add",
                    "code": "Sensor",
                    "after": {"parent": "Asset", "label": "Sensor"},
                    "evidence": [
                        {
                            "quote": source,
                            "source_name": "ISO 13374 excerpt",
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
            ],
        }
    )


def _approved_service(tmp_path: Path) -> tuple[GovernanceService, Path, str]:
    storage, governance_root, tenant_id = _prepare_storage(tmp_path)
    service = GovernanceService(
        governance_root,
        domain_storage_root=storage,
    )
    service.create_standard_draft(_request())
    service.submit_change_review(
        "CHG-EVOLUTION-1",
        actor="engineer",
        reason="Ready for review",
    )
    service.review_ontology_change(
        "CHG-EVOLUTION-1",
        decision="approve",
        actor="reviewer",
        reason="Impact and evidence accepted",
    )
    return service, storage, tenant_id


def test_publish_migrates_snapshots_and_keeps_history(tmp_path: Path) -> None:
    service, storage, tenant_id = _approved_service(tmp_path)
    before = json.loads(
        (storage / "tenants" / tenant_id / "active.json").read_text(encoding="utf-8")
    )

    impact = service.analyze_change_impact("CHG-EVOLUTION-1")
    dry_run = service.publish_change(
        "CHG-EVOLUTION-1",
        actor="publisher",
        dry_run=True,
    )
    published = service.publish_change(
        "CHG-EVOLUTION-1",
        actor="publisher",
    )

    after = json.loads(
        (storage / "tenants" / tenant_id / "active.json").read_text(encoding="utf-8")
    )
    assert impact["summary"]["tenant_count"] == 1
    assert impact["affected"]["classes"]
    assert dry_run["status"] == "dry_run"
    assert published["status"] == "published"
    assert service.versions.read_active().active_version == "industrial-fault-v0.2"
    assert after["run_id"] != before["run_id"]
    assert after["ontology_version"] == "industrial-fault-v0.2"
    assert (storage / "tenants" / tenant_id / "runs" / before["run_id"]).is_dir()
    loaded = service.versions.load_version("industrial-fault-v0.2")
    artifacts = loaded.schema_artifacts()
    entity_schema = json.loads(artifacts["entity.schema.json"])
    bundle_schema = json.loads(artifacts["domain_bundle.schema.json"])
    assert "Sensor" in entity_schema["$defs"]
    assert entity_schema["discriminator"]["mapping"]["Sensor"] == "#/$defs/Sensor"
    assert "#/$defs/Sensor" in {item["$ref"] for item in entity_schema["oneOf"]}
    assert (
        bundle_schema["properties"]["entities"]["items"]["discriminator"]["mapping"][
            "Sensor"
        ]
        == "#/$defs/Sensor"
    )
    detail = service.get_version(
        "industrial-fault-v0.2",
        include_schema=True,
    )
    assert detail["schemas"]
    assert detail["schema_artifacts"]["entity.schema.json"]["$defs"]["Sensor"]
    assert loaded.document()["classes"]["Sensor"]["schema_ref"].endswith(
        "#/$defs/Sensor"
    )

    repository = DomainStorageRepository.from_storage_dir(
        PACK,
        storage,
        default_tenant=tenant_id,
    )
    try:
        assert repository.ontology.version == "industrial-fault-v0.2"
        assert "Sensor" in repository.ontology.classes
    finally:
        repository.close()

    history = service.tenant_snapshot_history(tenant_id)
    assert history["active_run_id"] == after["run_id"]
    assert len(history["runs"]) == 2


def test_rollback_restores_previous_snapshot_without_deleting_new(
    tmp_path: Path,
) -> None:
    service, storage, tenant_id = _approved_service(tmp_path)
    before = json.loads(
        (storage / "tenants" / tenant_id / "active.json").read_text(encoding="utf-8")
    )
    service.publish_change("CHG-EVOLUTION-1", actor="publisher")
    after = json.loads(
        (storage / "tenants" / tenant_id / "active.json").read_text(encoding="utf-8")
    )
    repository = DomainStorageRepository.from_storage_dir(
        PACK,
        storage,
        default_tenant=tenant_id,
    )

    rolled_back = service.rollback_publication(
        "CHG-EVOLUTION-1",
        actor="reviewer",
        reason="Verification requires the previous ontology.",
    )
    restored = json.loads(
        (storage / "tenants" / tenant_id / "active.json").read_text(encoding="utf-8")
    )

    assert rolled_back["status"] == "rolled_back"
    assert service.versions.read_active().active_version == "industrial-fault-v0.1"
    assert restored["run_id"] == before["run_id"]
    assert (storage / "tenants" / tenant_id / "runs" / after["run_id"]).is_dir()
    try:
        assert repository.refresh_active_ontology() is True
        assert repository.ontology.version == "industrial-fault-v0.1"
    finally:
        repository.close()
