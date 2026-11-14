#!/usr/bin/env python3
"""Convert the CFDK standoff archive into W7 chunk and label JSONL.

CFDK (Compressor Fault Diagnosis Knowledge) ships expert spans for
``设备/需求/故障/原因/方案`` but no relation annotations.  This importer keeps
only ``故障`` and ``原因``, maps both to ``FaultEvent``, and constructs
``CAUSES`` under one deliberately conservative rule:

* a record with exactly one ``故障`` span gets one relation per ``原因`` span;
* a record with several faults keeps its entities but gets no relations.

The rule trades recall for precision.  The resulting targets are a bootstrap
set, not gold relations; teacher or human review can add the multi-fault cases
later.  The source is CC BY-SA 3.0 (see ``training/corpus/sources.yml``).

The archive is read in place with :mod:`zipfile`; nothing is extracted or
executed.  Run from the repository root so the shared validator import
resolves:

    python scripts/ingest_cfdk.py \
        --zip training/corpus/raw/cfdk/CFDK_dataset_BioNLP11_shared_task_standoff_format.zip

The default outputs are gitignored:
``training/corpus/parsed/cfdk_chunks.jsonl``,
``training/data/cfdk_bootstrap_labels.jsonl`` and
``training/data/cfdk_report.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lightrag.fault_tree.distillation.labels import (  # noqa: E402
    StudentLabelError,
    validate_student_target,
)

DEFAULT_ZIP = (
    REPO_ROOT
    / "training/corpus/raw/cfdk/"
    / "CFDK_dataset_BioNLP11_shared_task_standoff_format.zip"
)
DEFAULT_CHUNKS = REPO_ROOT / "training/corpus/parsed/cfdk_chunks.jsonl"
DEFAULT_LABELS = REPO_ROOT / "training/data/cfdk_bootstrap_labels.jsonl"
DEFAULT_REPORT = REPO_ROOT / "training/data/cfdk_report.json"
DEFAULT_SOURCE_ID = "cfdk-compressor-maintenance"
DEFAULT_SOURCE_URL = "https://github.com/chentao1999/CFDK"
DEFAULT_LICENSE = "CC BY-SA 3.0"

# A negative must look administrative and must not contain a fault or causal
# cue.  This is intentionally strict: a missed negative costs a training row,
# while a false negative teaches the student to stay silent on a real fault.
_FAULT_CUE = re.compile(
    r"故障|失效|损坏|漏|渗|异常|报警|不准|误差|磨损|堵塞|振动|高温|"
    r"压力高|温度高|断裂|卡死|卡住|锈|松动|噪声|异响|事故|问题|缺陷|"
    r"变形|破裂|烧|跳机|停机|无法|不能"
)
_CAUSE_CUE = re.compile(r"导致|因为|由于|原因|造成|引起|分析|检查|发现")
_ADMIN_CUE = re.compile(
    r"已安排|请|前往|调试|培训|采购|发货|资料|服务|联系|处理|落实|回复|通知|安排"
)


def is_cjk(ch: str) -> bool:
    """Return whether ``ch`` is a common CJK ideograph."""
    return "一" <= ch <= "鿿"


def clean_with_map(text: str) -> tuple[str, list[int]]:
    """Detokenize ``text`` and return a character-offset map.

    The CFDK ``.txt`` files are segmented with spaces and start with a BOM.
    ``mapping[i]`` is the offset in the cleaned string for original offset
    ``i``; dropped characters map to the next kept character.  The map lets
    the standoff annotation offsets survive detokenization.
    """
    out: list[str] = []
    mapping = [0] * (len(text) + 1)
    for i, ch in enumerate(text):
        mapping[i] = len(out)
        if ch == "﻿":
            continue
        if ch.isspace():
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if is_cjk(prev) and is_cjk(nxt):
                continue
            if prev in "，。！？；、：（）()" or nxt in "，。！？；、：（）()":
                continue
            if out and out[-1] == " ":
                continue
            out.append(" ")
            continue
        out.append(ch)
    mapping[len(text)] = len(out)
    return "".join(out), mapping


def record_spans(text: str) -> Iterable[tuple[int, int]]:
    """Yield ``(start, end)`` for blank-line separated records."""
    pos = 0
    for match in re.finditer(r"(?:\r?\n[ \t]*){2,}", text):
        segment = text[pos : match.start()]
        stripped = segment.strip()
        if stripped:
            lead = len(segment) - len(segment.lstrip())
            yield pos + lead, pos + lead + len(stripped)
        pos = match.end()
    segment = text[pos:]
    stripped = segment.strip()
    if stripped:
        lead = len(segment) - len(segment.lstrip())
        yield pos + lead, pos + lead + len(stripped)


def parse_standoff_ann(raw: bytes, text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse ``故障``/``原因`` spans and verify them against ``text``."""
    annotations: list[dict[str, Any]] = []
    mismatches = 0
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("T"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        head = parts[1].split()
        if len(head) < 3 or head[0] not in ("故障", "原因"):
            continue
        try:
            start, end = int(head[1]), int(head[2])
        except ValueError:
            continue
        actual = text[start:end]
        if re.sub(r"\s+", "", actual) != re.sub(r"\s+", "", parts[2]):
            mismatches += 1
            continue
        annotations.append(
            {"label": head[0], "start": start, "end": end}
        )
    return annotations, mismatches


_NAME_PREFIX = re.compile(
    r"^(?:初步分析为|初步分析|分析认为|分析为|经检查为|经检查|检查发现|"
    r"发现|判断为|判断|可能是由于|可能是|估计是|估计|应该是|应该|疑似|"
    r"主要是|主要|造成|导致|由于|因为|属|系|因)"
)
_NAME_SUFFIX = re.compile(r"(?:造成的|引起的|所致|导致)$")


def normalize_name(quote: str) -> str:
    """Turn a span into a cleaner fault name without losing the quote.

    CFDK annotates whole clauses, so a cause often starts with ``初步分析为``
    or ends with ``造成的``.  The evidence keeps the verbatim quote; only the
    ``name`` is trimmed.  Anonymization placeholders become their Chinese
    equivalents so the student does not learn ``company`` as a fault word.
    """
    name = quote
    # One pass would leave "主要由于..." -> "由于...", so keep stripping
    # until the name stops changing (bounded to avoid pathological loops).
    for _ in range(4):
        stripped = _NAME_PREFIX.sub("", name).strip("，,。；;：: ")
        stripped = _NAME_SUFFIX.sub("", stripped).strip()
        if stripped == name or not stripped:
            break
        name = stripped
    name = name.replace("company", "公司").replace("person", "人员")
    # Detokenization keeps spaces around Latin tokens, so the replacement
    # above can leave "两件 公司 电机".  Chinese text is normally unspaced.
    name = re.sub(r"(?<=[一-鿿])\s+|\s+(?=[一-鿿])", "", name).strip()
    if not name:
        name = quote
    return name if len(name) <= 100 else name[:97] + "..."


def _entity(local_id: str, quote: str) -> dict[str, Any]:
    name = normalize_name(quote)
    return {
        "local_id": local_id,
        "class": "FaultEvent",
        "name": name,
        "description": f"故障事件：{name}",
        "evidence": [{"quote": quote}],
    }


def build_target(
    chunk_text: str,
    annotations: list[dict[str, Any]],
    mapping: list[int],
    record_start: int,
    record_end: int,
    cleaned: str,
) -> tuple[dict[str, Any] | None, list[str], dict[str, int]]:
    """Build one target for a record, or return ``None`` for a skip."""
    warnings: list[str] = []
    stats = Counter()
    faults: list[str] = []
    causes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for ann in annotations:
        if not (record_start <= ann["start"] < record_end):
            continue
        quote = cleaned[mapping[ann["start"]] : mapping[ann["end"]]].strip()
        if not quote:
            warnings.append("span-empty")
            continue
        if quote not in chunk_text:
            warnings.append("span-not-grounded")
            continue
        key = (ann["label"], quote)
        if key in seen:
            continue
        seen.add(key)
        if ann["label"] == "故障":
            faults.append(quote)
        else:
            causes.append(quote)

    if not faults and not causes:
        if not _ADMIN_CUE.search(chunk_text):
            return None, warnings, dict(stats)
        if _FAULT_CUE.search(chunk_text) or _CAUSE_CUE.search(chunk_text):
            return None, warnings, dict(stats)
        return (
            {"entities": [], "relations": []},
            ["negative:strict-heuristic"],
            {"entities": 0, "relations": 0, "nodes": 0, "dropped_nodes": 0},
        )

    if not faults:
        # A cause without a fault cannot be oriented; keep the chunk for a
        # later teacher pass but do not emit a label now.
        return None, warnings, dict(stats)

    entities: list[dict[str, Any]] = []
    cause_ids: list[tuple[str, str]] = []
    for index, quote in enumerate(causes, start=1):
        local_id = f"c{index}"
        cause_ids.append((local_id, quote))
        entities.append(_entity(local_id, quote))
    fault_ids: list[tuple[str, str]] = []
    for index, quote in enumerate(faults, start=1):
        local_id = f"f{index}"
        fault_ids.append((local_id, quote))
        entities.append(_entity(local_id, quote))

    relations: list[dict[str, Any]] = []
    if len(fault_ids) == 1 and cause_ids:
        fault_id, fault_quote = fault_ids[0]
        for cause_id, cause_quote in cause_ids:
            if (
                cause_quote == fault_quote
                or cause_quote in fault_quote
                or fault_quote in cause_quote
            ):
                warnings.append("relation-skipped:containment")
                continue
            relations.append(
                {
                    "relation_code": "CAUSES",
                    "source_local_id": cause_id,
                    "target_local_id": fault_id,
                    "evidence": [
                        {"quote": cause_quote},
                        {"quote": fault_quote},
                    ],
                }
            )
    elif faults and causes:
        warnings.append("relations-skipped:multi-fault")

    stats["entities"] = len(entities)
    stats["relations"] = len(relations)
    stats["nodes"] = len(entities)
    stats["dropped_nodes"] = 0
    return (
        {
            "entities": entities,
            "relations": relations,
        },
        warnings,
        dict(stats),
    )


def _natural_key(name: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--chunks-out", type=Path, default=DEFAULT_CHUNKS)
    parser.add_argument("--labels-out", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--report-out", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--license", default=DEFAULT_LICENSE)
    parser.add_argument("--max-chars", type=int, default=2000)
    parser.add_argument("--max-negatives", type=int, default=400)
    parser.add_argument("--limit", type=int, default=0, help="only process N records")
    parser.add_argument("--strict", action="store_true", help="abort on invalid target")
    args = parser.parse_args(argv)

    if not args.zip.exists():
        parser.error(f"archive not found: {args.zip}")

    stats: Counter = Counter()
    length_hist: Counter = Counter()
    warning_hist: Counter = Counter()
    errors: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    records_seen = 0
    negatives = 0

    with zipfile.ZipFile(args.zip) as archive:
        pairs: dict[str, dict[str, str]] = {}
        for name in archive.namelist():
            if name.endswith((".txt", ".ann")):
                base, ext = name.rsplit(".", 1)
                pairs.setdefault(base, {})[ext] = name
        for base in sorted(pairs, key=_natural_key):
            if set(pairs[base]) != {"txt", "ann"}:
                continue
            stats["files"] += 1
            raw = archive.read(pairs[base]["txt"]).decode("utf-8")
            cleaned, mapping = clean_with_map(raw)
            annotations, mismatches = parse_standoff_ann(
                archive.read(pairs[base]["ann"]), raw
            )
            stats["annotation_offset_mismatches"] += mismatches
            asset_id = f"CFDK-{Path(base).name}"
            record_index = 0
            for record_start, record_end in record_spans(raw):
                if args.limit and records_seen >= args.limit:
                    break
                records_seen += 1
                stats["records"] += 1
                chunk_text = cleaned[
                    mapping[record_start] : mapping[record_end]
                ].strip()
                # The archive marks empty service fields with a standalone
                # "none" line.  Detokenization may turn that line break into
                # a space, so remove both a standalone line and a trailing
                # token.
                chunk_text = re.sub(
                    r"(?im)^[ \t]*none[ \t]*$", "", chunk_text
                )
                chunk_text = re.sub(
                    r"(?i)\s*none\s*$", "", chunk_text
                ).strip()
                if not chunk_text:
                    stats["empty_records"] += 1
                    continue
                if len(chunk_text) > args.max_chars:
                    stats["too_long_records"] += 1
                    continue
                text_sha256 = hashlib.sha256(
                    chunk_text.encode("utf-8")
                ).hexdigest()
                if text_sha256 in seen_hashes:
                    stats["duplicate_records"] += 1
                    continue
                seen_hashes.add(text_sha256)
                record_index += 1
                chunk_id = f"{asset_id}-{record_index:04d}"
                chunk = {
                    "chunk_id": chunk_id,
                    "asset_id": asset_id,
                    "text": chunk_text,
                    "page": None,
                    "section_path": "",
                    "modality": "text",
                    "source_id": args.source_id,
                    "source_url": args.source_url,
                    "license": args.license,
                    "text_sha256": text_sha256,
                }
                chunks.append(chunk)
                stats["chunks"] += 1
                if len(chunk_text) <= 500:
                    length_hist["<=500"] += 1
                elif len(chunk_text) <= 1000:
                    length_hist["501-1000"] += 1
                elif len(chunk_text) <= 1500:
                    length_hist["1001-1500"] += 1
                else:
                    length_hist[">1500"] += 1

                target, warnings, target_stats = build_target(
                    chunk_text,
                    annotations,
                    mapping,
                    record_start,
                    record_end,
                    cleaned,
                )
                if target is None:
                    if warnings:
                        warning_hist.update(warnings)
                    continue
                if not target["entities"] and not target["relations"]:
                    if negatives >= args.max_negatives:
                        stats["negatives_capped"] += 1
                        continue
                    negatives += 1
                    stats["negatives"] += 1
                elif target["relations"]:
                    stats["positive_with_relations"] += 1
                    stats["relations"] += len(target["relations"])
                else:
                    stats["positive_entities_only"] += 1
                stats["entities"] += len(target["entities"])
                warning_hist.update(warnings)
                try:
                    validate_student_target(
                        target, chunk_text, sample_id=chunk_id
                    )
                except StudentLabelError as exc:
                    stats["invalid_targets"] += 1
                    errors.append(
                        {
                            "chunk_id": chunk_id,
                            "asset_id": asset_id,
                            "error": str(exc),
                        }
                    )
                    if args.strict:
                        raise
                    continue
                labels.append(
                    {
                        "chunk_id": chunk_id,
                        "asset_id": asset_id,
                        "source_id": args.source_id,
                        "target": target,
                        "warnings": warnings,
                        "stats": target_stats,
                    }
                )
            if args.limit and records_seen >= args.limit:
                break

    _write_jsonl(args.chunks_out, chunks)
    _write_jsonl(args.labels_out, labels)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "scripts/ingest_cfdk.py",
        "source": {
            "id": args.source_id,
            "archive": str(args.zip),
            "license": args.license,
            "url": args.source_url,
            "public": True,
        },
        "stats": dict(stats),
        "length_histogram": dict(length_hist),
        "warnings": dict(warning_hist),
        "errors": errors[:50],
        "outputs": {
            "chunks": str(args.chunks_out),
            "labels": str(args.labels_out),
        },
    }
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
