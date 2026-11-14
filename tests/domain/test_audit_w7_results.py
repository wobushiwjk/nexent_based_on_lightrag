"""Tests for the W7 result audit and review queue."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_w7_results import audit


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_audit_builds_review_queue_and_reports_acceptance_gaps(
    tmp_path: Path,
) -> None:
    data = tmp_path / "test.jsonl"
    meta = tmp_path / "test_meta.jsonl"
    predictions = tmp_path / "student.predictions.jsonl"
    report = tmp_path / "report.json"
    selection = tmp_path / "selection.json"
    manifest = tmp_path / "manifest.json"

    _write_jsonl(
        data,
        [
            {
                "id": "asset-doc-0001",
                "conversations": [
                    {"from": "human", "value": "入口过滤器堵塞导致压力下降。"}
                ],
            }
        ],
    )
    _write_jsonl(
        meta,
        [
            {
                "id": "asset-doc-0001",
                "chunk_text": "入口过滤器堵塞导致压力下降。",
                "gold": {
                    "entities": [
                        {
                            "local_id": "blocked",
                            "class": "FaultEvent",
                            "name": "入口过滤器堵塞",
                        },
                        {
                            "local_id": "low",
                            "class": "FaultEvent",
                            "name": "压力下降",
                        },
                    ],
                    "relations": [
                        {
                            "relation_code": "CAUSES",
                            "source_local_id": "blocked",
                            "target_local_id": "low",
                            "evidence": [{"quote": "入口过滤器堵塞导致压力下降"}],
                        }
                    ],
                },
            }
        ],
    )
    _write_jsonl(
        predictions,
        [
            {
                "id": "asset-doc-0001",
                "raw": '{"entities":"bad","relations":[]}',
                "prediction": {"entities": "bad", "relations": []},
                "json_valid": True,
                "json_error": None,
                "generation_error": None,
                "output_tokens": 10,
            }
        ],
    )
    _write_json(
        manifest,
        {"assets": {"test": ["asset-doc"]}, "leakage": []},
    )
    _write_json(
        selection,
        {"selected": {"name": "B1_evidence"}},
    )
    base = {
        "samples": 1,
        "json_valid_rate": 1.0,
        "contract_valid_rate": 0.0,
        "quote_verbatim_rate": 0.5,
        "negative_accuracy": 0.0,
        "avg_output_tokens": 100,
        "entity": {"f1": 0.1},
        "relation": {"f1": 0.0},
    }
    student = {
        "samples": 1,
        "json_valid_rate": 1.0,
        "contract_valid_rate": 0.95,
        "quote_verbatim_rate": 0.5,
        "negative_accuracy": 0.0,
        "avg_output_tokens": 10,
        "cause_rule_accuracy": 0.0,
        "entity": {"f1": 0.2},
        "relation": {"f1": 0.1},
    }
    _write_json(
        report,
        {
            "leakage": [],
            "runs": {
                "base": {"aggregate": base},
                "B1_evidence": {"aggregate": student},
            },
        },
    )

    summary, queue = audit(
        report_path=report,
        selection_path=selection,
        predictions_path=predictions,
        data_path=data,
        meta_path=meta,
        manifest_path=manifest,
        queue_limit=10,
    )

    assert summary["status"] == "core_pass_with_acceptance_gaps"
    assert summary["core_extraction_pass"] is True
    assert "quote_verbatim_rate_pass" in summary["blockers"]
    assert "gguf_artifact_present" in summary["blockers"]
    assert len(queue) == 1
    assert queue[0]["id"] == "asset-doc-0001"
    assert queue[0]["reasons"]
    assert queue[0]["status"] == "pending"
