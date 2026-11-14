"""Convert ``训练数据集.json`` into W7 corpus chunks and extraction labels.

The repository already contains a public 101-sample aviation-maintenance
fault-tree dataset.  It is not pump-domain data, but it is useful as a
format/evidence bootstrap for W7: every sample has an input record, optional
background knowledge, and a nested fault tree whose leaf nodes carry verbatim
English quotes.

This script is deliberately conservative:

* A quote is kept only when it is a contiguous substring of the chunk text
  (``input`` plus optional ``context``).
* A node without any grounded quote is dropped, together with its edges.
* Intermediate nodes may borrow the first grounded quote from a kept child;
  the report records that the quote came from a child rather than the node.
* Maintenance ``treatment`` text is never turned into a fault entity.  It is
  retained only in the conversion report so a reviewer can see what was
  excluded.

Outputs (both are git-ignored because they are generated data):

``training/corpus/parsed/chunks.jsonl``
    One row per sample: ``chunk_id``, ``asset_id``, ``text``, locator fields.
``training/data/public_labels.jsonl``
    One row per converted sample: the student target plus conversion stats.
``training/data/public_conversion_report.json``
    Counts, warnings, and per-sample errors.

Run from the repository root::

    python scripts/import_public_fault_dataset.py

Use ``--strict`` to fail on the first sample that cannot be converted without
dropping a node; the default keeps going and records the problem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.distillation.labels import (  # noqa: E402
    StudentLabelError,
    validate_student_target,
)

DEFAULT_INPUT = ROOT / "训练数据集.json"
DEFAULT_SOURCES = ROOT / "training" / "corpus" / "sources.yml"
DEFAULT_CHUNK_DIR = ROOT / "training" / "corpus" / "parsed"
DEFAULT_LABEL_DIR = ROOT / "training" / "data"
DEFAULT_SOURCE_ID = "public-aviation-fault-tree-101"

_GATE_CODES = {"AND", "OR"}


class ConversionError(RuntimeError):
    """A sample cannot be converted without violating the label contract."""


@dataclass(frozen=True)
class RawSample:
    """One row of the source dataset."""

    index: int
    instruction: str
    input_text: str
    context: str
    output: dict[str, Any]


@dataclass
class TreeNode:
    """A node in the source fault tree, before evidence repair."""

    local_id: str
    name: str
    gate: str | None
    evidence_raw: Any
    treatment: str
    node_type: str
    children: list["TreeNode"] = field(default_factory=list)
    evidence_quotes: list[str] = field(default_factory=list)
    evidence_source: str = ""
    warnings: list[str] = field(default_factory=list)
    kept: bool = True


@dataclass
class ConvertedSample:
    """The chunk and student target produced from one source sample."""

    chunk_id: str
    asset_id: str
    text: str
    target: dict[str, Any]
    warnings: list[str]
    stats: dict[str, int]


def _read_source(path: Path, source_id: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConversionError(f"cannot read sources file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConversionError(f"{path} must contain a YAML mapping")
    for entry in payload.get("sources") or []:
        if isinstance(entry, dict) and entry.get("id") == source_id:
            return entry
    raise ConversionError(f"source id {source_id!r} not found in {path}")


def _load_samples(path: Path) -> list[RawSample]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversionError(f"cannot read dataset {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise ConversionError(f"{path} must contain a JSON array")
    samples: list[RawSample] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ConversionError(f"sample {index} is not an object")
        output = item.get("output")
        if not isinstance(output, dict):
            raise ConversionError(f"sample {index} has no object output")
        samples.append(
            RawSample(
                index=index,
                instruction=str(item.get("instruction") or ""),
                input_text=str(item.get("input") or ""),
                context=str(item.get("context") or ""),
                output=output,
            )
        )
    return samples


def _build_chunk_text(sample: RawSample) -> str:
    text = sample.input_text.strip()
    context = sample.context.strip()
    if context:
        return f"{text}\n\n背景知识：\n{context}"
    return text


def _raw_quote_values(raw: Any) -> list[str]:
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [item for item in raw if isinstance(item, str)]
    else:
        return []
    return [value.strip() for value in values if value.strip()]


def _ground_candidate(candidate: str, chunk_text: str) -> str | None:
    for variant in (candidate, candidate.rstrip(".;,")):
        if variant and variant in chunk_text:
            return variant
    return None


def _ground_quotes(raw: Any, chunk_text: str) -> list[str]:
    """Return grounded quotes, preferring a grounded whole evidence value.

    The source often joins two verbatim spans with ``;``.  A whole value is
    kept when it is itself contiguous in the chunk; otherwise each part is
    tested independently and only grounded parts survive.
    """
    values = _raw_quote_values(raw)
    quotes: list[str] = []
    for value in values:
        grounded_whole = _ground_candidate(value, chunk_text)
        if grounded_whole:
            if grounded_whole not in quotes:
                quotes.append(grounded_whole)
            continue
        normalized = (
            value.replace("\n", ";").replace("...", ";").replace("…", ";")
        )
        for part in normalized.split(";"):
            grounded = _ground_candidate(part.strip(), chunk_text)
            if grounded and grounded not in quotes:
                quotes.append(grounded)
    return quotes


def _parse_tree(output: dict[str, Any]) -> TreeNode | None:
    if not str(output.get("top_event") or "").strip():
        # 37 of the 101 public samples carry the tree under ``corrected_tree``
        # instead of at the top level.
        corrected = output.get("corrected_tree")
        if isinstance(corrected, dict):
            output = corrected
    top_event = str(output.get("top_event") or "").strip()
    if not top_event:
        return None
    counter = 0

    def build(raw: dict[str, Any], *, is_root: bool = False) -> TreeNode:
        nonlocal counter
        gate_raw = raw.get("logic_gate")
        gate = str(gate_raw).upper() if gate_raw else None
        if gate not in _GATE_CODES:
            gate = None
        node = TreeNode(
            local_id=f"n{counter}",
            name=str(raw.get("node_name") or "").strip(),
            gate=gate,
            evidence_raw=raw.get("evidence"),
            treatment=str(raw.get("treatment") or ""),
            node_type=str(raw.get("type") or ""),
            warnings=[],
        )
        counter += 1
        if is_root:
            node.name = top_event
        children = raw.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    node.children.append(build(child))
        return node

    root = build(output, is_root=True)
    if not root.name:
        return None
    return root


def _repair_evidence(node: TreeNode, chunk_text: str) -> None:
    """Fill ``evidence_quotes``, preferring the node's own grounded quote."""
    if not node.name:
        # Keep the node marked as present for the parent's fallback loop; its
        # children are still visited and emitted before the node is dropped in
        # _convert_tree.
        node.evidence_quotes = []
        node.evidence_source = "missing"
        node.warnings.append("dropped-missing-name")
        return
    quotes = _ground_quotes(node.evidence_raw, chunk_text)
    if quotes:
        node.evidence_quotes = quotes
        node.evidence_source = "node"
        return
    if node.name and node.name in chunk_text:
        node.evidence_quotes = [node.name]
        node.evidence_source = "name"
        node.warnings.append("evidence-from-name")
        return

    # An intermediate node in the source tree often has no evidence field.
    # Borrow the first grounded quote from a child; this is recorded so the
    # model card and review queue can treat it as weaker supervision.
    for child in node.children:
        if child.kept and child.evidence_quotes:
            node.evidence_quotes = [child.evidence_quotes[0]]
            node.evidence_source = "child"
            node.warnings.append("evidence-from-child")
            return
    node.evidence_quotes = []
    node.evidence_source = "missing"


