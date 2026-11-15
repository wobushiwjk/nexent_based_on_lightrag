#!/usr/bin/env python
"""Run a deterministic source-retrieval baseline over the new W9 corpus."""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "eval" / "datasets" / "w9_unseen_web_20261115.jsonl"
DEFAULT_CORPUS = ROOT / "eval" / "corpus"
DEFAULT_OUT = ROOT / "eval" / "reports" / "w9-unseen-baseline.json"

_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_ASCII_RE = re.compile(r"[a-z0-9]+")


class W9EvaluationError(RuntimeError):
    """The benchmark cannot be evaluated from the frozen local corpus."""


def _tokenize(text: str) -> list[str]:
    normalized = text.casefold()
    tokens = _ASCII_RE.findall(normalized)
    for run in _CJK_RE.findall(normalized):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise W9EvaluationError(f"{path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise W9EvaluationError(f"{path}:{line_number}: case is not an object")
        rows.append(value)
    return rows


class BM25:
    """Small dependency-free BM25 index for six short source documents."""

    def __init__(self, documents: Mapping[str, str]) -> None:
        self.documents = dict(documents)
        self.tokens = {
            source_id: _tokenize(text) for source_id, text in self.documents.items()
        }
        self.term_frequencies = {
            source_id: collections.Counter(tokens)
            for source_id, tokens in self.tokens.items()
        }
        self.lengths = {
            source_id: len(tokens) for source_id, tokens in self.tokens.items()
        }
        self.average_length = (
            sum(self.lengths.values()) / len(self.lengths) if self.lengths else 0.0
        )
        self.document_frequency: collections.Counter[str] = collections.Counter()
        for frequencies in self.term_frequencies.values():
            self.document_frequency.update(frequencies.keys())

    def search(self, query: str) -> list[tuple[str, float]]:
        query_terms = set(_tokenize(query))
        document_count = len(self.documents)
        scores: list[tuple[str, float]] = []
        for source_id in self.documents:
            frequencies = self.term_frequencies[source_id]
            length = self.lengths[source_id]
            score = 0.0
            for term in query_terms:
                tf = frequencies.get(term, 0)
                if not tf:
                    continue
                df = self.document_frequency[term]
                idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
                denominator = tf + 1.5 * (
                    1 - 0.75 + 0.75 * length / (self.average_length or 1.0)
                )
                score += idf * (tf * 2.5) / denominator
            scores.append((source_id, score))
        return sorted(scores, key=lambda item: (-item[1], item[0]))


def evaluate(
    *,
    cases_path: Path = DEFAULT_CASES,
    corpus_dir: Path = DEFAULT_CORPUS,
) -> dict[str, Any]:
    cases = _jsonl(cases_path)
    if not cases:
        raise W9EvaluationError("no W9 cases found")
    source_ids = sorted({str(case.get("source_id") or "") for case in cases})
    documents: dict[str, str] = {}
    for source_id in source_ids:
        path = corpus_dir / "parsed" / f"{source_id}.txt"
        try:
            documents[source_id] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise W9EvaluationError(f"cannot read {path}: {exc}") from exc
    index = BM25(documents)
    details: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case.get("case_id") or "")
        source_id = str(case.get("source_id") or "")
        question = str(case.get("question") or "")
        ranked = index.search(question)
        ranked_ids = [item[0] for item in ranked]
        rank = ranked_ids.index(source_id) + 1 if source_id in ranked_ids else None
        details.append(
            {
                "case_id": case_id,
                "expected_source": source_id,
                "ranked_sources": ranked_ids,
                "gold_source_rank": rank,
                "hit_at_1": rank == 1,
                "hit_at_3": rank is not None and rank <= 3,
            }
        )
    total = len(details)
    hit_at_1 = sum(1 for item in details if item["hit_at_1"])
    hit_at_3 = sum(1 for item in details if item["hit_at_3"])
    hit_at_all = sum(1 for item in details if item["gold_source_rank"] is not None)
    report = {
        "generated_by": "scripts/evaluate_w9_unseen_sources.py",
        "cases_file": cases_path.name,
        "source_count": len(documents),
        "case_count": total,
        "metrics": {
            "source_hit_at_1": hit_at_1 / total,
            "source_hit_at_3": hit_at_3 / total,
            "source_hit_at_all": hit_at_all / total,
            "mean_reciprocal_rank": sum(
                1 / item["gold_source_rank"]
                for item in details
                if item["gold_source_rank"] is not None
            )
            / total,
        },
        "misses": [item for item in details if not item["hit_at_1"]],
        "cases": details,
        "ok": total > 0 and hit_at_all == total,
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = evaluate(
            cases_path=args.cases.resolve(),
            corpus_dir=args.corpus.resolve(),
        )
    except W9EvaluationError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "cases": report["case_count"],
                "source_hit_at_1": report["metrics"]["source_hit_at_1"],
                "source_hit_at_3": report["metrics"]["source_hit_at_3"],
                "report": str(args.out.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BM25",
    "DEFAULT_CASES",
    "DEFAULT_CORPUS",
    "DEFAULT_OUT",
    "W9EvaluationError",
    "build_parser",
    "evaluate",
    "main",
]
