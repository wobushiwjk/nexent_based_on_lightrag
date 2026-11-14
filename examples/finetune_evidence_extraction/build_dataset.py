"""Build a small SFT dataset for fault-event extraction from the seed pack.

This is a teaching scaffold for W7, not the production dataset builder.  It
reads the four seed chunks, narrows the task to ``FaultEvent`` + ``CAUSES``,
repairs labels that violate the evidence contract, and emits LLaMA-Factory
ShareGPT JSONL.

Run from the repository root::

    .venv/Scripts/python.exe examples/finetune_evidence_extraction/build_dataset.py

The generated files land in ``examples/finetune_evidence_extraction/data``.
They are derived from the committed seed pack and are therefore safe to
rebuild at any time; the directory is git-ignored so a real teacher-labelled
dataset can replace it without touching the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.prompt_builder import (  # noqa: E402
    render_domain_extraction_rules,
)

DEFAULT_PACK = ROOT / "domain_packs" / "industrial_fault"
DEFAULT_PROMPT_PROFILE = ROOT / "prompts" / "entity_type" / "industrial_fault.yml"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "data"

_TASK_RULES = """\
本任务只抽取 FaultEvent（故障事件）和 CAUSES（原因 -> 结果）。
不要输出 Equipment、HAS_FAULT、Department、File、FaultCauseRule 或 LogicGate。
只输出一个 JSON 对象，不要输出 Markdown 代码块、解释或额外文字。

JSON 结构：
{
  "entities": [
    {
      "local_id": "chunk 内唯一的临时 ID",
      "class": "FaultEvent",
      "name": "规范故障名",
      "description": "一句话描述",
      "aliases": ["原文中出现的同义称呼"],
      "cause_rule": {"event_local_id": "..."} 或 {"op": "AND|OR", "children": [...]},
      "evidence": [{"quote": "原文逐字片段"}]
    }
  ],
  "relations": [
    {
      "relation_code": "CAUSES",
      "source_local_id": "原因 local_id",
      "target_local_id": "结果 local_id",
      "evidence": [{"quote": "原文逐字片段"}]
    }
  ]
}
"""

_SUPPLEMENT_RULES = """\
补充规则：
- CAUSES 的方向固定为原因 -> 结果。
- 多个相互独立、任一成立即可触发结果的原因使用 OR。
- 只有原文明确要求多个原因同时成立时才使用 AND。
- 原文形成 A -> B -> C 时必须保留 A -> B 和 B -> C，不能压缩成 A -> C。
- 同一故障在整段文本中使用统一的规范名称；原文同义称呼写入 aliases。
- 维修措施、检测动作、操作步骤和预防建议不是故障原因，不要抽取。
- 每个实体和关系都必须带 evidence.quote，且 quote 必须逐字复制原文片段。
- 没有原文证据的因果关系必须省略，不得引入文本外常识。
"""


@dataclass(frozen=True)
class Sample:
    """One training sample plus the text its quotes must match."""

    sample_id: str
    asset_id: str
    chunk_id: str
    chunk_text: str
    user_prompt: str
    target: dict[str, Any]


class DatasetError(RuntimeError):
    """A seed label cannot be turned into a contract-valid target."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DatasetError(f"{path} must contain a JSON object")
    return payload


