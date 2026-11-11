"""Deterministic seed-document import for the fault-tree domain.

The importer consumes a small, curated extraction fixture instead of calling an
LLM.  That keeps offline tests deterministic while exercising the same
projection, validation, evidence, tree, and graph-write paths that W7's local
model extraction will use later.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from lightrag.fault_tree.contracts import (
    DomainBundle,
    EntityRecord,
    FaultTreeDraft,
    FileAsset,
    ValidationIssue,
)
from lightrag.fault_tree.domain_validation import (
    errors_only,
    load_domain_bundle,
    validate_domain_bundle,
)
from lightrag.fault_tree.evidence_store import EvidenceStore
from lightrag.fault_tree.graph import GraphProjection, project_domain_graph
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.fault_tree.projection import (
    CandidateBatchInput,
    ProjectedDomain,
    SourceChunk,
    project_domain,
)
from lightrag.fault_tree.schemas import FaultCandidateBatch
from lightrag.fault_tree.tree_builder import build_fault_tree_draft

_ENTITY_ADAPTER = TypeAdapter(EntityRecord)


class ImportValidationError(ValueError):
    """The imported domain failed contract or tree validation."""

    def __init__(self, message: str, issues: list[ValidationIssue]) -> None:
        super().__init__(message)
        self.issues = issues


class SeedInputError(ValueError):
    """A seed context or extraction file is missing or malformed."""


@dataclass(frozen=True)
class SeedWorkspace:
    context: dict[str, Any]
    assets: list[EntityRecord]
    chunks: list[SourceChunk]
    batches: list[CandidateBatchInput]


@dataclass(frozen=True)
class ImportedDomain:
    workspace: SeedWorkspace
    projected: ProjectedDomain
    tree: FaultTreeDraft
    bundle: DomainBundle


def load_seed_workspace(
    pack_dir: str | Path,
    extraction_dir: str | Path,
    *,
    tenant_id: str | None = None,
) -> SeedWorkspace:
    """Load one seed fixture, optionally re-homing it under another tenant.

    ``tenant_id`` overrides the ``tenant_id`` stored in ``context.json`` and in
    every seed asset.  It exists so the same deterministic fixture can be
    imported under a deployment's own tenant (for example a Nexent tenant UUID)
    without editing the fixture.  Every asset must still agree with the chosen
    tenant, so a partial override is rejected rather than silently mixed.
    """

    pack = Path(pack_dir).resolve()
    extraction = Path(extraction_dir).resolve()
    context_path = extraction / "context.json"
    if not context_path.is_file():
        raise SeedInputError(f"seed context not found at {context_path}")
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedInputError(f"cannot read seed context: {exc}") from exc
    if not isinstance(context, dict):
        raise SeedInputError("seed context must contain a JSON object")

    override = str(tenant_id or "").strip()
    if override:
        context["tenant_id"] = override

    tenant_id = str(context.get("tenant_id") or "").strip()
    if not tenant_id:
        raise SeedInputError("seed context must define tenant_id")
    ontology_version = str(context.get("ontology_version") or "").strip()
    if not ontology_version:
        raise SeedInputError("seed context must define ontology_version")
    schema_version = str(context.get("schema_version") or "").strip()
    if not schema_version:
        schema_version = ontology_version.replace(
            "industrial-fault", "industrial-fault-schema"
        )

    assets: list[EntityRecord] = []
    try:
        for item in context.get("assets") or []:
            record = _ENTITY_ADAPTER.validate_python(item)
            if override and record.tenant_id != tenant_id:
                record = record.model_copy(update={"tenant_id": tenant_id})
            if record.tenant_id != tenant_id:
                raise SeedInputError(
                    f"asset {record.uid!r} belongs to a different tenant"
                )
            assets.append(record)
    except ImportValidationError:
        raise
    except Exception as exc:
        raise SeedInputError(f"cannot parse seed asset: {exc}") from exc

    files = context.get("files") or []
    if not isinstance(files, list):
        raise SeedInputError("seed context files must be a list")
    for spec in files:
        if not isinstance(spec, dict):
            raise SeedInputError("each seed file entry must be an object")
        assets.append(
            _build_file_asset(
                pack=pack,
                spec=spec,
                tenant_id=tenant_id,
                ontology_version=ontology_version,
                schema_version=schema_version,
                created_by=str(context.get("created_by") or "seed"),
            )
        )

    chunks: list[SourceChunk] = []
    chunk_ids: set[str] = set()
    for item in context.get("chunks") or []:
        if not isinstance(item, dict):
            raise SeedInputError("each seed chunk must be an object")
        chunk = SourceChunk(
            asset_id=str(item.get("asset_id") or ""),
            chunk_id=str(item.get("chunk_id") or ""),
            text=str(item.get("text") or ""),
            page=item.get("page"),
            section_path=item.get("section_path"),
            modality=item.get("modality") or "text",
            char_base=int(item.get("char_base") or 0),
        )
        if not chunk.asset_id or not chunk.chunk_id or not chunk.text:
            raise SeedInputError("seed chunks require asset_id, chunk_id, text")
        if chunk.chunk_id in chunk_ids:
            raise SeedInputError(f"duplicate seed chunk_id {chunk.chunk_id!r}")
        chunk_ids.add(chunk.chunk_id)
        chunks.append(chunk)
    if not chunks:
        raise SeedInputError("seed context must define at least one chunk")

    chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    asset_by_uid = {asset.uid: asset for asset in assets}
    for chunk in chunks:
        if chunk.asset_id not in asset_by_uid:
            raise SeedInputError(
                f"chunk {chunk.chunk_id!r} references unknown asset "
                f"{chunk.asset_id!r}"
            )

    extraction_files = context.get("extraction_files") or []
    if not isinstance(extraction_files, list) or not extraction_files:
        raise SeedInputError(
            "seed context must list extraction_files in import order"
        )

    batches: list[CandidateBatchInput] = []
    seen_extraction_chunks: set[str] = set()
    for file_name in extraction_files:
        path = extraction / str(file_name)
        if not path.is_file():
            raise SeedInputError(f"extraction file not found at {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SeedInputError(
                f"cannot read extraction file {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise SeedInputError(f"extraction file {path} must be an object")
        chunk_id = str(raw.get("chunk_id") or "").strip()
        if chunk_id not in chunk_by_id:
            raise SeedInputError(
                f"extraction file {path.name} references unknown chunk "
                f"{chunk_id!r}"
            )
        if chunk_id in seen_extraction_chunks:
            raise SeedInputError(
                f"chunk {chunk_id!r} has more than one extraction file"
            )
        seen_extraction_chunks.add(chunk_id)
        try:
            batch = FaultCandidateBatch(
                chunk_id=chunk_id,
                chunk_text=chunk_by_id[chunk_id].text,
                asset_id=chunk_by_id[chunk_id].asset_id,
                entities=raw.get("entities") or [],
                relations=raw.get("relations") or [],
                warnings=list(raw.get("warnings") or []),
            )
        except Exception as exc:
            raise SeedInputError(
                f"invalid extraction candidate in {path.name}: {exc}"
            ) from exc
        batches.append(
            CandidateBatchInput(chunk=chunk_by_id[chunk_id], batch=batch)
        )

    return SeedWorkspace(
        context=context,
        assets=assets,
        chunks=chunks,
        batches=batches,
    )


def build_imported_domain(
    pack_dir: str | Path,
    extraction_dir: str | Path,
    *,
    ontology: OntologyRegistry | None = None,
    review_status: str | None = None,
    bundle_version: str | None = None,
    tenant_id: str | None = None,
) -> ImportedDomain:
    pack = Path(pack_dir).resolve()
    extraction = Path(extraction_dir).resolve()
    if ontology is None:
        ontology = OntologyRegistry.from_file(pack / "ontology.yml")
    workspace = load_seed_workspace(pack, extraction, tenant_id=tenant_id)
    context = workspace.context
    tenant_id = str(context["tenant_id"])
    schema_version = str(
        context.get("schema_version") or ontology.schema_version or ""
    )
    if not schema_version:
        raise SeedInputError("seed context must define schema_version")

    created_at = _parse_datetime(context.get("created_at"))
    projected = project_domain(
        ontology,
        tenant_id=tenant_id,
        batches=workspace.batches,
        assets=workspace.assets,
        schema_version=schema_version,
        review_status=review_status or context.get("review_status") or "approved",
        created_by=str(context.get("created_by") or "seed"),
        created_at=created_at,
        extractor=str(context.get("created_by") or "seed"),
        extraction_run_id=str(context.get("extraction_run_id") or "seed"),
    )

    tree_created_at = _parse_datetime(context.get("tree_created_at")) or created_at
    tree = build_fault_tree_draft(
        tenant_id=tenant_id,
        ontology_version=ontology.version,
        entities=projected.entities,
        rules=projected.rules,
        top_event_uid=str(context.get("top_event_uid") or ""),
        draft_id=context.get("draft_id"),
        version=int(context.get("tree_version") or 1),
        review_status=review_status or context.get("review_status") or "approved",
        created_by=str(context.get("created_by") or "seed"),
        created_at=tree_created_at,
    )

    bundle = DomainBundle(
        bundle_version=bundle_version
        or str(context.get("bundle_version") or "seed-import-v1"),
        ontology_version=ontology.version,
        schema_version=schema_version,
        tenant_id=tenant_id,
        entities=projected.entities,
        evidence=projected.evidence,
        facts=projected.facts,
        rules=projected.rules,
        tree=tree,
        trace=None,
    )
    issues = validate_domain_bundle(bundle, pack)
    errors = errors_only(issues)
    if errors:
        raise ImportValidationError(
            "imported domain failed bundle validation",
            issues,
        )
    return ImportedDomain(
        workspace=workspace,
        projected=projected,
        tree=tree,
        bundle=bundle,
    )


def bundle_digest(bundle: DomainBundle) -> str:
    """Return the SHA-256 of the canonical full-bundle JSON text."""

    return hashlib.sha256(
        _json_text(bundle.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def run_id_for_bundle(bundle: DomainBundle) -> str:
    """Content-addressed run ID; identical bundles resolve to one snapshot."""

    return f"run-{bundle_digest(bundle)[:12]}"


def write_bundle_outputs(
    imported: ImportedDomain,
    storage_dir: str | Path,
    *,
    prune_tenant: bool = False,
) -> dict[str, Any]:
    """Publish one versioned snapshot and point the tenant at it.

    The snapshot lives in ``tenants/<tenant>/runs/<run_id>/`` and
    ``tenants/<tenant>/active.json`` names the run readers must use.  The
    pointer is replaced atomically after the snapshot is complete, so a crash
    leaves the previous run active and the new one unreferenced; re-running the
    import overwrites the same content-addressed directory.

    ``prune_tenant`` drops every other run and any pre-versioning layout under
    the tenant directory.  It is a maintenance flag for the seed import, not
    something a concurrent reader should use.
    """

    storage = Path(storage_dir).resolve()
    tenant_dir = storage / "tenants" / imported.bundle.tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    full_payload = imported.bundle.model_dump(mode="json")
    digest = hashlib.sha256(
        _json_text(full_payload).encode("utf-8")
    ).hexdigest()
    run_id = f"run-{digest[:12]}"
    run_dir = tenant_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    db_path = run_dir / "evidence.sqlite3"
    store = EvidenceStore(db_path)
    try:
        if prune_tenant:
            store.delete_tenant(imported.bundle.tenant_id)
        store.upsert_many(imported.bundle.evidence)
        full_path = run_dir / "bundle.full.json"
        runtime_path = run_dir / "bundle.json"
        _write_json(full_path, full_payload)
        runtime_bundle = imported.bundle.model_copy(update={"evidence": []})
        _write_json(runtime_path, runtime_bundle.model_dump(mode="json"))
        manifest = {
            "tenant_id": imported.bundle.tenant_id,
            "run_id": run_id,
            "bundle_digest": digest,
            "bundle_version": imported.bundle.bundle_version,
            "ontology_version": imported.bundle.ontology_version,
            "schema_version": imported.bundle.schema_version,
            "counts": {
                "entities": len(imported.bundle.entities),
                "evidence": len(imported.bundle.evidence),
                "evidence_rows_in_store": store.count(imported.bundle.tenant_id),
                "facts": len(imported.bundle.facts),
                "rules": len(imported.bundle.rules),
                "tree_nodes": (
                    len(imported.bundle.tree.nodes)
                    if imported.bundle.tree is not None
                    else 0
                ),
                "tree_edges": (
                    len(imported.bundle.tree.edges)
                    if imported.bundle.tree is not None
                    else 0
                ),
            },
            "run_dir": str(run_dir),
            "evidence_db": str(db_path),
            "full_bundle": str(full_path),
            "runtime_bundle": str(runtime_path),
            "warnings": imported.projected.warnings,
        }
        _write_json(run_dir / "manifest.json", manifest)

        active_path = tenant_dir / "active.json"
        _write_json_atomic(
            active_path,
            {
                "tenant_id": imported.bundle.tenant_id,
                "run_id": run_id,
                "bundle_digest": digest,
                "bundle_version": imported.bundle.bundle_version,
                "ontology_version": imported.bundle.ontology_version,
                "schema_version": imported.bundle.schema_version,
                "runtime_bundle": f"runs/{run_id}/bundle.json",
                "manifest": f"runs/{run_id}/manifest.json",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Only after the new run is complete and published is it safe to drop
        # the superseded snapshots.  A crash before this point leaves them
        # intact and the pointer already names the new run.
        if prune_tenant:
            _prune_other_runs(tenant_dir, keep_run_id=run_id)
            _remove_legacy_layout(tenant_dir)
    finally:
        store.close()
    return manifest


def _prune_other_runs(tenant_dir: Path, *, keep_run_id: str) -> None:
    runs_dir = tenant_dir / "runs"
    if not runs_dir.is_dir():
        return
    for entry in sorted(runs_dir.iterdir()):
        if entry.is_dir() and entry.name != keep_run_id:
            shutil.rmtree(entry)


def _remove_legacy_layout(tenant_dir: Path) -> None:
    """Remove the pre-versioning tenant files superseded by ``runs/``."""

    for name in (
        "bundle.json",
        "bundle.full.json",
        "evidence.sqlite3",
        "manifest.json",
    ):
        (tenant_dir / name).unlink(missing_ok=True)
    legacy_graph = tenant_dir / "graph"
    if legacy_graph.is_dir():
        shutil.rmtree(legacy_graph)


def _build_file_asset(
    *,
    pack: Path,
    spec: dict[str, Any],
    tenant_id: str,
    ontology_version: str,
    schema_version: str,
    created_by: str,
) -> FileAsset:
    relative = str(spec.get("path") or "").strip()
    if not relative:
        raise SeedInputError("seed file entries require path")
    source = (pack / relative).resolve()
    if not source.is_file():
        raise SeedInputError(f"seed source file not found at {source}")
    data = source.read_bytes()
    upload_time = _parse_datetime(spec.get("upload_time")) or datetime.now()
    return FileAsset(
        uid=str(spec.get("asset_uid") or ""),
        tenant_id=tenant_id,
        canonical_name=str(spec.get("canonical_name") or source.name),
        description=spec.get("description"),
        ontology_version=ontology_version,
        schema_version=schema_version,
        review_status="approved",
        confidence=1.0,
        source_uids=[],
        evidence_ids=[],
        created_at=upload_time,
        updated_at=upload_time,
        created_by=created_by,
        file_name=source.name,
        file_extension=source.suffix.lstrip(".").lower() or "bin",
        mime_type=str(spec.get("mime_type") or "application/octet-stream"),
        modality=spec.get("modality") or "text",
        object_uri=relative,
        checksum="sha256:" + hashlib.sha256(data).hexdigest(),
        file_size=len(data),
        version=str(spec.get("version") or "v1"),
        language=spec.get("language"),
        source_system=str(spec.get("source_system") or "seed"),
        uploader_id=created_by,
        upload_time=upload_time,
        parse_status="parsed",
        parse_engine="native",
        page_count=spec.get("page_count") or 1,
        chunk_count=spec.get("chunk_count"),
        last_parsed_at=_parse_datetime(spec.get("last_parsed_at")),
    )


def graph_projection_for(imported: ImportedDomain) -> GraphProjection:
    return project_domain_graph(imported.bundle)


async def write_graph(
    imported: ImportedDomain,
    storage_dir: str | Path,
    *,
    namespace: str = "fault_domain",
) -> dict[str, int]:
    """Write the canonical nodes/edges through NetworkXStorage.

    The graph lands beside the runtime bundle of the same content-addressed
    run.  Publish the bundle first (``write_bundle_outputs``); a graph written
    for any other run is an orphan that no reader follows.
    """

    from lightrag.kg.networkx_impl import NetworkXStorage
    from lightrag.kg.shared_storage import initialize_share_data
    from lightrag.utils import EmbeddingFunc

    initialize_share_data()
    run_id = run_id_for_bundle(imported.bundle)
    graph_dir = (
        Path(storage_dir).resolve()
        / "tenants"
        / imported.bundle.tenant_id
        / "runs"
        / run_id
        / "graph"
    )
    graph_dir.mkdir(parents=True, exist_ok=True)

    async def _embed(texts: list[str]):  # pragma: no cover - never called
        import numpy as np

        return np.zeros((len(texts), 1), dtype=float)

    storage = NetworkXStorage(
        namespace=namespace,
        workspace="",
        global_config={"working_dir": str(graph_dir)},
        embedding_func=EmbeddingFunc(embedding_dim=1, func=_embed),
    )
    await storage.initialize()
    try:
        projection = graph_projection_for(imported)
        nodes = [
            (item["uid"], _graph_attributes(item["properties"]))
            for item in projection.nodes
        ]
        edges = [
            (
                item["source_uid"],
                item["target_uid"],
                _graph_attributes(item["properties"]),
            )
            for item in projection.edges
        ]
        await storage.upsert_nodes_batch(nodes)
        await storage.upsert_edges_batch(edges)
        await storage.index_done_callback()
        graph = await storage._get_graph()
        return {
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
        }
    finally:
        await storage.finalize()


def _graph_attributes(properties: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in properties.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            result[str(key)] = value
        else:
            result[str(key)] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return result


def compare_tree(
    actual: FaultTreeDraft,
    gold_path: str | Path,
) -> list[ValidationIssue]:
    """Return error issues when the rebuilt tree differs from the gold tree."""

    gold_bundle = load_domain_bundle(gold_path)
    if gold_bundle.tree is None:
        raise ImportValidationError(
            "gold bundle has no tree",
            [],
        )
    gold = gold_bundle.tree
    issues: list[ValidationIssue] = []
    if actual.top_event_uid != gold.top_event_uid:
        issues.append(
            _issue(
                "gold-top-event",
                f"{actual.top_event_uid!r} != {gold.top_event_uid!r}",
            )
        )
    if actual.draft_id != gold.draft_id:
        issues.append(
            _issue(
                "gold-draft-id",
                f"{actual.draft_id!r} != {gold.draft_id!r}",
            )
        )
    if [item.model_dump(mode="json") for item in actual.nodes] != [
        item.model_dump(mode="json") for item in gold.nodes
    ]:
        issues.append(_issue("gold-tree-nodes", "node lists differ"))
    if [item.model_dump(mode="json") for item in actual.edges] != [
        item.model_dump(mode="json") for item in gold.edges
    ]:
        issues.append(_issue("gold-tree-edges", "edge lists differ"))
    return issues


def _issue(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity="error",
        message=message,
        target_id=None,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_text(payload), encoding="utf-8")


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(_json_text(payload), encoding="utf-8")
    temp.replace(path)


__all__ = [
    "ImportValidationError",
    "ImportedDomain",
    "SeedInputError",
    "SeedWorkspace",
    "build_imported_domain",
    "bundle_digest",
    "compare_tree",
    "graph_projection_for",
    "load_seed_workspace",
    "run_id_for_bundle",
    "write_bundle_outputs",
    "write_graph",
]
