"""Run deterministic industrial-domain retrieval, evidence, tree, and E2E checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.importer import (  # noqa: E402
    build_imported_domain,
    write_bundle_outputs,
)
from mcp_server.errors import DomainError  # noqa: E402
from mcp_server.service import DomainService  # noqa: E402
from mcp_server.storage_repository import DomainStorageRepository  # noqa: E402
from skills.fault_diagnosis import handler as diagnosis_skill  # noqa: E402
from skills.runtime import quote_digest  # noqa: E402

PACK = ROOT / "domain_packs" / "industrial_fault"
EXTRACTION = PACK / "seeds" / "extraction"
DEFAULT_STORAGE = PACK / "storage"
DEFAULT_OUT = ROOT / "eval" / "reports" / "industrial-domain-eval.json"
EXPECTED_RETRIEVAL_FAULTS = (
    "FAULT-LOW-PRESSURE",
    "FAULT-CAVITATION",
)
TREE_CAUSE_FAULTS = (
    "FAULT-FILTER-BLOCKED",
    "FAULT-CAVITATION",
)
EXPECTED_TOP_EVENT = "FAULT-LOW-PRESSURE"


class ServiceToolClient:
    """Adapt the synchronous DomainService to the Skill ToolClient protocol."""

    def __init__(self, service: DomainService, tenant_id: str) -> None:
        self.service = service
        self.tenant_id = tenant_id

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            if name == "get_ontology":
                response = self.service.success(
                    self.service.get_ontology(), self.tenant_id
                )
            elif name == "get_equipment":
                response = self.service.success(
                    self.service.get_equipment(
                        str(arguments.get("tenant_id") or self.tenant_id),
                        str(arguments.get("equipment_ref") or ""),
                    ),
                    str(arguments.get("tenant_id") or self.tenant_id),
                )
            elif name == "search_faults":
                response = self.service.success(
                    self.service.search_faults(
                        str(arguments.get("tenant_id") or self.tenant_id),
                        str(arguments.get("query") or ""),
                        int(arguments.get("limit") or 10),
                    ),
                    str(arguments.get("tenant_id") or self.tenant_id),
                )
            elif name == "get_fault_tree":
                response = self.service.success(
                    self.service.get_fault_tree(
                        str(arguments.get("tenant_id") or self.tenant_id),
                        draft_id=arguments.get("draft_id"),
                        top_event_uid=arguments.get("top_event_uid"),
                    ),
                    str(arguments.get("tenant_id") or self.tenant_id),
                )
            elif name == "trace_evidence":
                response = self.service.success(
                    self.service.trace_evidence(
                        str(arguments.get("tenant_id") or self.tenant_id),
                        str(arguments.get("target_type") or ""),
                        str(arguments.get("target_id") or ""),
                    ),
                    str(arguments.get("tenant_id") or self.tenant_id),
                )
            elif name == "validate_fault_tree":
                response = self.service.success(
                    self.service.validate_fault_tree(
                        str(arguments.get("tenant_id") or self.tenant_id),
                        draft_id=arguments.get("draft_id"),
                        draft=arguments.get("draft"),
                    ),
                    str(arguments.get("tenant_id") or self.tenant_id),
                )
            else:
                raise DomainError(
                    "unsupported",
                    f"unknown tool {name!r}",
                    {"tool": name},
                )
        except DomainError as exc:
            response = self.service.failure(exc, self.tenant_id)
        return response.model_dump(mode="json")


def _edge_signature(data: dict[str, Any]) -> list[list[str]]:
    draft = data.get("draft")
    draft = draft if isinstance(draft, dict) else {}
    return sorted(
        [
            str(edge.get("source_uid") or ""),
            str(edge.get("relation_code") or ""),
            str(edge.get("target_uid") or ""),
        ]
        for edge in draft.get("edges") or []
        if isinstance(edge, dict)
    )


def _norm_name(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _expected_fault_uids(results: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    expected_names = {
        _norm_name("出口压力低"): "FAULT-LOW-PRESSURE",
        _norm_name("入口过滤器堵塞"): "FAULT-FILTER-BLOCKED",
        _norm_name("叶轮汽蚀"): "FAULT-CAVITATION",
    }
    for item in results:
        uid = str(item.get("uid") or item.get("fault_uid") or "")
        if uid in EXPECTED_RETRIEVAL_FAULTS:
            found.add(uid)
        normalized = _norm_name(item.get("canonical_name") or item.get("fault_name"))
        if normalized in expected_names:
            found.add(expected_names[normalized])
    return found


def _evidence_metrics(service: DomainService, tenant_id: str) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    covered: set[str] = set()
    for fault_uid in TREE_CAUSE_FAULTS:
        data = service.trace_evidence(tenant_id, "entity", fault_uid)
        items = [item for item in data.get("evidence") or [] if isinstance(item, dict)]
        if items:
            covered.add(fault_uid)
        evidence.extend(items)
    unique = {str(item.get("evidence_id") or ""): item for item in evidence}
    total = len(unique)
    valid_hash = sum(
        1
        for item in unique.values()
        if item.get("quote_hash") == quote_digest(str(item.get("quote") or ""))
    )
    valid_span = sum(
        1
        for item in unique.values()
        if item.get("char_start") is not None
        and item.get("char_end") is not None
        and int(item["char_end"]) - int(item["char_start"])
        == len(str(item.get("quote") or ""))
    )
    return {
        "expected_faults": len(TREE_CAUSE_FAULTS),
        "covered_faults": len(covered),
        "coverage": len(covered) / len(TREE_CAUSE_FAULTS),
        "quotes": total,
        "hash_valid_rate": valid_hash / total if total else 0.0,
        "span_valid_rate": valid_span / total if total else 0.0,
    }


async def _full_graph(service: DomainService, tenant_id: str) -> dict[str, Any]:
    client = ServiceToolClient(service, tenant_id)
    started = time.perf_counter()
    result = await diagnosis_skill.run(
        {
            "tenant_id": tenant_id,
            "equipment_ref": "P-101A",
            "symptom": "出口压力不足，可能的原因有哪些",
            "max_candidates": 5,
            "include_tree": True,
        },
        client,
    )
    elapsed = (time.perf_counter() - started) * 1000
    data = result.data or {}
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    if not isinstance(data, dict):
        data = {}
    candidates = [
        item for item in data.get("candidates") or [] if isinstance(item, dict)
    ]
    tree = data.get("fault_tree") if isinstance(data.get("fault_tree"), dict) else {}
    evidence = _evidence_metrics(service, tenant_id)
    return {
        "status": result.status,
        "ok": result.ok,
        "latency_ms": round(elapsed, 3),
        "candidate_uids": sorted(
            str(item.get("fault_uid") or "") for item in candidates
        ),
        "retrieval_hit": expected_hit(candidates),
        "evidence": evidence,
        "tree_valid": bool(tree.get("validation") and tree["validation"].get("valid")),
        "tree_top_event": tree.get("top_event_uid"),
        "tree_nodes": int((tree.get("counts") or {}).get("nodes") or 0),
        "tree_edges": int((tree.get("counts") or {}).get("edges") or 0),
    }


def expected_hit(results: list[dict[str, Any]]) -> float:
    found = _expected_fault_uids(results)
    return len(found) / len(EXPECTED_RETRIEVAL_FAULTS)


def _lexical_only(service: DomainService, tenant_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    data = service.search_faults(
        tenant_id,
        "出口压力不足，可能的原因有哪些",
        limit=5,
    )
    elapsed = (time.perf_counter() - started) * 1000
    results = [item for item in data.get("results") or [] if isinstance(item, dict)]
    return {
        "status": "retrieved",
        "ok": True,
        "latency_ms": round(elapsed, 3),
        "candidate_uids": sorted(str(item.get("uid") or "") for item in results),
        "retrieval_hit": expected_hit(results),
        "evidence": {
            "covered_faults": 0,
            "coverage": 0.0,
            "quotes": 0,
            "hash_valid_rate": 0.0,
            "span_valid_rate": 0.0,
        },
        "tree_valid": False,
        "tree_top_event": None,
        "tree_nodes": 0,
        "tree_edges": 0,
    }


def evaluate(
    *,
    pack_dir: Path = PACK,
    extraction_dir: Path = EXTRACTION,
    storage_dir: Path | None = DEFAULT_STORAGE,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Build a fresh run when needed and evaluate lexicographic vs graph paths."""

    temporary = None
    if storage_dir is None:
        import tempfile

        temporary = tempfile.TemporaryDirectory(prefix="w9-domain-eval-")
        storage = Path(temporary.name) / "storage"
        imported = build_imported_domain(pack_dir, extraction_dir)
        write_bundle_outputs(imported, storage, prune_tenant=True)
        selected_tenant = imported.bundle.tenant_id
    else:
        storage = storage_dir.resolve()
        selected_tenant = tenant_id

    repository = DomainStorageRepository.from_storage_dir(
        pack_dir,
        storage,
        default_tenant=selected_tenant,
    )
    try:
        service = DomainService(repository)
        effective_tenant = selected_tenant or repository.default_tenant
        if not effective_tenant:
            raise ValueError(
                "evaluation requires a tenant id or a single-tenant storage"
            )

        tree = service.get_fault_tree(
            effective_tenant, top_event_uid=EXPECTED_TOP_EVENT
        )
        validation = service.validate_fault_tree(
            effective_tenant,
            draft_id=str((tree.get("draft") or {}).get("draft_id") or ""),
        )
        full = asyncio.run(_full_graph(service, effective_tenant))
        baseline = _lexical_only(service, effective_tenant)
        full["tree_structure_match"] = _edge_signature(tree) == _demo_edge_signature()
        tree_fault_uids = {
            str(item.get("uid") or "")
            for item in tree.get("faults") or []
            if isinstance(item, dict)
        }
        full["tree_cause_coverage"] = len(
            tree_fault_uids & set(TREE_CAUSE_FAULTS)
        ) / len(TREE_CAUSE_FAULTS)
        baseline["tree_structure_match"] = False
        baseline["tree_cause_coverage"] = 0.0
        report = {
            "generated_by": "scripts/evaluate_domain.py",
            "tenant_id": effective_tenant,
            "question": "P-101A 出口压力不足，可能的原因有哪些？",
            "expected_retrieval_faults": list(EXPECTED_RETRIEVAL_FAULTS),
            "expected_tree_cause_faults": list(TREE_CAUSE_FAULTS),
            "expected_top_event": EXPECTED_TOP_EVENT,
            "tree_validation": validation,
            "variants": {
                "lexical_no_graph": baseline,
                "graph_evidence_full": full,
            },
            "checks": {
                "tree_schema_valid": bool(validation.get("valid")),
                "full_graph_retrieval": full["retrieval_hit"] == 1.0,
                "full_graph_evidence": full["evidence"]["coverage"] == 1.0
                and full["evidence"]["hash_valid_rate"] == 1.0
                and full["evidence"]["span_valid_rate"] == 1.0,
                "full_graph_tree_causes": full["tree_cause_coverage"] == 1.0,
                "full_graph_end_to_end": full["ok"]
                and full["status"] == "diagnosed"
                and full["retrieval_hit"] == 1.0,
                "graph_improves_evidence": full["evidence"]["coverage"]
                > baseline["evidence"]["coverage"],
                "graph_improves_tree": full["tree_structure_match"]
                and not baseline["tree_structure_match"],
            },
        }
        report["ok"] = all(report["checks"].values())
        return report
    finally:
        repository.close()
        if temporary is not None:
            temporary.cleanup()


def _demo_edge_signature() -> list[list[str]]:
    bundle = json.loads(
        (PACK / "seeds" / "demo_bundle.json").read_text(encoding="utf-8")
    )
    tree = bundle.get("tree") if isinstance(bundle.get("tree"), dict) else {}
    return sorted(
        [
            str(edge.get("source_uid") or ""),
            str(edge.get("relation_code") or ""),
            str(edge.get("target_uid") or ""),
        ]
        for edge in tree.get("edges") or []
        if isinstance(edge, dict)
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-dir", type=Path, default=None)
    parser.add_argument("--tenant-id")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = evaluate(
        storage_dir=args.storage_dir,
        tenant_id=args.tenant_id,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"wrote {args.out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ServiceToolClient", "build_parser", "evaluate", "main"]
