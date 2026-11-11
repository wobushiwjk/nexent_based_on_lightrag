"""Tests for the SQLite evidence store and its target index."""

from __future__ import annotations

import hashlib

from lightrag.fault_tree.contracts import Evidence, EvidenceTarget
from lightrag.fault_tree.evidence_store import EvidenceStore

TENANT = "tenant-store"


def _evidence(quote: str = "出口压力不足，过滤器堵塞。") -> Evidence:
    return Evidence(
        evidence_id="EV-1",
        tenant_id=TENANT,
        asset_id="FILE-MANUAL",
        chunk_id="CHUNK-1",
        page=12,
        section_path="4.2",
        char_start=0,
        char_end=len(quote),
        quote=quote,
        quote_hash="sha256:" + hashlib.sha256(quote.encode()).hexdigest(),
        modality="text",
        targets=[
            EvidenceTarget(
                target_type="entity", target_id="EQ-P101A", role="mentions"
            ),
            EvidenceTarget(
                target_type="rule", target_id="RULE-1", role="supports"
            ),
        ],
        extractor="seed",
        extraction_run_id="run-1",
        review_status="approved",
    )


def test_upsert_and_lazy_get(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    try:
        item = _evidence()
        store.upsert(item)
        assert store.quotes_loaded == 0
        assert store.count(TENANT) == 1
        assert store.target_count(TENANT) == 2
        assert store.exists(TENANT, "EV-1")
        assert not store.exists(TENANT, "EV-MISSING")
        assert store.missing_ids(TENANT, ["EV-1", "EV-MISSING"]) == [
            "EV-MISSING"
        ]
        assert store.ids_for_target(TENANT, "entity", "EQ-P101A") == ("EV-1",)

        loaded = store.get(TENANT, "EV-1")
        assert loaded == item
        assert store.quotes_loaded == 1
        assert store.get(TENANT, "EV-MISSING") is None
    finally:
        store.close()


def test_upsert_replaces_targets_without_growth(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    try:
        store.upsert(_evidence())
        updated = _evidence("过滤器堵塞导致出口压力不足。").model_copy(
            update={
                "targets": [
                    EvidenceTarget(
                        target_type="rule",
                        target_id="RULE-1",
                        role="supports",
                    )
                ]
            }
        )
        store.upsert(updated)
        store.upsert(updated)

        assert store.count(TENANT) == 1
        assert store.target_count(TENANT) == 1
        assert store.ids_for_target(TENANT, "entity", "EQ-P101A") == ()
        assert store.ids_for_target(TENANT, "rule", "RULE-1") == ("EV-1",)
        assert store.get(TENANT, "EV-1") == updated
    finally:
        store.close()


def test_target_lookup_uses_the_index(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    try:
        plan = store.explain_target_query()
        assert any("idx_evidence_targets" in line for line in plan), plan
    finally:
        store.close()


def test_tenant_deletion_cascades_targets(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    try:
        store.upsert(_evidence())
        assert store.delete_tenant(TENANT) == 1
        assert store.count(TENANT) == 0
        assert store.target_count(TENANT) == 0
        assert list(store.iter_ids(TENANT)) == []
    finally:
        store.close()


def test_evidence_rows_round_trip_after_reopen(tmp_path) -> None:
    path = tmp_path / "evidence.sqlite3"
    first = EvidenceStore(path)
    try:
        first.upsert(_evidence())
    finally:
        first.close()

    second = EvidenceStore(path)
    try:
        assert second.quotes_loaded == 0
        rows = list(second.iter_evidence(TENANT))
        assert len(rows) == 1
        assert rows[0].quote == "出口压力不足，过滤器堵塞。"
        assert second.quotes_loaded == 1
    finally:
        second.close()
