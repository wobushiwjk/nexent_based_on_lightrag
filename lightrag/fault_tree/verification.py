"""Acceptance checks shared by the seed and multimodal import scripts.

These helpers read the artifacts the import already wrote: evidence rows,
the active run bundle, the SQLite store, and the graph.  They are verification
code, not part of the ingestion path, so a failure here means an acceptance
criterion failed rather than that input data was rejected.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from lightrag.fault_tree.contracts import Evidence
from lightrag.fault_tree.evidence_store import EvidenceStore
from lightrag.fault_tree.importer import ImportedDomain
from lightrag.fault_tree.projection import SourceChunk, span_matches


class VerificationError(RuntimeError):
    """One acceptance check failed."""


def verify_evidence(imported: ImportedDomain) -> dict[str, int]:
    """Check every evidence row is verbatim and every fact/rule traces to it."""

    chunks: dict[str, SourceChunk] = {
        chunk.chunk_id: chunk for chunk in imported.workspace.chunks
    }
    evidence_source: dict[str, SourceChunk] = {}
    for batch in imported.workspace.batches:
        payloads = [*batch.batch.entities, *batch.batch.relations]
        for payload in payloads:
            for candidate in payload.evidence:
                if candidate.evidence_id:
                    evidence_source[candidate.evidence_id] = batch.chunk
    evidence: dict[str, Evidence] = {
        item.evidence_id: item for item in imported.projected.evidence
    }
    failures: list[str] = []
    target_count = 0
    for item in imported.projected.evidence:
        # Extraction locators may be finer-grained than the source chunk
        # (``manual-4.2-1`` vs ``manual-4.2``); the candidate's batch names the
        # chunk whose text the quote must appear in.
        chunk = evidence_source.get(item.evidence_id) or chunks.get(
            item.chunk_id
        )
        if chunk is None:
            failures.append(
                f"{item.evidence_id}: unknown chunk {item.chunk_id!r}"
            )
            continue
        if item.quote_hash != "sha256:" + hashlib.sha256(
            item.quote.encode("utf-8")
        ).hexdigest():
            failures.append(f"{item.evidence_id}: quote_hash mismatch")
        if item.quote not in chunk.text:
            failures.append(f"{item.evidence_id}: quote is not verbatim")
        if (
            item.char_start is None
            or item.char_end is None
            or not span_matches(
                chunk.text,
                item.quote,
                item.char_start,
                item.char_end,
                chunk.char_base,
            )
        ):
            failures.append(f"{item.evidence_id}: char span does not match")
        if not item.targets:
            failures.append(f"{item.evidence_id}: no targets")
        target_count += len(item.targets)

    for fact in imported.projected.facts:
        if not fact.evidence_ids:
            failures.append(f"{fact.fact_id}: fact has no evidence")
        for evidence_id in fact.evidence_ids:
            if evidence_id not in evidence:
                failures.append(
                    f"{fact.fact_id}: unknown evidence {evidence_id!r}"
                )
    for rule in imported.projected.rules:
        if not rule.evidence_ids:
            failures.append(f"{rule.rule_uid}: rule has no evidence")
        for evidence_id in rule.evidence_ids:
            if evidence_id not in evidence:
                failures.append(
                    f"{rule.rule_uid}: unknown evidence {evidence_id!r}"
                )
    for entity in imported.projected.entities:
        for evidence_id in entity.evidence_ids:
            if evidence_id not in evidence:
                failures.append(
                    f"{entity.uid}: unknown evidence {evidence_id!r}"
                )
    if failures:
        raise VerificationError("; ".join(failures[:10]))
    return {
        "evidence": len(evidence),
        "targets": target_count,
        "facts": len(imported.projected.facts),
        "rules": len(imported.projected.rules),
    }


def active_run_dir(storage: Path, tenant_id: str) -> Path:
    pointer_path = storage / "tenants" / tenant_id / "active.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(
            f"cannot read active pointer {pointer_path}: {exc}"
        ) from exc
    run_id = pointer.get("run_id") if isinstance(pointer, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise VerificationError(f"active pointer {pointer_path} has no run_id")
    run_dir = storage / "tenants" / tenant_id / "runs" / run_id
    if not (run_dir / "bundle.json").is_file():
        raise VerificationError(f"active run bundle not found at {run_dir}")
    return run_dir


def check_query_plan(storage: Path, tenant_id: str) -> list[str]:
    run_dir = active_run_dir(storage, tenant_id)
    store = EvidenceStore(run_dir / "evidence.sqlite3")
    try:
        plan = store.explain_target_query()
    finally:
        store.close()
    if not any("idx_evidence_targets" in line for line in plan):
        raise VerificationError(
            "trace_evidence query plan does not use the target index: "
            + " | ".join(plan)
        )
    return plan


def check_storage_repository(
    pack: Path,
    storage: Path,
    tenant_id: str,
    *,
    rule_uid: str = "RULE-CAVITATION-AND",
    equipment_tag: str = "P-101A",
    equipment_uid: str = "EQ-P101A",
    top_event_uid: str = "FAULT-LOW-PRESSURE",
    draft_id: str = "TREE-P101A-LOW-PRESSURE-v1",
) -> dict[str, Any]:
    """Read through the storage-backed MCP repository without quote preload."""

    # Imported here so lightrag.fault_tree does not depend on mcp_server at
    # module import time; only verification needs the service.
    from mcp_server.service import DomainService
    from mcp_server.storage_repository import DomainStorageRepository

    repository = DomainStorageRepository.from_storage_dir(pack, storage)
    try:
        if repository.quotes_loaded != 0:
            raise VerificationError(
                "storage repository loaded quote text at startup"
            )
        service = DomainService(repository)  # type: ignore[arg-type]
        traced = service.trace_evidence(tenant_id, "rule", rule_uid)
        equipment = service.get_equipment(tenant_id, equipment_tag)
        tree = service.get_fault_tree(
            tenant_id, top_event_uid=top_event_uid
        )
        if traced["count"] < 1:
            raise VerificationError("trace_evidence returned no evidence")
        if equipment["equipment"]["uid"] != equipment_uid:
            raise VerificationError("get_equipment returned the wrong asset")
        if tree["draft"]["draft_id"] != draft_id:
            raise VerificationError("get_fault_tree returned the wrong draft")
        if repository.quotes_loaded < 1:
            raise VerificationError(
                "trace_evidence did not load a matched quote on demand"
            )
        return {
            "trace_count": traced["count"],
            "quotes_loaded": repository.quotes_loaded,
            "tree_nodes": tree["counts"]["nodes"],
            "tree_edges": tree["counts"]["edges"],
        }
    finally:
        repository.close()


__all__ = [
    "VerificationError",
    "active_run_dir",
    "check_query_plan",
    "check_storage_repository",
    "verify_evidence",
]
