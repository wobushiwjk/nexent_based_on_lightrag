#!/usr/bin/env python
"""Import the multimodal sample pack and verify W6 acceptance criteria.

The flow is offline by default: PDF/table parsing is local, and the image is
transcribed by the manifest's ``transcript_fixture`` unless an explicit
``--vlm-endpoint`` selects a local OpenAI-compatible VLM.  ``--verify`` adds
the storage-backed MCP reads and the evidence-index query plan check.

Exit codes:

* ``0`` — every document parsed (or failures were explicitly allowed) and the
  import verified.
* ``1`` — a manifest, validation, or verification error.
* ``2`` — at least one document failed and ``--allow-failures`` was not set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lightrag.fault_tree.evidence_store import EvidenceStore  # noqa: E402
from lightrag.fault_tree.importer import (  # noqa: E402
    ImportValidationError,
    ImportedDomain,
    build_imported_domain_from_workspace,
    graph_projection_for,
    write_bundle_outputs,
    write_graph,
)
from lightrag.fault_tree.multimodal import (  # noqa: E402
    DocumentStatusStore,
    FixtureVlm,
    ImageTranscriber,
    MultimodalError,
    MultimodalManifest,
    OpenAICompatibleVlm,
    build_multimodal_workspace,
    load_manifest,
)
from lightrag.fault_tree.verification import (  # noqa: E402
    VerificationError,
    active_run_dir,
    check_query_plan,
    check_storage_repository,
    verify_evidence,
)

DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"
DEFAULT_STORAGE = DEFAULT_PACK / "storage"
DEFAULT_STATUS = DEFAULT_PACK / "storage" / "multimodal_status.json"


def _store_counts(storage: Path, tenant_id: str) -> dict[str, int]:
    run_dir = active_run_dir(storage, tenant_id)
    store = EvidenceStore(run_dir / "evidence.sqlite3")
    try:
        return {
            "evidence_rows": store.count(tenant_id),
            "target_rows": store.target_count(tenant_id),
        }
    finally:
        store.close()


def _fixture_path(manifest: MultimodalManifest, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (manifest.pack / path).resolve()


def _build_vlm(
    manifest: MultimodalManifest, args: argparse.Namespace
) -> ImageTranscriber | None:
    if args.vlm_endpoint:
        if not args.vlm_model and not manifest.vlm.model:
            raise MultimodalError(
                "vlm-not-configured",
                "--vlm-endpoint also needs --vlm-model or manifest.vlm.model",
            )
        return OpenAICompatibleVlm(
            args.vlm_endpoint,
            args.vlm_model or str(manifest.vlm.model),
            api_key=args.vlm_api_key or manifest.vlm.api_key,
            timeout=manifest.vlm.timeout,
            allow_external=args.allow_external_vlm,
        )
    if args.vlm_fixture:
        return FixtureVlm.from_file(_fixture_path(manifest, args.vlm_fixture))
    if manifest.vlm.transcript_fixture:
        return FixtureVlm.from_file(
            _fixture_path(manifest, manifest.vlm.transcript_fixture)
        )
    if manifest.vlm.endpoint and manifest.vlm.model:
        return OpenAICompatibleVlm(
            manifest.vlm.endpoint,
            manifest.vlm.model,
            api_key=manifest.vlm.api_key,
            timeout=manifest.vlm.timeout,
            allow_external=args.allow_external_vlm,
        )
    raise MultimodalError(
        "vlm-not-configured",
        "image documents need a local VLM endpoint or a transcript fixture",
    )


def _statuses_payload(statuses: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for status in statuses:
        row = {"asset_id": status.asset_id, **status.to_dict()}
        rows.append(row)
    return rows


def _equipment_identity(imported: ImportedDomain) -> tuple[str, str]:
    for entity in imported.projected.entities:
        if getattr(entity, "class_code", "") == "Equipment":
            return entity.equipment_tag, entity.uid
    return "P-101A", "EQ-P101A"


def run(
    *,
    pack: Path,
    manifest_path: str | None,
    storage: Path,
    status_file: Path,
    tenant_id: str | None,
    retry_failed: bool,
    allow_failures: bool,
    vlm: ImageTranscriber | None,
    verify: bool,
) -> int:
    manifest = load_manifest(pack, manifest_path).with_tenant(tenant_id)
    store = DocumentStatusStore(status_file)
    ingested = build_multimodal_workspace(
        manifest,
        vlm=vlm,
        status_store=store,
        retry_failed=retry_failed,
    )
    parsed = [item for item in ingested.statuses if item.status == "parsed"]
    failed = ingested.failed
    if failed and not allow_failures:
        print(
            json.dumps(
                {
                    "tenant": manifest.tenant_id,
                    "parsed": len(parsed),
                    "failed": len(failed),
                    "documents": _statuses_payload(ingested.statuses),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(
            "[error] refusing to import: failed documents need a retry or "
            "--allow-failures",
            file=sys.stderr,
        )
        return 2
    if not ingested.workspace.chunks or not ingested.workspace.batches:
        print(
            json.dumps(
                {
                    "tenant": manifest.tenant_id,
                    "parsed": len(parsed),
                    "failed": len(failed),
                    "documents": _statuses_payload(ingested.statuses),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(
            "[error] no parsed chunk with an extraction fixture remains",
            file=sys.stderr,
        )
        return 2

    imported = build_imported_domain_from_workspace(
        pack, ingested.workspace
    )
    evidence_checks = verify_evidence(imported)

    first = write_bundle_outputs(imported, storage, prune_tenant=True)
    first_counts = _store_counts(storage, imported.bundle.tenant_id)
    second = write_bundle_outputs(imported, storage)
    second_counts = _store_counts(storage, imported.bundle.tenant_id)
    if first_counts != second_counts:
        raise VerificationError(
            "repeated import changed evidence counts: "
            f"{first_counts} -> {second_counts}"
        )
    if first["counts"] != second["counts"]:
        raise VerificationError("repeated import changed bundle counts")
    if first["run_id"] != second["run_id"]:
        raise VerificationError(
            f"repeated import created a new run: "
            f"{first['run_id']} -> {second['run_id']}"
        )

    expected_graph = {
        "nodes": len(graph_projection_for(imported).nodes),
        "edges": len(graph_projection_for(imported).edges),
    }
    graph_first = asyncio.run(write_graph(imported, storage))
    graph_second = asyncio.run(write_graph(imported, storage))
    if graph_first != graph_second:
        raise VerificationError(
            f"repeated import changed graph counts: {graph_first} -> "
            f"{graph_second}"
        )
    if graph_first != expected_graph:
        raise VerificationError(
            f"graph counts {graph_first} != projection {expected_graph}"
        )

    result: dict[str, Any] = {
        "tenant": imported.bundle.tenant_id,
        "run_id": first["run_id"],
        "parsed": len(parsed),
        "failed": len(failed),
        "documents": _statuses_payload(ingested.statuses),
        "entities": len(imported.bundle.entities),
        "evidence": len(imported.bundle.evidence),
        "facts": len(imported.bundle.facts),
        "rules": len(imported.bundle.rules),
        "tree_nodes": (
            len(imported.tree.nodes) if imported.tree is not None else 0
        ),
        "tree_edges": (
            len(imported.tree.edges) if imported.tree is not None else 0
        ),
        "evidence_checks": evidence_checks,
        "evidence_rows": first_counts["evidence_rows"],
        "target_rows": first_counts["target_rows"],
        "graph": graph_first,
        "warnings": imported.projected.warnings,
    }
    if verify:
        result["query_plan"] = check_query_plan(
            storage, imported.bundle.tenant_id
        )
        rule_uid = (
            imported.projected.rules[0].rule_uid
            if imported.projected.rules
            else None
        )
        if rule_uid is None:
            raise VerificationError(
                "the imported domain has no rule to trace"
            )
        equipment_tag, equipment_uid = _equipment_identity(imported)
        result["storage_repository"] = check_storage_repository(
            pack,
            storage,
            imported.bundle.tenant_id,
            rule_uid=rule_uid,
            equipment_tag=equipment_tag,
            equipment_uid=equipment_uid,
            top_event_uid=manifest.top_event_uid,
            draft_id=str(manifest.draft_id),
        )
        if result["storage_repository"]["quotes_loaded"] == 0:
            raise VerificationError("no quote was loaded on demand")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "Manifest path, relative to the pack. Defaults to "
            "seeds/multimodal/manifest.json."
        ),
    )
    parser.add_argument("--storage", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument(
        "--status-file", type=Path, default=DEFAULT_STATUS
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help="Import under this tenant instead of the manifest tenant.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reuse parsed images and retry only documents that failed.",
    )
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Import the parsed subset when some documents failed.",
    )
    parser.add_argument(
        "--vlm-endpoint",
        default=None,
        help="Local OpenAI-compatible endpoint; overrides the fixture.",
    )
    parser.add_argument("--vlm-model", default=None)
    parser.add_argument(
        "--vlm-api-key",
        default=os.environ.get("DOMAIN_VLM_API_KEY", ""),
    )
    parser.add_argument(
        "--vlm-fixture",
        default=None,
        help="Transcript fixture path, relative to the pack.",
    )
    parser.add_argument(
        "--allow-external-vlm",
        action="store_true",
        help="Permit a non-local VLM endpoint (privacy exception).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Also check the evidence query plan and MCP storage reads.",
    )
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(
            args.pack.resolve(), args.manifest
        ).with_tenant(args.tenant)
        vlm = _build_vlm(manifest, args)
        return run(
            pack=args.pack.resolve(),
            manifest_path=args.manifest,
            storage=args.storage.resolve(),
            status_file=args.status_file.resolve(),
            tenant_id=args.tenant,
            retry_failed=args.retry_failed,
            allow_failures=args.allow_failures,
            vlm=vlm,
            verify=args.verify,
        )
    except (
        MultimodalError,
        VerificationError,
        ImportValidationError,
        ValueError,
    ) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