def _walk(node: TreeNode) -> Iterable[TreeNode]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _walk_postorder(node: TreeNode) -> Iterable[TreeNode]:
    for child in node.children:
        yield from _walk_postorder(child)
    yield node


def _entity(node: TreeNode) -> dict[str, Any]:
    return {
        "local_id": node.local_id,
        "class": "FaultEvent",
        "name": node.name,
        "description": f"故障事件：{node.name}",
        "evidence": [{"quote": quote} for quote in node.evidence_quotes],
    }


def _cause_rule(node: TreeNode) -> dict[str, Any] | None:
    kept_children = [child for child in node.children if child.kept]
    if len(kept_children) < 2:
        return None
    gate = node.gate or "OR"
    if node.gate is None:
        node.warnings.append("default-or")
    return {
        "op": gate,
        "children": [
            {"event_local_id": child.local_id} for child in kept_children
        ],
    }


def _convert_tree(node: TreeNode) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]]
]:
    """Convert a repaired tree into entities and CAUSES relations.

    The traversal is post-order, so a child is emitted before its parent and
    a parent's ``cause_rule`` only references local IDs that already exist.
    """
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []

    def visit(current: TreeNode) -> None:
        if not current.kept:
            return
        for child in current.children:
            visit(child)

        kept_children = [child for child in current.children if child.kept]
        if not current.name:
            current.kept = False
            current.warnings.append("dropped-missing-name")
            return
        if not current.evidence_quotes:
            current.kept = False
            current.warnings.append("dropped-missing-evidence")
            return

        entity = _entity(current)
        rule = _cause_rule(current)
        if rule is not None:
            entity["cause_rule"] = rule
        entities.append(entity)

        for child in kept_children:
            relation = {
                "relation_code": "CAUSES",
                "source_local_id": child.local_id,
                "target_local_id": current.local_id,
                "evidence": [
                    {"quote": quote} for quote in child.evidence_quotes
                ],
            }
            relations.append(relation)

    visit(node)
    return entities, relations


def _validate_target(
    target: dict[str, Any], chunk_text: str, *, sample_id: str
) -> None:
    try:
        validate_student_target(target, chunk_text, sample_id=sample_id)
    except StudentLabelError as exc:
        raise ConversionError(str(exc)) from exc


