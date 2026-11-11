#!/usr/bin/env python
"""Benchmark the storage-backed evidence path at 100k records.

The benchmark builds a disposable workspace under ``temp/``, writes 100k
evidence rows (180-character quotes, two targets each), and measures:

* Python heap growth while the MCP storage repository starts up;
* quote rows loaded during startup (must stay zero);
* the ``EXPLAIN QUERY PLAN`` used by ``trace_evidence``;
* lookup latency for a one-hit target versus a target with many hits.

The quote payload dominates the database; startup only reads IDs and the
runtime bundle, so heap use must stay independent of the evidence count.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import shutil
import statistics
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lightrag.fault_tree.contracts import (  # noqa: E402
    DomainBundle,
    Equipment,
    Evidence,
    EvidenceTarget,
)
from lightrag.fault_tree.evidence_store import EvidenceStore  # noqa: E402
from mcp_server.service import DomainService  # noqa: E402
from mcp_server.storage_repository import (  # noqa: E402
    DomainStorageRepository,
)

DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"
DEFAULT_STORAGE = REPO_ROOT / "temp" / "evidence_benchmark"
TENANT = "tenant-benchmark"
MAX_STARTUP_HEAP_MIB = 256.0
HOT_TARGET = "EQ-HOT"
HOT_HITS = 100


def _quote(index: int) -> str:
    seed = f"quote-{index:08d}-"
    return (seed + "压" * 180)[:180]


def _evidence(index: int) -> Evidence:
    quote = _quote(index)
    targets = [
        EvidenceTarget(
            target_type="entity",
            target_id="EQ-BENCH",
            role="mentions",
        ),
        EvidenceTarget(
            target_type="fact",
            target_id=f"FACT-{index % 200:04d}",
            role="supports",
        ),
    ]
    if index == 0:
        targets.append(
            EvidenceTarget(
                target_type="entity", target_id="EQ-ONE-HIT", role="mentions"
            )
        )
    if index < HOT_HITS:
        targets.append(
            EvidenceTarget(
                target_type="entity", target_id=HOT_TARGET, role="mentions"
            )
        )
    return Evidence(
        evidence_id=f"EV-{index:08d}",
        tenant_id=TENANT,
        asset_id="ASSET-BENCH",
        chunk_id=f"CHUNK-{index:08d}",
        char_start=0,
        char_end=len(quote),
        quote=quote,
        quote_hash="sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        modality="text",
        targets=targets,
        extractor="benchmark",
        extraction_run_id="benchmark-run",
        review_status="approved",
    )


def _equipment(uid: str, tag: str, now: datetime) -> Equipment:
    return Equipment(
        uid=uid,
        tenant_id=TENANT,
        canonical_name=f"Benchmark pump {tag}",
        ontology_version="industrial-fault-v0.1",
        schema_version="industrial-fault-schema-v0.1",
        review_status="approved",
        created_at=now,
        updated_at=now,
        created_by="benchmark",
        equipment_tag=tag,
        equipment_name=f"Benchmark pump {tag}",
        equipment_type="centrifugal pump",
    )


def _build_storage(storage: Path, records: int) -> None:
    if storage.exists():
        shutil.rmtree(storage)
    tenant_dir = storage / "tenants" / TENANT
    tenant_dir.mkdir(parents=True)
    now = datetime(2026, 11, 7, tzinfo=UTC)
    entities = [
        _equipment("EQ-BENCH", "P-BENCH", now),
        _equipment("EQ-ONE-HIT", "P-ONE", now),
        _equipment("EQ-HOT", "P-HOT", now),
    ]
    bundle = DomainBundle(
        bundle_version="benchmark-v1",
        ontology_version="industrial-fault-v0.1",
        schema_version="industrial-fault-schema-v0.1",
        tenant_id=TENANT,
        entities=entities,
        evidence=[],
    )
    (tenant_dir / "bundle.json").write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    store = EvidenceStore(tenant_dir / "evidence.sqlite3")
    try:
        chunk_size = 2000
        for start in range(0, records, chunk_size):
            store.upsert_many(
                _evidence(index)
                for index in range(start, min(start + chunk_size, records))
            )
    finally:
        store.close()


def _median_us(function, repeats: int = 9) -> float:
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        function()
        samples.append((time.perf_counter() - started) * 1_000_000)
    return statistics.median(samples)


def run(*, storage: Path, records: int) -> int:
    started = time.perf_counter()
    _build_storage(storage, records)
    build_seconds = time.perf_counter() - started

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    startup_started = time.perf_counter()
    repository = DomainStorageRepository.from_storage_dir(DEFAULT_PACK, storage)
    startup_seconds = time.perf_counter() - startup_started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    heap_delta_mib = (current - baseline) / (1024 * 1024)
    startup_quotes = repository.quotes_loaded
    try:
        if heap_delta_mib >= MAX_STARTUP_HEAP_MIB:
            raise SystemExit(
                f"startup heap delta {heap_delta_mib:.2f} MiB exceeds "
                f"{MAX_STARTUP_HEAP_MIB:.0f} MiB"
            )
        if startup_quotes != 0:
            raise SystemExit(f"startup loaded {startup_quotes} quotes")
        store = EvidenceStore(
            storage / "tenants" / TENANT / "evidence.sqlite3"
        )
        try:
            plan = store.explain_target_query()

            def one_hit() -> tuple[str, ...]:
                return store.ids_for_target(TENANT, "entity", "EQ-ONE-HIT")

            def hot_hit() -> tuple[str, ...]:
                return store.ids_for_target(TENANT, "entity", HOT_TARGET)

            one_hit_ids = one_hit()
            hot_hit_ids = hot_hit()
            if len(one_hit_ids) != 1 or len(hot_hit_ids) != HOT_HITS:
                raise SystemExit("target index returned unexpected hit counts")
            store_times = {
                "one_hit_us": _median_us(one_hit),
                "hot_hit_us": _median_us(hot_hit),
            }
        finally:
            store.close()

        service = DomainService(repository)  # type: ignore[arg-type]
        service_times = {
            "one_hit_us": _median_us(
                lambda: service.trace_evidence(
                    TENANT, "entity", "EQ-ONE-HIT"
                ),
                repeats=5,
            ),
            "hot_hit_us": _median_us(
                lambda: service.trace_evidence(TENANT, "entity", HOT_TARGET),
                repeats=5,
            ),
        }
        if not any("idx_evidence_targets" in line for line in plan):
            raise SystemExit(
                "target lookup does not use an evidence_targets index: "
                + " | ".join(plan)
            )
        result: dict[str, Any] = {
            "records": records,
            "quote_chars": len(_quote(0)),
            "targets_per_record": 2,
            "build_seconds": round(build_seconds, 2),
            "startup_seconds": round(startup_seconds, 4),
            "startup_heap_delta_mib": round(heap_delta_mib, 2),
            "startup_peak_mib": round(peak / (1024 * 1024), 2),
            "quotes_loaded_at_startup": startup_quotes,
            "query_plan": plan,
            "store_lookup": store_times,
            "trace_evidence": service_times,
            "hot_hits": HOT_HITS,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if store_times["hot_hit_us"] < store_times["one_hit_us"]:
            raise SystemExit(
                "hot-target lookup was faster than one-hit lookup; "
                "the measurement is not meaningful"
            )
        return 0
    finally:
        repository.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--storage", default=str(DEFAULT_STORAGE))
    args = parser.parse_args(argv)
    if args.records < 1:
        parser.error("--records must be positive")
    return run(
        storage=Path(args.storage).expanduser().resolve(),
        records=args.records,
    )


if __name__ == "__main__":
    raise SystemExit(main())
