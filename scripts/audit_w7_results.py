"""Audit W7 evaluation artifacts and build a low-quality review queue."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.distillation.metrics import (  # noqa: E402
    resolve_asset_id,
    source_group,
)
from scripts.evaluate_distillation import (  # noqa: E402
    _read_jsonl,
    _score_records,
    load_samples,
)

DEFAULT_REPORT = ROOT / "training" / "artifacts" / "eval" / "w7_b_test" / "report.json"
DEFAULT_SELECTION = (
    ROOT / "training" / "artifacts" / "eval" / "w7_b_test" / "selection.json"
)
DEFAULT_PREDICTIONS = (
    ROOT
    / "training"
    / "artifacts"
    / "eval"
    / "w7_b_test"
    / "B1_evidence.predictions.jsonl"
)
DEFAULT_DATA = ROOT / "training" / "data" / "w7_b" / "test.jsonl"
DEFAULT_META = ROOT / "training" / "data" / "w7_b" / "test_meta.jsonl"
DEFAULT_MANIFEST = ROOT / "training" / "data" / "w7_b" / "manifest.json"
DEFAULT_AUDIT = ROOT / "training" / "artifacts" / "w7-audit.json"
DEFAULT_QUEUE = ROOT / "training" / "artifacts" / "w7_review_queue.jsonl"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _metric(
    aggregate: Mapping[str, Any],
    *path: str,
    default: Any = None,
) -> Any:
    value: Any = aggregate
    for key in path:
        if not isinstance(value, Mapping):
            return default
        value = value.get(key)
    return default if value is None else value


def audit(
    *,
    report_path: Path,
    selection_path: Path,
    predictions_path: Path,
    data_path: Path,
    meta_path: Path,
    manifest_path: Path,
    queue_limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return an audit summary and prioritized review queue."""

    report = _read_json(report_path)
    selection = _read_json(selection_path)
    manifest = _read_json(manifest_path)
    samples = load_samples(data_path, meta_path)
    prediction_rows = _read_jsonl(predictions_path)
    records = {
        str(row.get("id") or ""): row
        for row in prediction_rows
        if isinstance(row, dict) and row.get("id")
    }
    missing = sorted({sample.sample_id for sample in samples} - set(records))
    if missing:
        raise ValueError(
            f"{len(missing)} samples have no prediction, e.g. {missing[:5]}"
        )

    assets = {
        str(asset)
        for values in (manifest.get("assets") or {}).values()
        for asset in values or ()
    }
    asset_ids = {
        sample.sample_id: resolve_asset_id(sample.sample_id, assets) or ""
        for sample in samples
    }
    scores = _score_records(samples, records, asset_ids)
    samples_by_id = {sample.sample_id: sample for sample in samples}
    queue = _build_queue(
        scores=scores,
        records=records,
        samples_by_id=samples_by_id,
        limit=queue_limit,
    )

    runs = report.get("runs") or {}
    base = (runs.get("base") or {}).get("aggregate") or {}
    student = (runs.get("B1_evidence") or {}).get("aggregate") or {}
    if not student:
        raise ValueError("report has no B1_evidence aggregate")

    checks = {
        "evaluation_complete": int(student.get("samples") or 0) == len(samples),
        "leakage_clean": not report.get("leakage"),
        "json_valid_rate_pass": float(student.get("json_valid_rate") or 0) >= 0.95,
        "contract_valid_rate_pass": float(student.get("contract_valid_rate") or 0)
        >= 0.90,
        "quote_verbatim_rate_pass": float(student.get("quote_verbatim_rate") or 0)
        >= 1.0,
        "entity_uplift_pass": _metric(student, "entity", "f1", default=0.0)
        > _metric(base, "entity", "f1", default=0.0),
        "relation_uplift_pass": _metric(student, "relation", "f1", default=0.0)
        > _metric(base, "relation", "f1", default=0.0),
        "rule_accuracy_pass": float(student.get("cause_rule_accuracy") or 0) >= 0.95,
        "gguf_artifact_present": any(
            (ROOT / "training" / "artifacts").glob("**/*.gguf")
        ),
        "a_cfdk_ablation_present": "A_cfdk" in runs,
        "b2_ablation_present": "B2_evidence_stage2" in runs,
        "runtime_integration_present": _runtime_integration_present(),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    selected = selection.get("selected") or {}
    summary = {
        "generated_by": "scripts/audit_w7_results.py",
        "report": str(report_path),
        "selection": str(selection_path),
        "samples": len(samples),
        "leakage": report.get("leakage") or [],
        "base": {
            "entity_f1": _metric(base, "entity", "f1", default=0.0),
            "relation_f1": _metric(base, "relation", "f1", default=0.0),
            "json_valid_rate": float(base.get("json_valid_rate") or 0),
            "contract_valid_rate": float(base.get("contract_valid_rate") or 0),
            "quote_verbatim_rate": float(base.get("quote_verbatim_rate") or 0),
            "negative_accuracy": base.get("negative_accuracy"),
            "avg_output_tokens": float(base.get("avg_output_tokens") or 0),
        },
        "selected": {
            "name": selected.get("name"),
            "entity_f1": _metric(student, "entity", "f1", default=0.0),
            "relation_f1": _metric(student, "relation", "f1", default=0.0),
            "json_valid_rate": float(student.get("json_valid_rate") or 0),
            "contract_valid_rate": float(student.get("contract_valid_rate") or 0),
            "quote_verbatim_rate": float(student.get("quote_verbatim_rate") or 0),
            "negative_accuracy": student.get("negative_accuracy"),
            "cause_rule_accuracy": float(student.get("cause_rule_accuracy") or 0),
            "avg_output_tokens": float(student.get("avg_output_tokens") or 0),
        },
        "absolute_uplift": {
            "entity_f1": _metric(student, "entity", "f1", default=0.0)
            - _metric(base, "entity", "f1", default=0.0),
            "relation_f1": _metric(student, "relation", "f1", default=0.0)
            - _metric(base, "relation", "f1", default=0.0),
            "json_valid_rate": float(student.get("json_valid_rate") or 0)
            - float(base.get("json_valid_rate") or 0),
            "quote_verbatim_rate": float(student.get("quote_verbatim_rate") or 0)
            - float(base.get("quote_verbatim_rate") or 0),
        },
        "checks": checks,
        "blockers": blockers,
        "core_extraction_pass": not any(
            name in blockers
            for name in (
                "evaluation_complete",
                "leakage_clean",
                "json_valid_rate_pass",
                "contract_valid_rate_pass",
                "entity_uplift_pass",
                "relation_uplift_pass",
            )
        ),
        "review_queue_count": len(queue),
        "review_priority_counts": _reason_counts(queue),
        "status": "core_pass_with_acceptance_gaps" if blockers else "pass",
    }
    return summary, queue


def _runtime_integration_present() -> bool:
    """Check that the student has a Nexent-facing execution path."""

    server = ROOT / "ingestion_mcp_server" / "server.py"
    config = ROOT / "nexent" / "config" / "agents.json"
    try:
        text = config.read_text(encoding="utf-8")
        env_example = (ROOT / "nexent" / ".env.example").read_text(encoding="utf-8")
    except OSError:
        return False
    return (
        server.is_file()
        and '"ingest_uploaded_document"' in text
        and '"industrial-fault-ingestion"' in text
        and "W7_STUDENT_ENDPOINT" in env_example
    )


def _build_queue(
    *,
    scores: Sequence[Any],
    records: Mapping[str, Mapping[str, Any]],
    samples_by_id: Mapping[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for score in scores:
        reasons: list[dict[str, Any]] = []
        if score.generation_error:
            reasons.append(_reason("generation_error", "error", 100))
        if not score.json_valid:
            reasons.append(_reason("invalid_json", "error", 95))
        if score.contract_error:
            reasons.append(_reason("invalid_contract", "error", 90))
        if score.quotes and score.quotes_verbatim < score.quotes:
            reasons.append(
                _reason(
                    "non_verbatim_quote",
                    "error",
                    85,
                    failed=score.quotes - score.quotes_verbatim,
                    total=score.quotes,
                )
            )
        if score.empty_pred and not score.empty_gold:
            reasons.append(_reason("false_negative_empty", "error", 80))
        if score.empty_gold and not score.empty_pred:
            reasons.append(_reason("negative_false_positive", "warning", 70))
        if score.reversed_relations:
            reasons.append(
                _reason(
                    "reversed_relation",
                    "error",
                    75,
                    count=score.reversed_relations,
                )
            )
        if score.malformed_entities or score.malformed_relations:
            reasons.append(
                _reason(
                    "malformed_entity_or_relation",
                    "warning",
                    65,
                    entities=score.malformed_entities,
                    relations=score.malformed_relations,
                )
            )
        if not reasons:
            continue
        sample = samples_by_id[score.sample_id]
        record = records[score.sample_id]
        priority = max(item["weight"] for item in reasons)
        rows.append(
            {
                "id": score.sample_id,
                "chunk_id": score.sample_id,
                "asset_id": score.asset_id,
                "group": score.group or source_group(score.asset_id),
                "chunk_text": sample.chunk_text,
                "gold": sample.gold,
                "model_output": record.get("prediction"),
                "raw": record.get("raw"),
                "error": reasons[0]["code"],
                "reasons": reasons,
                "priority": priority,
                "confidence": round(max(0.0, 1.0 - priority / 100.0), 4),
                "status": "pending",
            }
        )
    rows.sort(
        key=lambda row: (-int(row["priority"]), float(row["confidence"]), row["id"])
    )
    return rows[: max(1, limit)]


def _reason(
    code: str,
    severity: str,
    weight: int,
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "weight": weight,
        "details": details,
    }


def _reason_counts(queue: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in queue:
        for reason in row.get("reasons") or ():
            code = str(reason.get("code") or "unknown")
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--audit-out", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--queue-out", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--queue-limit", type=int, default=200)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary, queue = audit(
        report_path=args.report,
        selection_path=args.selection,
        predictions_path=args.predictions,
        data_path=args.data,
        meta_path=args.meta,
        manifest_path=args.manifest,
        queue_limit=args.queue_limit,
    )
    args.audit_out.parent.mkdir(parents=True, exist_ok=True)
    args.audit_out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.queue_out.parent.mkdir(parents=True, exist_ok=True)
    with args.queue_out.open("w", encoding="utf-8", newline="\n") as handle:
        for item in queue:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {args.audit_out}")
    print(f"wrote {args.queue_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["audit", "build_parser", "main"]