def _load_prompt_profile(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DatasetError(f"cannot read prompt profile {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DatasetError(f"{path} must contain a YAML mapping")
    return payload


def _build_system_prompt(profile: dict[str, Any]) -> str:
    relations = profile.get("relations") or {}
    causes = relations.get("CAUSES")
    if not isinstance(causes, dict):
        raise DatasetError("prompt profile must define the CAUSES relation")
    rendered = render_domain_extraction_rules(
        ontology_version=str(profile.get("ontology_version") or ""),
        classes=profile.get("classes") or {},
        relations={"CAUSES": causes},
        instructions="",
    )
    return "\n\n".join([_TASK_RULES, rendered, _SUPPLEMENT_RULES]).strip()


def _user_prompt(chunk: dict[str, Any], file_info: dict[str, Any]) -> str:
    lines = [
        f"来源文件：{file_info.get('canonical_name') or chunk.get('asset_id')}",
        f"片段：{chunk.get('chunk_id')}",
    ]
    if chunk.get("page"):
        lines.append(f"页码：{chunk['page']}")
    if chunk.get("section_path"):
        lines.append(f"章节：{chunk['section_path']}")
    lines.append(f"模态：{chunk.get('modality') or 'text'}")
    lines.extend(
        [
            "",
            "请从下面的原文片段中抽取故障事件与因果关系：",
            "<<<",
            str(chunk.get("text") or ""),
            ">>>",
        ]
    )
    return "\n".join(lines)


def _quote_texts(evidence: Any) -> list[str]:
    if not isinstance(evidence, list):
        return []
    quotes: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote")
        if isinstance(quote, str) and quote.strip():
            quotes.append(quote)
    return quotes


def _clean_evidence(evidence: Any) -> list[dict[str, str]]:
    return [{"quote": quote} for quote in dict.fromkeys(_quote_texts(evidence))]


def _grounded_aliases(raw: Any, chunk_text: str) -> list[str]:
    if not isinstance(raw, list):
        return []
    aliases: list[str] = []
    for alias in raw:
        if isinstance(alias, str) and alias and alias in chunk_text:
            aliases.append(alias)
    return list(dict.fromkeys(aliases))


def _prune_expression(expression: Any, valid_ids: set[str]) -> dict[str, Any] | None:
    """Drop references to entities that did not survive evidence repair."""
    if not isinstance(expression, dict):
        return None
    event_id = expression.get("event_local_id")
    if isinstance(event_id, str) and event_id:
        return dict(expression) if event_id in valid_ids else None
    op = expression.get("op")
    children = expression.get("children")
    if op not in {"AND", "OR"} or not isinstance(children, list):
        return None
    kept = [
        child
        for child in (_prune_expression(item, valid_ids) for item in children)
        if child is not None
    ]
    if len(kept) < 2:
        return None
    return {"op": op, "children": kept}


def _prepare_chunk(
    raw: dict[str, Any],
    chunk_text: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Narrow one seed extraction to FaultEvent + CAUSES with valid quotes."""
    entities: dict[str, dict[str, Any]] = {}
    for raw_entity in raw.get("entities") or []:
        if not isinstance(raw_entity, dict):
            continue
        # The seed uses ``class``; the prompt-profile examples use ``type``.
        class_code = raw_entity.get("class") or raw_entity.get("type")
        if class_code != "FaultEvent":
            continue
        local_id = str(raw_entity.get("local_id") or "").strip()
        if not local_id or local_id in entities:
            continue
        name = str(raw_entity.get("name") or local_id)
        entity: dict[str, Any] = {
            "local_id": local_id,
            "class": "FaultEvent",
            "name": name,
            "description": str(
                raw_entity.get("description") or f"故障事件：{name}"
            ),
        }
        aliases = _grounded_aliases(raw_entity.get("aliases"), chunk_text)
        if aliases:
            entity["aliases"] = aliases
        if isinstance(raw_entity.get("cause_rule"), dict):
            entity["cause_rule"] = raw_entity["cause_rule"]
        entity["evidence"] = _clean_evidence(raw_entity.get("evidence"))
        entities[local_id] = entity

    relations: list[dict[str, Any]] = []
    quotes_by_endpoint: dict[str, list[str]] = {}
    for raw_relation in raw.get("relations") or []:
        if not isinstance(raw_relation, dict):
            continue
        if raw_relation.get("relation_code") != "CAUSES":
            continue
        source = str(raw_relation.get("source_local_id") or "").strip()
        target = str(raw_relation.get("target_local_id") or "").strip()
        if source not in entities or target not in entities:
            continue
        quotes = [
            quote
            for quote in _quote_texts(raw_relation.get("evidence"))
            if quote in chunk_text
        ]
        if not quotes:
            continue
        relation: dict[str, Any] = {
            "relation_code": "CAUSES",
            "source_local_id": source,
            "target_local_id": target,
            "evidence": [{"quote": quote} for quote in dict.fromkeys(quotes)],
        }
        gate_type = raw_relation.get("gate_type_hint")
        if gate_type in {"AND", "OR"}:
            relation["gate_type_hint"] = gate_type
        gate_key = raw_relation.get("gate_key")
        if isinstance(gate_key, str) and gate_key:
            relation["gate_key"] = gate_key
        relations.append(relation)
        for endpoint in (source, target):
            quotes_by_endpoint.setdefault(endpoint, []).extend(quotes)

    # The seed sometimes leaves an endpoint without its own quote.  Repair it
    # from the entity name, then from a touching relation, before dropping it.
    for local_id, entity in entities.items():
        if entity["evidence"]:
            continue
        if entity["name"] in chunk_text:
            entity["evidence"] = [{"quote": entity["name"]}]
        elif quotes_by_endpoint.get(local_id):
            entity["evidence"] = [
                {"quote": quote}
                for quote in dict.fromkeys(quotes_by_endpoint[local_id])
            ]

    valid_ids = {
        local_id
        for local_id, entity in entities.items()
        if entity["evidence"]
        and all(quote["quote"] in chunk_text for quote in entity["evidence"])
    }
    for local_id in list(entities):
        if local_id not in valid_ids:
            del entities[local_id]

    kept_relations: list[dict[str, Any]] = []
    for relation in relations:
        if (
            relation["source_local_id"] not in valid_ids
            or relation["target_local_id"] not in valid_ids
        ):
            continue
        if all(
            item["quote"] in chunk_text for item in relation["evidence"]
        ):
            kept_relations.append(relation)

    for entity in entities.values():
        if "cause_rule" in entity:
            pruned = _prune_expression(entity["cause_rule"], valid_ids)
            if pruned is None:
                entity.pop("cause_rule", None)
            else:
                entity["cause_rule"] = pruned

    return entities, kept_relations


def _measures_negative(document: Path) -> str:
    """Return the maintenance-measures section, which mentions no fault event."""
    try:
        text = document.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = "## 4.3 处理措施"
    if marker not in text:
        return ""
    section = text.split(marker, 1)[1]
    lines: list[str] = []
    for line in section.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _iter_quotes(target: dict[str, Any]) -> Iterable[str]:
    for entity in target.get("entities") or []:
        for item in entity.get("evidence") or []:
            yield item["quote"]
    for relation in target.get("relations") or []:
        for item in relation.get("evidence") or []:
            yield item["quote"]


def build_samples(
    pack: Path, prompt_profile: Path
) -> tuple[list[Sample], str]:
    profile = _load_prompt_profile(prompt_profile)
    system_prompt = _build_system_prompt(profile)
    extraction_dir = pack / "seeds" / "extraction"
    context = _read_json(extraction_dir / "context.json")
    files = {
        str(item.get("asset_uid")): item
        for item in context.get("files") or []
        if isinstance(item, dict)
    }
    # Seed file names do not follow chunk IDs (``manual.json`` holds
    # ``manual-4.2``), so map by the chunk_id inside the payload.
    extractions: dict[str, dict[str, Any]] = {}
    for name in context.get("extraction_files") or []:
        raw = _read_json(extraction_dir / str(name))
        raw_chunk_id = str(raw.get("chunk_id") or "")
        if raw_chunk_id:
            extractions[raw_chunk_id] = raw

    samples: list[Sample] = []
    for chunk in context.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or "")
        asset_id = str(chunk.get("asset_id") or "")
        chunk_text = str(chunk.get("text") or "")
        raw = extractions.get(chunk_id)
        if raw is None:
            continue
        entities, relations = _prepare_chunk(raw, chunk_text)
        target = {
            "entities": list(entities.values()),
            "relations": relations,
        }
        user = _user_prompt(chunk, files.get(asset_id, {}))
        samples.append(
            Sample(
                sample_id=f"{asset_id}:{chunk_id}",
                asset_id=asset_id,
                chunk_id=chunk_id,
                chunk_text=chunk_text,
                user_prompt=user,
                target=target,
            )
        )

    negative_text = _measures_negative(
        pack / "seeds" / "documents" / "pump_manual_excerpt.md"
    )
    if negative_text:
        negative_chunk = {
            "asset_id": "FILE-P101A-MANUAL",
            "chunk_id": "manual-4.3-measures",
            "text": negative_text,
            "page": None,
            "section_path": "4.3 处理措施",
            "modality": "text",
        }
        samples.append(
            Sample(
                sample_id="FILE-P101A-MANUAL:manual-4.3-measures",
                asset_id="FILE-P101A-MANUAL",
                chunk_id="manual-4.3-measures",
                chunk_text=negative_text,
                user_prompt=_user_prompt(
                    negative_chunk, files.get("FILE-P101A-MANUAL", {})
                ),
                target={"entities": [], "relations": []},
            )
        )

    for sample in samples:
        for quote in _iter_quotes(sample.target):
            if quote not in sample.chunk_text:
                raise DatasetError(
                    f"{sample.sample_id}: quote is not verbatim: {quote!r}"
                )
    return samples, system_prompt


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _split_samples(
    samples: list[Sample], val_assets: set[str]
) -> tuple[list[Sample], list[Sample]]:
    train = [sample for sample in samples if sample.asset_id not in val_assets]
    val = [sample for sample in samples if sample.asset_id in val_assets]
    if not val and len(samples) > 1:
        val = [samples[-1]]
        train = samples[:-1]
    if not train:
        raise DatasetError("no training samples left after the split")
    return train, val


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    parser.add_argument(
        "--prompt-profile", type=Path, default=DEFAULT_PROMPT_PROFILE
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--val-assets",
        default="FILE-P101A-INSPECTION",
        help="Comma-separated asset UIDs held out for evaluation.",
    )
    args = parser.parse_args(argv)

    samples, system_prompt = build_samples(
        args.pack.resolve(), args.prompt_profile.resolve()
    )
    val_assets = {
        item.strip() for item in args.val_assets.split(",") if item.strip()
    }
    train, val = _split_samples(samples, val_assets)

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    def rows(items: list[Sample]) -> list[dict[str, Any]]:
        return [
            {
                "id": sample.sample_id,
                "system": system_prompt,
                "conversations": [
                    {"from": "human", "value": sample.user_prompt},
                    {
                        "from": "gpt",
                        "value": json.dumps(
                            sample.target,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
            }
            for sample in items
        ]

    _write_jsonl(out_dir / "train.jsonl", rows(train))
    _write_jsonl(out_dir / "val.jsonl", rows(val))
    _write_jsonl(
        out_dir / "val_meta.jsonl",
        [
            {
                "id": sample.sample_id,
                "chunk_text": sample.chunk_text,
                "gold": sample.target,
            }
            for sample in val
        ],
    )

    entity_count = sum(
        len(sample.target["entities"]) for sample in samples
    )
    relation_count = sum(
        len(sample.target["relations"]) for sample in samples
    )
    try:
        pack_label = str(args.pack.resolve().relative_to(ROOT))
    except ValueError:
        pack_label = str(args.pack.resolve())
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "examples/finetune_evidence_extraction/build_dataset.py",
        "pack": pack_label,
        "task": "FaultEvent + CAUSES evidence extraction",
        "system_prompt_sha256": hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest(),
        "split": {
            "train": [sample.sample_id for sample in train],
            "val": [sample.sample_id for sample in val],
        },
        "stats": {
            "samples": len(samples),
            "train": len(train),
            "val": len(val),
            "entities": entity_count,
            "relations": relation_count,
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"[build] {len(train)} train / {len(val)} val samples, "
        f"{entity_count} entities, {relation_count} relations"
    )
    print(f"[build] wrote {out_dir}")
    print(f"[build] system prompt sha256 {manifest['system_prompt_sha256'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
