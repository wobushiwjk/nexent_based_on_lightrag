"""Tests for the independently downloaded W9 dataset pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.build_w9_web_manifest import build_manifest
from scripts.evaluate_w9_unseen_sources import evaluate


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_manifest_validates_verbatim_gold_and_hashes(tmp_path: Path) -> None:
    sources = tmp_path / "sources.json"
    cases = tmp_path / "cases.jsonl"
    corpus = tmp_path / "corpus"
    parsed = corpus / "parsed"
    parsed.mkdir(parents=True)
    (parsed / "source-a.txt").write_text(
        "离心泵不出水。可能原因：吸入管路漏气。",
        encoding="utf-8",
    )
    _write_json(
        sources,
        {
            "frozen_on": "2026-11-15",
            "usage_policy": "test",
            "sources": [
                {
                    "id": "source-a",
                    "title": "泵故障",
                    "publisher": "测试厂商",
                    "url": "https://example.test/a",
                }
            ],
        },
    )
    _write_json(
        corpus / "manifest.json",
        {
            "sources": [
                {
                    "source_id": "source-a",
                    "raw_sha256": "raw",
                    "text_sha256": "text",
                }
            ]
        },
    )
    rows = [
        {
            "case_id": "W9-T-1",
            "source_id": "source-a",
            "category": "fault_cause",
            "question": "离心泵不出水是什么原因？",
            "gold": {
                "quote": "离心泵不出水。可能原因：吸入管路漏气。",
                "required_terms": ["吸入管路漏气"],
            },
            "expected_status": "diagnosed",
        },
        {
            "case_id": "W9-T-2",
            "source_id": "source-a",
            "category": "fault_cause",
            "question": "应该检查什么？",
            "gold": {
                "quote": "可能原因：吸入管路漏气。",
                "required_terms": ["吸入管路漏气"],
            },
            "expected_status": "diagnosed",
        },
    ]
    cases.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    report = build_manifest(
        cases_path=cases,
        sources_path=sources,
        corpus_dir=corpus,
        minimum_cases=2,
    )

    assert report["ok"] is True
    assert report["case_count"] == 2
    assert report["source_count"] == 1
    assert len(report["cases"][0]["gold_quote_sha256"]) == 64


def test_source_retrieval_baseline_reports_rank_metrics(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    corpus = tmp_path / "corpus" / "parsed"
    corpus.mkdir(parents=True)
    (corpus / "pump-a.txt").write_text(
        "离心泵不出水。吸入管路漏气是常见原因。",
        encoding="utf-8",
    )
    (corpus / "pump-b.txt").write_text(
        "轴承发热。润滑油不足是常见原因。",
        encoding="utf-8",
    )
    case_rows = [
        {
            "case_id": "W9-R-1",
            "source_id": "pump-a",
            "question": "离心泵不出水是什么原因？",
        },
        {
            "case_id": "W9-R-2",
            "source_id": "pump-b",
            "question": "轴承发热为什么缺油？",
        },
    ]
    cases.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in case_rows),
        encoding="utf-8",
    )

    report = evaluate(cases_path=cases, corpus_dir=corpus.parent)

    assert report["ok"] is True
    assert report["metrics"]["source_hit_at_1"] == 1.0
    assert report["metrics"]["source_hit_at_all"] == 1.0
