#!/usr/bin/env python
"""Validate the new W9 web corpus cases and write a frozen manifest."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "eval" / "datasets" / "w9_unseen_web_20261115.jsonl"
DEFAULT_SOURCES = ROOT / "eval" / "w9_sources.json"
DEFAULT_CORPUS = ROOT / "eval" / "corpus"
DEFAULT_OUT = ROOT / "eval" / "reports" / "w9-unseen-manifest.json"
MINIMUM_CASES = 30


class W9ManifestError(RuntimeError):
    """The selected cases cannot form a valid held-out W9 suite."""


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise W9ManifestError(f"{path} must contain a JSON object")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise W9ManifestError(f"cannot read {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise W9ManifestError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise W9ManifestError(f"{path}:{line_number}: case must be an object")
        rows.append(row)
    return rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(
    *,
    cases_path: Path = DEFAULT_CASES,
    sources_path: Path = DEFAULT_SOURCES,
    corpus_dir: Path = DEFAULT_CORPUS,
    minimum_cases: int = MINIMUM_CASES,
) -> dict[str, Any]:
    registry = _read_json(sources_path)
    source_rows = registry.get("sources")
    if not isinstance(source_rows, list):
        raise W9ManifestError(f"{sources_path} has no sources list")
    sources = {
        str(item.get("id")): item
        for item in source_rows
        if isinstance(item, dict) and item.get("id")
    }
    manifest_path = corpus_dir / "manifest.json"
    downloaded = _read_json(manifest_path)
    downloaded_by_id = {
        str(item.get("source_id")): item
        for item in downloaded.get("sources") or []
        if isinstance(item, dict)
    }
    cases = _read_jsonl(cases_path)
    if len(cases) < minimum_cases:
        raise W9ManifestError(f"need at least {minimum_cases} cases, got {len(cases)}")
    seen: set[str] = set()
    source_counts: collections.Counter[str] = collections.Counter()
    category_counts: collections.Counter[str] = collections.Counter()
    validated: list[dict[str, Any]] = []
    for row in cases:
        case_id = _required_text(row, "case_id")
        if case_id in seen:
            raise W9ManifestError(f"duplicate case_id {case_id!r}")
        seen.add(case_id)
        source_id = _required_text(row, "source_id")
        if source_id not in sources or source_id not in downloaded_by_id:
            raise W9ManifestError(
                f"{case_id}: source {source_id!r} is not frozen in the corpus"
            )
        text_path = corpus_dir / "parsed" / f"{source_id}.txt"
        try:
            text = text_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise W9ManifestError(f"cannot read {text_path}: {exc}") from exc
        gold = row.get("gold")
        if not isinstance(gold, dict):
            raise W9ManifestError(f"{case_id}: gold must be an object")
        quote = _required_text(gold, "quote", owner=f"{case_id}.gold")
        if quote not in text:
            raise W9ManifestError(f"{case_id}: gold quote is not verbatim")
        terms = gold.get("required_terms")
        if not isinstance(terms, list) or not terms:
            raise W9ManifestError(f"{case_id}: required_terms must not be empty")
        missing = [str(term) for term in terms if str(term) not in quote]
        if missing:
            raise W9ManifestError(
                f"{case_id}: terms are not supported by its gold quote: {missing}"
            )
        category = _required_text(row, "category")
        source_counts[source_id] += 1
        category_counts[category] += 1
        validated.append(
            {
                "case_id": case_id,
                "source_id": source_id,
                "publisher": sources[source_id].get("publisher"),
                "category": category,
                "question": _required_text(row, "question"),
                "gold_terms": [str(term) for term in terms],
                "gold_quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                "expected_status": _required_text(row, "expected_status"),
            }
        )

    w7_assets = _w7_assets()
    if any(source_id in w7_assets for source_id in source_counts):
        raise W9ManifestError("W9 source ids overlap the W7 dataset")
    return {
        "generated_by": "scripts/build_w9_web_manifest.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "frozen_on": registry.get("frozen_on"),
        "language": "zh-CN",
        "usage_policy": registry.get("usage_policy"),
        "cases_path": _display(cases_path),
        "cases_sha256": _sha256(cases_path),
        "sources_path": _display(sources_path),
        "sources_sha256": _sha256(sources_path),
        "corpus_fingerprint": _corpus_fingerprint(downloaded),
        "case_count": len(validated),
        "source_count": len(source_counts),
        "sources": [
            {
                "source_id": source_id,
                "title": sources[source_id].get("title"),
                "publisher": sources[source_id].get("publisher"),
                "url": sources[source_id].get("url"),
                "case_count": count,
                "raw_sha256": downloaded_by_id[source_id].get("raw_sha256"),
                "text_sha256": downloaded_by_id[source_id].get("text_sha256"),
            }
            for source_id, count in sorted(source_counts.items())
        ],
        "category_counts": dict(sorted(category_counts.items())),
        "w7_overlap": [],
        "cases": validated,
        "ok": True,
    }


def _w7_assets() -> set[str]:
    manifest_path = ROOT / "training" / "data" / "w7_b" / "manifest.json"
    if not manifest_path.is_file():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        return set()
    return {str(item) for values in assets.values() for item in values or ()}


def _corpus_fingerprint(manifest: Mapping[str, Any]) -> str:
    rows = [
        {
            "source_id": str(item.get("source_id")),
            "raw_sha256": item.get("raw_sha256"),
            "text_sha256": item.get("text_sha256"),
        }
        for item in manifest.get("sources") or []
        if isinstance(item, dict)
    ]
    rows.sort(key=lambda item: item["source_id"])
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _required_text(
    mapping: Mapping[str, Any],
    key: str,
    *,
    owner: str = "case",
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise W9ManifestError(f"{owner}.{key} must be a non-empty string")
    return value.strip()


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--minimum-cases", type=int, default=MINIMUM_CASES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_manifest(
            cases_path=args.cases.resolve(),
            sources_path=args.sources.resolve(),
            corpus_dir=args.corpus.resolve(),
            minimum_cases=args.minimum_cases,
        )
    except W9ManifestError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "cases": report["case_count"],
                "sources": report["source_count"],
                "manifest": str(args.out.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_CASES",
    "DEFAULT_CORPUS",
    "DEFAULT_OUT",
    "DEFAULT_SOURCES",
    "MINIMUM_CASES",
    "W9ManifestError",
    "build_manifest",
    "build_parser",
    "main",
]