def convert_sample(sample: RawSample) -> ConvertedSample:
    chunk_text = _build_chunk_text(sample)
    if not chunk_text:
        raise ConversionError(f"sample {sample.index} has empty text")
    tree = _parse_tree(sample.output)
    if tree is None:
        raise ConversionError(f"sample {sample.index} has no top_event")

    # Repair bottom-up so an intermediate node can borrow a kept child's
    # grounded quote.
    for node in _walk_postorder(tree):
        _repair_evidence(node, chunk_text)

    entities, relations = _convert_tree(tree)
    warnings = [
        f"{node.local_id}:{warning}"
        for node in _walk(tree)
        for warning in node.warnings
    ]

    dropped = sum(1 for node in _walk(tree) if not node.kept)
    if not entities and dropped:
        raise ConversionError(
            f"sample {sample.index}: every node was dropped for missing evidence"
        )

    asset_id = f"PUBLIC-AVIATION-{sample.index + 1:03d}"
    target = {"entities": entities, "relations": relations}
    _validate_target(target, chunk_text, sample_id=asset_id)

    stats = {
        "entities": len(entities),
        "relations": len(relations),
        "nodes": sum(1 for _ in _walk(tree)),
        "dropped_nodes": dropped,
        "warnings": len(warnings),
    }
    return ConvertedSample(
        chunk_id=f"{asset_id}:chunk",
        asset_id=asset_id,
        text=chunk_text,
        target=target,
        warnings=warnings,
        stats=stats,
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def convert_dataset(
    samples: list[RawSample], *, strict: bool = False
) -> tuple[list[ConvertedSample], list[dict[str, Any]]]:
    converted: list[ConvertedSample] = []
    errors: list[dict[str, Any]] = []
    for sample in samples:
        try:
            result = convert_sample(sample)
            if strict and result.stats["dropped_nodes"]:
                raise ConversionError(
                    f"sample {sample.index}: dropped "
                    f"{result.stats['dropped_nodes']} node(s) for missing evidence"
                )
            converted.append(result)
        except ConversionError as exc:
            errors.append({"index": sample.index, "error": str(exc)})
            if strict:
                raise
    return converted, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--chunk-dir", type=Path, default=DEFAULT_CHUNK_DIR)
    parser.add_argument("--label-dir", type=Path, default=DEFAULT_LABEL_DIR)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    source = _read_source(args.sources.resolve(), args.source_id)
    samples = _load_samples(args.input.resolve())
    if args.limit > 0:
        samples = samples[: args.limit]

    converted, errors = convert_dataset(samples, strict=args.strict)
    if not converted:
        print(
            f"[convert] no samples converted; {len(errors)} errors",
            file=sys.stderr,
        )
        for error in errors[:10]:
            print(f"  sample {error['index']}: {error['error']}", file=sys.stderr)
        return 2

    chunk_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    source_entry = {
        "id": source.get("id"),
        "title": source.get("title"),
        "license": source.get("license"),
        "url": source.get("url"),
        "public": bool(source.get("public")),
    }
    for item in converted:
        text_sha256 = hashlib.sha256(item.text.encode("utf-8")).hexdigest()
        chunk_rows.append(
            {
                "chunk_id": item.chunk_id,
                "asset_id": item.asset_id,
                "text": item.text,
                "page": None,
                "section_path": "input+context",
                "modality": "text",
                "source_id": args.source_id,
                "source_url": source_entry["url"],
                "license": source_entry["license"],
                "text_sha256": text_sha256,
            }
        )
        label_rows.append(
            {
                "chunk_id": item.chunk_id,
                "asset_id": item.asset_id,
                "source_id": args.source_id,
                "target": item.target,
                "warnings": item.warnings,
                "stats": item.stats,
            }
        )

    chunk_path = args.chunk_dir.resolve() / "chunks.jsonl"
    label_path = args.label_dir.resolve() / "public_labels.jsonl"
    report_path = args.label_dir.resolve() / "public_conversion_report.json"
    _write_jsonl(chunk_path, chunk_rows)
    _write_jsonl(label_path, label_rows)

    total_entities = sum(item.stats["entities"] for item in converted)
    total_relations = sum(item.stats["relations"] for item in converted)
    total_nodes = sum(item.stats["nodes"] for item in converted)
    total_dropped = sum(item.stats["dropped_nodes"] for item in converted)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "scripts/import_public_fault_dataset.py",
        "source": source_entry,
        "input": str(args.input.resolve()),
        "stats": {
            "samples_read": len(samples),
            "samples_converted": len(converted),
            "samples_failed": len(errors),
            "entities": total_entities,
            "relations": total_relations,
            "nodes": total_nodes,
            "dropped_nodes": total_dropped,
        },
        "errors": errors,
        "sample_warnings": {
            item.asset_id: item.warnings for item in converted if item.warnings
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"[convert] {len(converted)}/{len(samples)} samples, "
        f"{total_entities} entities, {total_relations} relations, "
        f"{total_dropped} dropped nodes"
    )
    print(f"[convert] chunks -> {chunk_path}")
    print(f"[convert] labels -> {label_path}")
    print(f"[convert] report -> {report_path}")
    if errors:
        print(
            f"[convert] {len(errors)} samples failed; see the report",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
