"""Canonical student/teacher prompt for the W7 extraction task.

The prompt is shared by the teacher labelling CLI, the dataset builder, and
the student evaluation script so the three cannot drift.  It narrows the
ontology to ``FaultEvent`` entities and ``CAUSES`` relations only; everything
else stays in the W3 pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from lightrag.fault_tree.prompt_builder import render_domain_extraction_rules

TASK_RULES = """\
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

SUPPLEMENT_RULES = """\
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


def load_prompt_profile(path: Path) -> dict[str, Any]:
    """Read the ontology prompt profile used by the runtime extractor."""
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return payload


def build_system_prompt(profile: dict[str, Any]) -> str:
    """Render the system prompt from the industrial-fault prompt profile.

    ``profile`` is the parsed ``prompts/entity_type/industrial_fault.yml``.
    It must define the ``CAUSES`` relation; the call fails loudly otherwise
    because a missing relation would silently train the student on a
    different task than the runtime validator accepts.
    """
    relations = profile.get("relations") or {}
    causes = relations.get("CAUSES")
    if not isinstance(causes, dict):
        raise ValueError("prompt profile must define the CAUSES relation")
    rendered = render_domain_extraction_rules(
        ontology_version=str(profile.get("ontology_version") or ""),
        classes=profile.get("classes") or {},
        relations={"CAUSES": causes},
        instructions="",
    )
    return "\n\n".join([TASK_RULES, rendered, SUPPLEMENT_RULES]).strip()


def build_user_prompt(
    chunk: dict[str, Any], *, file_name: str | None = None
) -> str:
    """Build the user turn for one chunk.

    Only the chunk text and minimal locator fields are included.  Do not add
    file paths, tenant IDs, customer names, or ticket numbers: the same prompt
    is sent to the external teacher for every chunk marked ``public``.
    """
    lines = [
        f"来源文件：{file_name or chunk.get('asset_id') or 'unknown'}",
        f"片段：{chunk.get('chunk_id') or 'unknown'}",
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
