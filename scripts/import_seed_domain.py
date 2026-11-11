#!/usr/bin/env python
"""Import the curated seed documents and verify the W3 acceptance criteria.

The script is deterministic and offline: it projects the extraction fixtures
under ``domain_packs/industrial_fault/seeds/extraction`` into entities, facts,
rules, evidence, and a fault-tree draft, writes the SQLite evidence store plus
the auditable/runtime bundles, writes the canonical graph through
NetworkXStorage, and then verifies that a second import changes no counts.

``--verify`` additionally proves:

* the rebuilt tree equals ``seeds/demo_bundle.json`` node-for-node;
* every fact and rule names at least one verbatim evidence row;
* evidence rows and graph edges survive a repeated import without growth;
* the storage-backed MCP repository loads no quote text at startup;
* ``trace_evidence`` reads through the target index.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lightrag.fault_tree.contracts import Evidence  # noqa: E402
from lightrag.fault_tree.evidence_store import EvidenceStore  # noqa: E402
from lightrag.fault_tree.importer import (  # noqa: E402
    ImportValidationError,
    ImportedDomain,
    build_imported_domain,
    compare_tree,
    graph_projection_for,
    write_bundle_outputs,
    write_graph,
)
from lightrag.fault_tree.projection import (  # noqa: E402
    SourceChunk,
    span_matches,
)
from mcp_server.service import DomainService  # noqa: E402
from mcp_server.storage_repository import (  # noqa: E402
    DomainStorageRepository,
)

DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"
DEFAULT_EXTRACTION = DEFAULT_PACK / "seeds" / "extraction"
DEFAULT_GOLD = DEFAULT_PACK / "seeds" / "demo_bundle.json"
DEFAULT_STORAGE = DEFAULT_PACK / "storage"


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


def _active_run_dir(storage: Path, tenant_id: str) -> Path:
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


def _store_counts(storage: Path, tenant_id: str) -> dict[str, int]:
    run_dir = _active_run_dir(storage, tenant_id)
    store = EvidenceStore(run_dir / "evidence.sqlite3")
    try:
        return {
            "evidence_rows": store.count(tenant_id),
            "target_rows": store.target_count(tenant_id),
        }
    finally:
        store.close()


def _graph_expected(imported: ImportedDomain) -> dict[str, int]:
    projection = graph_projection_for(imported)
    return {"nodes": len(projection.nodes), "edges": len(projection.edges)}


def _check_query_plan(storage: Path, tenant_id: str) -> list[str]:
    run_dir = _active_run_dir(storage, tenant_id)
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


def _check_storage_repository(
    pack: Path,
    storage: Path,
    tenant_id: str,
) -> dict[str, Any]:
    repository = DomainStorageRepository.from_storage_dir(pack, storage)
    try:
        if repository.quotes_loaded != 0:
            raise VerificationError(
                "storage repository loaded quote text at startup"
            )
        service = DomainService(repository)  # type: ignore[arg-type]
        traced = service.trace_evidence(
            tenant_id, "rule", "RULE-CAVITATION-AND"
        )
        equipment = service.get_equipment(tenant_id, "P-101A")
        tree = service.get_fault_tree(
            tenant_id, top_event_uid="FAULT-LOW-PRESSURE"
        )
        if traced["count"] < 1:
            raise VerificationError("trace_evidence returned no evidence")
        if equipment["equipment"]["uid"] != "EQ-P101A":
            raise VerificationError("get_equipment returned the wrong asset")
        if tree["draft"]["draft_id"] != "TREE-P101A-LOW-PRESSURE-v1":
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


def run(
    *,
    pack: Path,
    extraction: Path,
    gold: Path,
    storage: Path,
    verify: bool,
    tenant_id: str | None = None,
) -> int:
    if not gold.is_file():
        raise VerificationError(f"gold bundle not found at {gold}")
    imported = build_imported_domain(pack, extraction, tenant_id=tenant_id)
    if verify:
        issues = compare_tree(imported.tree, gold)
        if issues:
            raise VerificationError(
                "rebuilt tree differs from gold: "
                + "; ".join(f"{item.code}: {item.message}" for item in issues)
            )
        evidence_counts = verify_evidence(imported)

    first = write_bundle_outputs(imported, storage, prune_tenant=True)
    first_counts = _store_counts(storage, imported.bundle.tenant_id)
    second = write_bundle_outputs(imported, storage)
    second_counts = _store_counts(storage, imported.bundle.tenant_id)
    if first_counts != second_counts:
        raise VerificationError(
            f"repeated import changed evidence counts: "
            f"{first_counts} -> {second_counts}"
        )
    if first["counts"] != second["counts"]:
        raise VerificationError("repeated import changed bundle counts")
    if first["run_id"] != second["run_id"]:
        raise VerificationError(
            f"repeated import created a new run: "
            f"{first['run_id']} -> {second['run_id']}"
        )
    runs_dir = (
        storage
        / "tenants"
        / imported.bundle.tenant_id
        / "runs"
    )
    run_dirs = sorted(entry.name for entry in runs_dir.iterdir() if entry.is_dir())
    if run_dirs != [first["run_id"]]:
        raise VerificationError(
            f"expected one run directory, found {run_dirs}"
        )

    expected_graph = _graph_expected(imported)
    graph_first = asyncio.run(write_graph(imported, storage))
    graph_second = asyncio.run(write_graph(imported, storage))
    if graph_first != graph_second:
        raise VerificationError(
            f"repeated import changed graph counts: "
            f"{graph_first} -> {graph_second}"
        )
    if graph_first != expected_graph:
        raise VerificationError(
            f"graph counts {graph_first} != projection {expected_graph}"
        )

    result: dict[str, Any] = {
        "tenant": imported.bundle.tenant_id,
        "run_id": first["run_id"],
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
        "evidence_rows": first_counts["evidence_rows"],
        "target_rows": first_counts["target_rows"],
        "graph": graph_first,
        "warnings": imported.projected.warnings,
    }
    if verify:
        result["evidence_checks"] = evidence_counts
        result["query_plan"] = _check_query_plan(
            storage, imported.bundle.tenant_id
        )
        result["storage_repository"] = _check_storage_repository(
            pack, storage, imported.bundle.tenant_id
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
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument("--extraction", default=str(DEFAULT_EXTRACTION))
    parser.add_argument("--gold", default=str(DEFAULT_GOLD))
    parser.add_argument("--storage", default=str(DEFAULT_STORAGE))
    parser.add_argument(
        "--tenant",
        default=None,
        help=(
            "Import under this tenant ID instead of the one in "
            "seeds/extraction/context.json. Use it to serve a deployment "
            "tenant such as a Nexent tenant UUID."
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check gold-tree equality, evidence quotes, and MCP reads.",
    )
    args = parser.parse_args(argv)
    try:
        return run(
            pack=Path(args.pack).expanduser().resolve(),
            extraction=Path(args.extraction).expanduser().resolve(),
            gold=Path(args.gold).expanduser().resolve(),
            storage=Path(args.storage).expanduser().resolve(),
            verify=args.verify,
            tenant_id=args.tenant,
        )
    except (ImportValidationError, VerificationError, ValueError) as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
