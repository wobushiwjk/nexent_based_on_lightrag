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
import json
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
    build_imported_domain,
    compare_tree,
    graph_projection_for,
    write_bundle_outputs,
    write_graph,
)
from lightrag.fault_tree.verification import (  # noqa: E402
    VerificationError,
    active_run_dir,
    check_query_plan,
    check_storage_repository,
    verify_evidence,
)

DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"
DEFAULT_EXTRACTION = DEFAULT_PACK / "seeds" / "extraction"
DEFAULT_GOLD = DEFAULT_PACK / "seeds" / "demo_bundle.json"
DEFAULT_STORAGE = DEFAULT_PACK / "storage"


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


def _graph_expected(imported: ImportedDomain) -> dict[str, int]:
    projection = graph_projection_for(imported)
    return {"nodes": len(projection.nodes), "edges": len(projection.edges)}


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
        result["query_plan"] = check_query_plan(
            storage, imported.bundle.tenant_id
        )
        result["storage_repository"] = check_storage_repository(
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
