"""Run the complete W8 ontology-governance acceptance flow in isolation.

This command never touches the repository's active storage. It builds a
disposable tenant run, proves that an unresolved same-version draft blocks a
second proposal, rejects the superseded draft, publishes a source-supported
revision with versioned Schema artifacts, and rolls it back without deleting
history.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance_mcp_server.bootstrap import bootstrap_ontology_governance  # noqa: E402
from governance_mcp_server.service import GovernanceService  # noqa: E402
from lightrag.fault_tree.governance import OntologyStandardDraftRequest  # noqa: E402
from lightrag.fault_tree.importer import (  # noqa: E402
    build_imported_domain,
    write_bundle_outputs,
)
from mcp_server.storage_repository import DomainStorageRepository  # noqa: E402

PACK = ROOT / "domain_packs" / "industrial_fault"
EXTRACTION = PACK / "seeds" / "extraction"
SOURCE_TEXT = "条件监测系统应识别安装在旋转设备上的传感器。"


def _legacy_request() -> OntologyStandardDraftRequest:
    return OntologyStandardDraftRequest.model_validate(
        {
            "change_id": "CHG-LEGACY-SENSOR",
            "proposed_version": "industrial-fault-v0.2",
            "author": "acceptance",
            "rationale": "Superseded Sensor-only draft.",
            "source_name": "用户提供的条件监测标准节选",
            "source_text": SOURCE_TEXT,
            "operations": [
                {
                    "kind": "class",
                    "action": "add",
                    "code": "Sensor",
                    "after": {"label": "传感器", "parent": "Asset"},
                    "evidence": [
                        {
                            "quote": SOURCE_TEXT,
                            "source_name": "用户提供的条件监测标准节选",
                        }
                    ],
                }
            ],
        }
    )


def _revision_request() -> OntologyStandardDraftRequest:
    return OntologyStandardDraftRequest.model_validate(
        {
            "change_id": "CHG-0003",
            "proposed_version": "industrial-fault-v0.2",
            "author": "acceptance",
            "rationale": (
                "Model only the sensor and rotating-equipment concepts directly "
                "stated by the supplied standard excerpt."
            ),
            "source_name": "用户提供的条件监测标准节选",
            "source_text": SOURCE_TEXT,
            "operations": [
                {
                    "kind": "class",
                    "action": "add",
                    "code": "Sensor",
                    "after": {
                        "label": "传感器",
                        "parent": "Asset",
                        "prompt_visible": True,
                        "schema_ref": "schemas/entity.schema.json#/$defs/Sensor",
                    },
                    "evidence": [
                        {
                            "quote": "传感器",
                            "source_name": "用户提供的条件监测标准节选",
                        }
                    ],
                },
                {
                    "kind": "class",
                    "action": "add",
                    "code": "RotatingEquipment",
                    "after": {
                        "label": "旋转设备",
                        "parent": "Equipment",
                        "prompt_visible": True,
                        "schema_ref": (
                            "schemas/entity.schema.json#/$defs/RotatingEquipment"
                        ),
                    },
                    "evidence": [
                        {
                            "quote": "旋转设备",
                            "source_name": "用户提供的条件监测标准节选",
                        }
                    ],
                },
                {
                    "kind": "relation",
                    "action": "add",
                    "code": "INSTALLED_ON",
                    "after": {
                        "source_types": ["Sensor"],
                        "target_types": ["RotatingEquipment"],
                        "directed": True,
                        "prompt_visible": True,
                        "evidence_required": True,
                    },
                    "evidence": [
                        {
                            "quote": "安装在旋转设备上的传感器",
                            "source_name": "用户提供的条件监测标准节选",
                        }
                    ],
                },
            ],
        }
    )


def run_acceptance(workdir: str | Path) -> dict[str, Any]:
    """Execute the isolated acceptance and return a machine-readable report."""

    workspace = Path(workdir).resolve()
    storage = workspace / "storage"
    imported = build_imported_domain(PACK, EXTRACTION)
    write_bundle_outputs(imported, storage, prune_tenant=True)
    tenant_id = imported.bundle.tenant_id
    governance_root = storage / "governance"
    bootstrap_ontology_governance(
        governance_root=governance_root,
        ontology_path=PACK / "ontology.yml",
    )
    service = GovernanceService(
        governance_root,
        domain_storage_root=storage,
        domain_pack_root=PACK,
    )

    legacy = service.create_standard_draft(_legacy_request())
    conflict: dict[str, Any] | None = None
    try:
        service.create_standard_draft(_revision_request())
    except Exception as exc:
        details = getattr(exc, "details", {})
        conflict = {
            "code": getattr(exc, "code", type(exc).__name__),
            "conflicting_change_ids": details.get("conflicting_change_ids", []),
        }
    if not conflict or conflict["conflicting_change_ids"] != ["CHG-LEGACY-SENSOR"]:
        raise RuntimeError("same-version draft conflict was not rejected as expected")

    service.submit_change_review(
        "CHG-LEGACY-SENSOR",
        actor="acceptance",
        reason="Superseded by a source-supported revision.",
    )
    rejected = service.review_ontology_change(
        "CHG-LEGACY-SENSOR",
        decision="reject",
        actor="acceptance",
        reason="Superseded by CHG-0003; it omitted the installation relation.",
    )

    created = service.create_standard_draft(_revision_request())
    evidence_ids = {
        evidence_id
        for operation in created["change"]["operations"]
        for evidence_id in operation["evidence_ids"]
    }
    if len(evidence_ids) != 3:
        raise RuntimeError(
            "revision operations did not receive three distinct evidence IDs"
        )

    service.submit_change_review(
        "CHG-0003",
        actor="acceptance",
        reason="Deterministic W8 acceptance review.",
    )
    service.review_ontology_change(
        "CHG-0003",
        decision="approve",
        actor="acceptance",
        reason="Only source-supported classes and the installation relation are included.",
    )
    impact = service.analyze_change_impact("CHG-0003")
    dry_run = service.publish_change(
        "CHG-0003",
        actor="acceptance",
        dry_run=True,
    )
    published = service.publish_change("CHG-0003", actor="acceptance")

    repository = DomainStorageRepository.from_storage_dir(
        PACK,
        storage,
        default_tenant=tenant_id,
    )
    try:
        repository.refresh_active_ontology()
        version_detail = service.get_version(
            "industrial-fault-v0.2",
            include_schema=True,
        )
        entity_schema = version_detail["schema_artifacts"]["entity.schema.json"]
        bundle_schema = version_detail["schema_artifacts"]["domain_bundle.schema.json"]
        if not {
            "Sensor",
            "RotatingEquipment",
        }.issubset(entity_schema["$defs"]):
            raise RuntimeError("new classes are missing from versioned entity Schema")
        mapping = bundle_schema["properties"]["entities"]["items"]["discriminator"][
            "mapping"
        ]
        if mapping.get("Sensor") != "#/$defs/Sensor":
            raise RuntimeError("domain bundle Schema does not reference Sensor")
        if "INSTALLED_ON" not in repository.ontology.relations:
            raise RuntimeError("published domain ontology did not refresh INSTALLED_ON")
    finally:
        repository.close()

    history_before_rollback = service.tenant_snapshot_history(tenant_id)
    if len(history_before_rollback["runs"]) < 2:
        raise RuntimeError("publication did not retain both tenant snapshots")
    new_run_id = history_before_rollback["active_run_id"]

    rolled_back = service.rollback_publication(
        "CHG-0003",
        actor="acceptance",
        reason="Deterministic acceptance requires rollback verification.",
    )
    history_after_rollback = service.tenant_snapshot_history(tenant_id)
    if history_after_rollback["active_run_id"] == new_run_id:
        raise RuntimeError("rollback did not restore the previous tenant snapshot")
    if not (storage / "tenants" / tenant_id / "runs" / str(new_run_id)).is_dir():
        raise RuntimeError("rollback deleted the published snapshot")
    service.versions.load_version("industrial-fault-v0.2").schema_artifacts()
    if service.versions.read_active().active_version != "industrial-fault-v0.1":
        raise RuntimeError("rollback did not restore ontology v0.1")

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "legacy_change_id": legacy["change"]["change_id"],
        "conflict": conflict,
        "rejected_status": rejected["status"],
        "change_id": created["change"]["change_id"],
        "operation_count": created["operation_count"],
        "distinct_evidence_ids": sorted(evidence_ids),
        "impact_summary": impact["summary"],
        "dry_run_schema_artifacts": dry_run["schema_artifacts"],
        "published_status": published["status"],
        "published_version": published["publication"]["ontology_version"],
        "schema_version": published["publication"]["schema_version"],
        "snapshots_before_rollback": history_before_rollback["runs"],
        "rolled_back_status": rolled_back["status"],
        "snapshots_after_rollback": history_after_rollback["runs"],
        "final_active_version": service.versions.read_active().active_version,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an isolated end-to-end W8 ontology governance acceptance.",
    )
    parser.add_argument(
        "--workdir",
        help="Optional empty workspace. Defaults to a temporary directory.",
    )
    parser.add_argument("--report", help="Write the JSON report to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_path = Path(args.report).expanduser().resolve() if args.report else None
    if args.workdir:
        report = run_acceptance(args.workdir)
    else:
        with tempfile.TemporaryDirectory(prefix="w8-acceptance-") as temporary:
            report = run_acceptance(temporary)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SOURCE_TEXT",
    "build_parser",
    "main",
    "run_acceptance",
]
