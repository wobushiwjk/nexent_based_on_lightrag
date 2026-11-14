"""Build the W7 student dataset from chunk and label JSONL files.

The builder is deliberately small and data-agnostic: it does not call a model,
read a document, or touch the network.  Its job is to take the chunk manifest
and one or more label files, keep only contract-valid targets, split by
**document** (``asset_id``) rather than by chunk, and emit ShareGPT rows plus
an auditable manifest.

Label-file precedence follows the order given on the command line: a later
file overrides an earlier one for the same ``chunk_id``.  The default order is
``public`` -> ``teacher`` -> ``reviewed``, so a human review always wins.  If
the higher-priority label is invalid it is recorded as an error and the
previous valid label is kept; a bad review must not silently discard a good
teacher label, and it must not be silently used either.

Train rows can be repeated or capped by source (``rebalance_train``) so a
small target-domain corpus is not drowned by a larger cross-domain one.  Only
training rows are touched; the evaluation splits keep their shape.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from lightrag.fault_tree.distillation.labels import (
    StudentLabelError,
    validate_student_target,
)
from lightrag.fault_tree.distillation.prompts import build_user_prompt


class DatasetBuildError(RuntimeError):
    """The dataset cannot be built from the supplied files."""


@dataclass(frozen=True)
class ChunkRecord:
    """One chunk from the parsed corpus."""

    chunk_id: str
    asset_id: str
    text: str
    page: Any = None
    section_path: str = ""
    modality: str = "text"
    source_id: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any], *, line_no: int) -> "ChunkRecord":
        chunk_id = str(row.get("chunk_id") or "").strip()
        asset_id = str(row.get("asset_id") or "").strip()
        text = str(row.get("text") or "")
        if not chunk_id:
            raise DatasetBuildError(f"chunk line {line_no}: missing chunk_id")
        if not asset_id:
            raise DatasetBuildError(f"chunk line {line_no}: missing asset_id")
        if not text.strip():
            raise DatasetBuildError(f"chunk line {line_no}: empty text")
        return cls(
            chunk_id=chunk_id,
            asset_id=asset_id,
            text=text,
            page=row.get("page"),
            section_path=str(row.get("section_path") or ""),
            modality=str(row.get("modality") or "text"),
            source_id=str(row.get("source_id") or ""),
        )


@dataclass(frozen=True)
class LabelRecord:
    """A validated target for one chunk."""

    chunk_id: str
    target: dict[str, Any]
    origin: str
    source_path: str


@dataclass
class Split:
    train: list[LabelRecord] = field(default_factory=list)
    val: list[LabelRecord] = field(default_factory=list)
    test: list[LabelRecord] = field(default_factory=list)
    assets: dict[str, list[str]] = field(default_factory=dict)


def _read_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetBuildError(f"cannot read {path}: {exc}") from exc
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetBuildError(
                f"{path}:{line_no}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise DatasetBuildError(f"{path}:{line_no}: row must be an object")
        yield line_no, row


def load_chunks(path: Path) -> dict[str, ChunkRecord]:
    chunks: dict[str, ChunkRecord] = {}
    for line_no, row in _read_jsonl(path):
        chunk = ChunkRecord.from_row(row, line_no=line_no)
        if chunk.chunk_id in chunks:
            raise DatasetBuildError(
                f"{path}:{line_no}: duplicate chunk_id {chunk.chunk_id}"
            )
        chunks[chunk.chunk_id] = chunk
    if not chunks:
        raise DatasetBuildError(f"{path}: no chunks found")
    return chunks


def merge_chunk_files(paths: Sequence[Path]) -> dict[str, ChunkRecord]:
    """Load several chunk manifests into one id-keyed map.

    Cross-domain datasets are built from one manifest per source (CFDK,
    manuals, accident reports), so the builder must accept more than one
    file.  A chunk id may appear only once across all files; a collision is a
    data error, not something to resolve by precedence.
    """
    merged: dict[str, ChunkRecord] = {}
    for path in paths:
        for chunk_id, chunk in load_chunks(path).items():
            if chunk_id in merged:
                raise DatasetBuildError(
                    f"duplicate chunk_id {chunk_id} across chunk files "
                    f"(second occurrence in {path})"
                )
            merged[chunk_id] = chunk
    if not merged:
        raise DatasetBuildError("no chunks found in the supplied files")
    return merged


def _read_label_rows(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    return _read_jsonl(path)


def merge_labels(
    label_files: Sequence[tuple[Path, str]],
    chunks: dict[str, ChunkRecord],
    *,
    strict: bool = False,
) -> tuple[list[LabelRecord], list[dict[str, Any]]]:
    """Validate and merge label files; later files take precedence.

    ``label_files`` is ordered from lowest to highest priority.  A later file
    overrides an earlier one only when its target passes validation.
    """
    chosen: dict[str, LabelRecord] = {}
    errors: list[dict[str, Any]] = []
    for path, origin in label_files:
        for line_no, row in _read_label_rows(path):
            chunk_id = str(row.get("chunk_id") or "").strip()
            if not chunk_id:
                error = {
                    "file": str(path),
                    "line": line_no,
                    "chunk_id": "",
                    "origin": origin,
                    "error": "missing chunk_id",
                }
                errors.append(error)
                if strict:
                    raise DatasetBuildError(
                        f"{path}:{line_no}: missing chunk_id"
                    )
                continue
            chunk = chunks.get(chunk_id)
            if chunk is None:
                error = {
                    "file": str(path),
                    "line": line_no,
                    "chunk_id": chunk_id,
                    "origin": origin,
                    "error": "chunk not found in chunks.jsonl",
                }
                errors.append(error)
                if strict:
                    raise DatasetBuildError(
                        f"{path}:{line_no}: unknown chunk {chunk_id}"
                    )
                continue
            target = row.get("target")
            if not isinstance(target, dict):
                error = {
                    "file": str(path),
                    "line": line_no,
                    "chunk_id": chunk_id,
                    "origin": origin,
                    "error": "target must be an object",
                }
                errors.append(error)
                if strict:
                    raise DatasetBuildError(
                        f"{path}:{line_no}: target must be an object"
                    )
                continue
            try:
                validate_student_target(
                    target, chunk.text, sample_id=chunk_id
                )
            except StudentLabelError as exc:
                error = {
                    "file": str(path),
                    "line": line_no,
                    "chunk_id": chunk_id,
                    "origin": origin,
                    "error": str(exc),
                }
                errors.append(error)
                if strict:
                    raise DatasetBuildError(str(exc)) from exc
                continue
            chosen[chunk_id] = LabelRecord(
                chunk_id=chunk_id,
                target=target,
                origin=origin,
                source_path=str(path),
            )
    records = sorted(
        chosen.values(),
        key=lambda record: (chunks[record.chunk_id].asset_id, record.chunk_id),
    )
    return records, errors


def split_by_asset(
    records: Sequence[LabelRecord],
    chunks: dict[str, ChunkRecord],
    *,
    val_ratio: float = 0.1,
    test_ratio: float = 0.1,
    seed: int = 42,
    forced_splits: dict[str, str] | None = None,
) -> Split:
    """Split by document so no asset leaks between train and evaluation.

    ``forced_splits`` pins named assets to ``train``/``val``/``test`` before
    the random fill.  It exists for domain holdouts: the target-domain manual
    must be evaluated, not merely sampled into training by a lucky seed.
    """
    if not 0.0 <= val_ratio < 1.0 or not 0.0 <= test_ratio < 1.0:
        raise DatasetBuildError("split ratios must be in [0, 1)")
    grouped: dict[str, list[LabelRecord]] = {}
    for record in records:
        asset_id = chunks[record.chunk_id].asset_id
        grouped.setdefault(asset_id, []).append(record)
    assets = sorted(grouped)
    if not assets:
        raise DatasetBuildError("no labels to split")

    forced = dict(forced_splits or {})
    allowed = {"train", "val", "test"}
    bad = sorted(set(forced.values()) - allowed)
    if bad:
        raise DatasetBuildError(f"unknown split names: {bad}")
    unknown = sorted(set(forced) - set(assets))
    if unknown:
        raise DatasetBuildError(f"forced assets not in the dataset: {unknown}")
    forced_by_split: dict[str, set[str]] = {
        "train": set(),
        "val": set(),
        "test": set(),
    }
    for asset_id, name in forced.items():
        forced_by_split[name].add(asset_id)

    remaining = [asset for asset in assets if asset not in forced]
    random.Random(seed).shuffle(remaining)

    count = len(assets)
    if count == 1:
        val_count = test_count = 0
    elif count == 2:
        val_count, test_count = 1, 0
    else:
        val_count = max(1, int(round(count * val_ratio))) if val_ratio else 0
        test_count = max(1, int(round(count * test_ratio))) if test_ratio else 0
        while val_count + test_count >= count:
            if test_count >= val_count and test_count > 0:
                test_count -= 1
            elif val_count > 0:
                val_count -= 1
            else:
                break

    test_need = max(0, test_count - len(forced_by_split["test"]))
    val_need = max(0, val_count - len(forced_by_split["val"]))
    test_assets = forced_by_split["test"] | set(remaining[:test_need])
    val_assets = forced_by_split["val"] | set(
        remaining[test_need : test_need + val_need]
    )
    train_assets = forced_by_split["train"] | set(
        remaining[test_need + val_need :]
    )

    split = Split()
    for asset_id in sorted(grouped):
        bucket = (
            split.test
            if asset_id in test_assets
            else split.val
            if asset_id in val_assets
            else split.train
        )
        bucket.extend(grouped[asset_id])
    split.assets = {
        "train": sorted(train_assets),
        "val": sorted(val_assets),
        "test": sorted(test_assets),
    }
    return split


def rebalance_train(
    split: Split,
    chunks: dict[str, ChunkRecord],
    *,
    repeats: dict[str, int] | None = None,
    caps: dict[str, int] | None = None,
    seed: int = 42,
) -> Split:
    """Repeat or cap train rows by source; val and test stay untouched.

    A small target-domain corpus is otherwise swallowed by the larger
    cross-domain one.  ``repeats`` maps a ``source_id`` to how many times its
    train rows appear (loss weighting); ``caps`` maps a source to the maximum
    number of train rows, sampled deterministically with ``seed`` (replay
    budget).  Repetition duplicates rows in the output file; it does not
    invent new ones, so the per-source metrics still count unique chunks.

    A source must have at least one train row; naming a source that appears
    only in val/test is an error rather than a silent no-op.
    """
    repeats = dict(repeats or {})
    caps = dict(caps or {})
    for name, value in [*repeats.items(), *caps.items()]:
        if value < 1:
            raise DatasetBuildError(f"{name}: count must be >= 1, got {value}")
    both = sorted(set(repeats) & set(caps))
    if both:
        raise DatasetBuildError(
            f"sources cannot be both repeated and capped: {both}"
        )

    def source_of(record: LabelRecord) -> str:
        chunk = chunks[record.chunk_id]
        return chunk.source_id or chunk.asset_id

    grouped: dict[str, list[LabelRecord]] = {}
    for record in split.train:
        grouped.setdefault(source_of(record), []).append(record)

    unknown = sorted((set(repeats) | set(caps)) - set(grouped))
    if unknown:
        raise DatasetBuildError(f"sources not in the train split: {unknown}")

    rng = random.Random(seed)
    train: list[LabelRecord] = []
    for source in sorted(grouped):
        rows = sorted(
            grouped[source],
            key=lambda record: (chunks[record.chunk_id].asset_id, record.chunk_id),
        )
        if source in caps and len(rows) > caps[source]:
            rows = sorted(
                rng.sample(rows, caps[source]),
                key=lambda record: (
                    chunks[record.chunk_id].asset_id,
                    record.chunk_id,
                ),
            )
        train.extend(rows * repeats.get(source, 1))

    return Split(
        train=train,
        val=list(split.val),
        test=list(split.test),
        assets=split.assets,
    )


def sharegpt_row(
    record: LabelRecord,
    chunk: ChunkRecord,
    system_prompt: str,
) -> dict[str, Any]:
    return {
        "id": chunk.chunk_id,
        "system": system_prompt,
        "conversations": [
            {
                "from": "human",
                "value": build_user_prompt(
                    {
                        "chunk_id": chunk.chunk_id,
                        "asset_id": chunk.asset_id,
                        "text": chunk.text,
                        "page": chunk.page,
                        "section_path": chunk.section_path,
                        "modality": chunk.modality,
                    }
                ),
            },
            {
                "from": "gpt",
                "value": json.dumps(
                    record.target, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ],
    }


def _meta_row(record: LabelRecord, chunk: ChunkRecord) -> dict[str, Any]:
    return {
        "id": chunk.chunk_id,
        "chunk_text": chunk.text,
        "gold": record.target,
    }


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_dataset(
    split: Split,
    chunks: dict[str, ChunkRecord],
    system_prompt: str,
    out_dir: Path,
    *,
    seed: int,
    val_ratio: float,
    test_ratio: float,
    label_files: Sequence[tuple[Path, str]],
    errors: Sequence[dict[str, Any]],
    chunk_files: Sequence[Path] = (),
    forced_splits: dict[str, str] | None = None,
    train_mix: dict[str, Any] | None = None,
    pinned_from: str | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        out_dir / "train.jsonl",
        (sharegpt_row(record, chunks[record.chunk_id], system_prompt)
         for record in split.train),
    )
    _write_jsonl(
        out_dir / "val.jsonl",
        (sharegpt_row(record, chunks[record.chunk_id], system_prompt)
         for record in split.val),
    )
    _write_jsonl(
        out_dir / "test.jsonl",
        (sharegpt_row(record, chunks[record.chunk_id], system_prompt)
         for record in split.test),
    )
    _write_jsonl(
        out_dir / "val_meta.jsonl",
        (_meta_row(record, chunks[record.chunk_id]) for record in split.val),
    )
    _write_jsonl(
        out_dir / "test_meta.jsonl",
        (_meta_row(record, chunks[record.chunk_id]) for record in split.test),
    )

    records = [*split.train, *split.val, *split.test]
    entity_count = sum(len(record.target.get("entities") or []) for record in records)
    relation_count = sum(
        len(record.target.get("relations") or []) for record in records
    )
    negative_count = sum(
        not (record.target.get("entities") or []) for record in records
    )
    by_origin: dict[str, int] = {}
    for record in records:
        by_origin[record.origin] = by_origin.get(record.origin, 0) + 1
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "scripts/build_distillation_dataset.py",
        "task": "FaultEvent + CAUSES evidence extraction",
        "system_prompt_sha256": hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest(),
        "seed": seed,
        "ratios": {"val": val_ratio, "test": test_ratio},
        "forced_splits": dict(sorted((forced_splits or {}).items())),
        "pinned_from": pinned_from,
        "train_mix": train_mix or {},
        "chunk_files": [str(path) for path in chunk_files],
        "label_files": [
            {"path": str(path), "origin": origin}
            for path, origin in label_files
        ],
        "assets": split.assets,
        "stats": {
            "labels": len(records),
            "train": len(split.train),
            "train_unique": len({record.chunk_id for record in split.train}),
            "val": len(split.val),
            "test": len(split.test),
            "entities": entity_count,
            "relations": relation_count,
            "negatives": negative_count,
            "by_origin": by_origin,
            "errors": len(errors),
        },
        "errors": list(errors),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
