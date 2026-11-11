"""End-to-end tests for the deterministic seed-document import."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from lightrag.fault_tree.evidence_store import EvidenceStore
from lightrag.fault_tree.importer import (
    build_imported_domain,
    compare_tree,
    graph_projection_for,
    write_bundle_outputs,
    write_graph,
)

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "domain_packs" / "industrial_fault"
EXTRACTION = PACK / "seeds" / "extraction"
GOLD = PACK / "seeds" / "demo_bundle.json"


@pytest.fixture()
def imported():
    return build_imported_domain(PACK, EXTRACTION)


def test_rebuilt_tree_matches_gold(imported) -> None:
    assert compare_tree(imported.tree, GOLD) == []
    assert imported.projected.warnings == []


def test_bundle_outputs_keep_quotes_out_of_the_runtime_bundle(
    imported, tmp_path
) -> None:
    manifest = write_bundle_outputs(imported, tmp_path, prune_tenant=True)
    tenant_dir = tmp_path / "tenants" / imported.bundle.tenant_id
    run_dir = Path(manifest["run_dir"])

    full = json.loads(
        (run_dir / "bundle.full.json").read_text(encoding="utf-8")
    )
    runtime = json.loads(
        (run_dir / "bundle.json").read_text(encoding="utf-8")
    )
    assert len(full["evidence"]) == len(imported.bundle.evidence)
    assert runtime["evidence"] == []
    assert manifest["counts"]["evidence"] == len(imported.bundle.evidence)

    active = json.loads(
        (tenant_dir / "active.json").read_text(encoding="utf-8")
    )
    assert active["run_id"] == manifest["run_id"]
    assert active["runtime_bundle"] == f"runs/{manifest['run_id']}/bundle.json"
    assert not (tenant_dir / "bundle.json").exists()

    store = EvidenceStore(run_dir / "evidence.sqlite3")
    try:
        assert store.count(imported.bundle.tenant_id) == len(
            imported.bundle.evidence
        )
        assert store.quotes_loaded == 0
    finally:
        store.close()


def test_repeated_import_does_not_duplicate_records(imported, tmp_path) -> None:
    tenant = imported.bundle.tenant_id
    first_manifest = write_bundle_outputs(imported, tmp_path, prune_tenant=True)
    first = _counts(tmp_path, tenant)
    second_manifest = write_bundle_outputs(imported, tmp_path)
    second = _counts(tmp_path, tenant)
    assert first == second
    assert first_manifest["run_id"] == second_manifest["run_id"]

    run_dir = (
        tmp_path / "tenants" / tenant / "runs" / first_manifest["run_id"]
    )
    assert sorted(entry.name for entry in run_dir.parent.iterdir()) == [
        first_manifest["run_id"]
    ]

    expected_evidence = len(imported.bundle.evidence)
    expected_targets = sum(
        len(item.targets) for item in imported.bundle.evidence
    )
    assert second["evidence_rows"] == expected_evidence
    assert second["target_rows"] == expected_targets


def test_new_content_switches_the_active_run_and_keeps_history(
    imported, tmp_path
) -> None:
    tenant = imported.bundle.tenant_id
    first = write_bundle_outputs(imported, tmp_path, prune_tenant=True)
    changed = replace(
        imported,
        bundle=imported.bundle.model_copy(
            update={"bundle_version": "seed-import-v2"}
        ),
    )
    second = write_bundle_outputs(changed, tmp_path)

    assert second["run_id"] != first["run_id"]
    active = json.loads(
        (
            tmp_path / "tenants" / tenant / "active.json"
        ).read_text(encoding="utf-8")
    )
    assert active["run_id"] == second["run_id"]
    runs = tmp_path / "tenants" / tenant / "runs"
    assert sorted(entry.name for entry in runs.iterdir()) == sorted(
        [first["run_id"], second["run_id"]]
    )


def test_prune_tenant_drops_superseded_runs(imported, tmp_path) -> None:
    tenant = imported.bundle.tenant_id
    changed = replace(
        imported,
        bundle=imported.bundle.model_copy(
            update={"bundle_version": "seed-import-v2"}
        ),
    )
    write_bundle_outputs(imported, tmp_path, prune_tenant=True)
    second = write_bundle_outputs(changed, tmp_path)
    write_bundle_outputs(changed, tmp_path, prune_tenant=True)

    runs = tmp_path / "tenants" / tenant / "runs"
    assert sorted(entry.name for entry in runs.iterdir()) == [second["run_id"]]


def test_graph_projection_matches_written_graph(imported, tmp_path) -> None:
    projection = graph_projection_for(imported)
    expected = {
        "nodes": len(projection.nodes),
        "edges": len(projection.edges),
    }
    manifest = write_bundle_outputs(imported, tmp_path)
    first = asyncio.run(write_graph(imported, tmp_path))
    second = asyncio.run(write_graph(imported, tmp_path))
    assert first == expected
    assert second == expected
    assert (Path(manifest["run_dir"]) / "graph").is_dir()


def _active_run_dir(storage: Path, tenant: str) -> Path:
    active = json.loads(
        (
            storage / "tenants" / tenant / "active.json"
        ).read_text(encoding="utf-8")
    )
    return storage / "tenants" / tenant / "runs" / active["run_id"]


def _counts(storage: Path, tenant: str) -> dict[str, int]:
    store = EvidenceStore(_active_run_dir(storage, tenant) / "evidence.sqlite3")
    try:
        return {
            "evidence_rows": store.count(tenant),
            "target_rows": store.target_count(tenant),
        }
    finally:
        store.close()
