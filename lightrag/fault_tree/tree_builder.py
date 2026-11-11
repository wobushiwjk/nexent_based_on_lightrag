"""
tree_builder.py 的作用是：从一个顶事件出发，沿着已有的 FaultCauseRule 递归向下展开，
生成一棵 FaultTreeDraft。

输入是：
实体列表：其中要有作为顶事件的 FaultEvent；
因果规则列表：每条规则说明什么原因通过什么逻辑导致哪个结果故障；
顶事件 UID；
树的版本、审核状态等元数据。
输出是由故障事件、规则、逻辑门及关系边组成的 FaultTreeDraft，并附上结构校验结果。

它不负责抽取原文、保存 SQLite 或写图数据库。

简化后的整体执行顺序：
build_fault_tree_draft
  → 检查顶事件
  → visit 顶事件
      → _select_rule
      → plan_gate 规划 AND/OR 门
      → visit 原因事件
  → 创建事件、规则、逻辑门节点
  → emit_gate 生成门之间的边
  → 创建 FaultTreeDraft
  → validate_fault_tree_draft 校验
  → 返回带校验结果的草案
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from lightrag.fault_tree.contracts import (
    EntityRecord,
    FaultCauseRule,
    FaultEvent,
    FaultTreeDraft,
    FaultTreeEdge,
    FaultTreeNode,
    UidExpression,
    ValidationIssue,
    ValidationResult,
)
from lightrag.fault_tree.domain_validation import (
    errors_only,
    validate_fault_tree_draft,
)

#构建故障树失败时使用的基础异常。
class TreeBuildError(ValueError):
    """Base error for a tree that cannot be projected from the rules."""

#同一个结果故障有多条规则，却没有指定用哪一条时抛出。
class AmbiguousRuleError(TreeBuildError):
    """More than one rule produces the same effect and no rule is selected."""

#顶事件不是已投影的故障事件，或规则输入不是故障事件时抛出。
class MissingRuleError(TreeBuildError):
    """The requested top event has no causal rule."""

#记录一个逻辑门输入是故障事件还是另一个子门。
@dataclass(frozen=True)
class _GateInput:
    uid: str
    kind: Literal["event", "gate"]

#暂存待生成的逻辑门，包括所属规则、门类型、输入、输出和表达式位置。
@dataclass(frozen=True)
class _PlannedGate:
    uid: str
    rule_uid: str
    gate_type: str
    effect_uid: str
    output_uid: str
    expression_path: str
    inputs: tuple[_GateInput, ...]
    is_root: bool

#build_fault_tree_draft 是主入口，负责组织整棵树的生成。

# 先整理输入并确认顶事件
# 函数先把实体整理成 uid → 实体 的字典，并按结果故障 UID 给规则分组。
# 顶事件必须是 FaultEvent，否则抛出 MissingRuleError。
# 同一个故障的多条规则按规则 UID 排序，方便结果保持稳定。
# 然后定义三个内部函数
# 它们只在 build_fault_tree_draft 运行期间使用。

# 最后组装和校验树
# 完成遍历后，主函数依次创建：
# 故障事件节点：使用实体 UID、名称和严重程度；
# 规则节点：使用规则 UID 和结果故障名称；
# 逻辑门节点：记录门类型、所属规则、输入数和嵌套位置；
# 边：每条规则先有 HAS_CAUSE_RULE，门和原因事件之间再用 INPUT_OF、OUTPUT_TO 连接。
# 随后创建 FaultTreeDraft，调用 validate_fault_tree_draft 校验树结构。
# 最终的 validation.valid 根据是否发现 error 设置；
# warning 和 error 都会保存在 validation.issues 中。
def build_fault_tree_draft(
    *,
    tenant_id: str,
    ontology_version: str,
    entities: Iterable[EntityRecord],
    rules: Iterable[FaultCauseRule],
    top_event_uid: str,
    draft_id: str | None = None,
    version: int = 1,
    review_status: str = "proposed",
    created_by: str = "projection",
    created_at: datetime | None = None,
) -> FaultTreeDraft:
    """Build one tree by walking rules downward from ``top_event_uid``.

    Event nodes appear in discovery order, followed by rules, followed by
    gates in pre-order.  Edges are emitted rule by rule: ``HAS_CAUSE_RULE``,
    then for each gate ``OUTPUT_TO`` followed by ``INPUT_OF`` for every input.
    A nested gate is emitted immediately after the ``INPUT_OF`` edge that
    feeds its parent.  That order is what makes a rebuilt one-level draft
    byte-comparable with the reviewed seed tree.
    """

    entity_index = {entity.uid: entity for entity in entities}
    rule_list = list(rules)
    top = entity_index.get(top_event_uid)
    if not isinstance(top, FaultEvent):
        raise MissingRuleError(
            f"top event {top_event_uid!r} is not a projected FaultEvent"
        )

    rules_by_effect: dict[str, list[FaultCauseRule]] = defaultdict(list)
    for rule in rule_list:
        rules_by_effect[rule.effect_fault_uid].append(rule)
    for group in rules_by_effect.values():
        group.sort(key=lambda item: item.rule_uid)

    expanded_events: set[str] = set()
    expanded_rules: set[str] = set()
    event_order: list[str] = []
    rule_order: list[FaultCauseRule] = []
    gate_order: list[str] = []
    planned_gates: dict[str, _PlannedGate] = {}
    gate_by_rule: dict[str, _PlannedGate] = {}
    used_gate_uids: set[str] = set()
    warnings: list[ValidationIssue] = []

    #plan_gate 把一个 AND/OR 表达式递归转换成临时门结构。
    #它返回：
    # 规划好的门；
    # 按表达式顺序收集的故障事件叶子 UID。
    def plan_gate(
        expression: UidExpression,
        *,
        rule: FaultCauseRule,
        output_uid: str,
        path: str,
    ) -> tuple[_PlannedGate, list[str]]:
        """Create this gate and its nested gates; return event leaves in order."""

        if expression.op is None:
            raise TreeBuildError(
                f"rule {rule.rule_uid!r} has a non-operator expression at "
                f"path {path or 'root'!r}"
            )
        gate_uid = _gate_uid(rule.rule_uid, expression.op, used_gate_uids)
        used_gate_uids.add(gate_uid)
        gate_order.append(gate_uid)

        inputs: list[_GateInput] = []
        event_uids: list[str] = []
        for index, child in enumerate(expression.children):
            child_path = f"{index}" if path == "" else f"{path}.{index}"
            if child.op is None:
                event_uid = child.event_uid or ""
                inputs.append(_GateInput(uid=event_uid, kind="event"))
                event_uids.append(event_uid)
                continue
            child_gate, child_events = plan_gate(
                child,
                rule=rule,
                output_uid=gate_uid,
                path=child_path,
            )
            inputs.append(_GateInput(uid=child_gate.uid, kind="gate"))
            event_uids.extend(child_events)

        gate = _PlannedGate(
            uid=gate_uid,
            rule_uid=rule.rule_uid,
            gate_type=expression.op,
            effect_uid=rule.effect_fault_uid,
            output_uid=output_uid,
            expression_path=path,
            inputs=tuple(inputs),
            is_root=path == "",
        )
        planned_gates[gate_uid] = gate
        return gate, event_uids

    #从顶事件向原因递归遍历
    # 检查当前故障是否出现在祖先路径中；如果出现，说明因果链成环，抛出 TreeBuildError。
    # 记录该故障事件；同一个事件不重复展开。
    # 调用 _select_rule 为当前故障选规则。
    # 如果没有规则，当前故障作为叶子结束递归。
    # 如果有 AND/OR 规则，调用 plan_gate 规划门，再递归访问表达式里的每个原因事件。
    def visit(event_uid: str, path: tuple[str, ...]) -> None:
        if event_uid in path:
            raise TreeBuildError(
                "causal expression contains a cycle: "
                + " -> ".join((*path, event_uid))
            )
        if event_uid in expanded_events:
            return
        expanded_events.add(event_uid)
        event_order.append(event_uid)
        rule = _select_rule(
            event_uid,
            entity_index=entity_index,
            rules_by_effect=rules_by_effect,
        )
        if rule is None:
            return
        if rule.rule_uid in expanded_rules:
            return
        expanded_rules.add(rule.rule_uid)
        if not isinstance(rule.expression, UidExpression):
            raise TreeBuildError(
                f"rule {rule.rule_uid!r} has an invalid expression"
            )
        if rule.expression.op is None:
            # A leaf rule has no gate.  The direct CAUSES edge is emitted
            # below so the tree still records the causal relation.
            visit(rule.expression.event_uid or "", (*path, event_uid))
            return
        root_gate, event_uids = plan_gate(
            rule.expression,
            rule=rule,
            output_uid=event_uid,
            path="",
        )
        gate_by_rule[rule.rule_uid] = root_gate
        rule_order.append(rule)
        for input_uid in event_uids:
            visit(input_uid, (*path, event_uid))

    visit(top_event_uid, ())

    if not event_order:
        raise MissingRuleError(f"top event {top_event_uid!r} produced no nodes")

    gates = [planned_gates[uid] for uid in gate_order]

    nodes: list[FaultTreeNode] = []
    for event_uid in event_order:
        event = entity_index.get(event_uid)
        if not isinstance(event, FaultEvent):
            raise MissingRuleError(
                f"rule input {event_uid!r} is not a projected FaultEvent"
            )
        nodes.append(
            FaultTreeNode(
                uid=event.uid,
                class_code="FaultEvent",
                label=event.canonical_name,
                properties={"severity": event.severity},
            )
        )

    for rule in rule_order:
        effect = entity_index.get(rule.effect_fault_uid)
        effect_name = (
            effect.canonical_name
            if isinstance(effect, FaultEvent)
            else rule.effect_fault_uid
        )
        gate = gate_by_rule.get(rule.rule_uid)
        if gate is not None:
            label = f"{effect_name}的 {gate.gate_type} 规则"
        else:
            label = f"{effect_name}的因果规则"
        nodes.append(
            FaultTreeNode(
                uid=rule.rule_uid,
                class_code="FaultCauseRule",
                label=label,
                properties={"effect_fault_uid": rule.effect_fault_uid},
            )
        )

    for gate in gates:
        properties: dict[str, object] = {
            "gate_type": gate.gate_type,
            "rule_uid": gate.rule_uid,
            "input_count": len(gate.inputs),
        }
        if not gate.is_root:
            properties["expression_path"] = gate.expression_path
        nodes.append(
            FaultTreeNode(
                uid=gate.uid,
                class_code="LogicGate",
                label=f"{gate.gate_type} 门",
                properties=properties,
            )
        )

    edges: list[FaultTreeEdge] = []

    # emit_gate 将一个规划门变成 FaultTreeEdge：
    # 门连向它的输出目标：OUTPUT_TO；
    # 每个输入连向该门：INPUT_OF；
    # 如果输入本身是子门，就递归生成子门的边。
    # 所以嵌套规则会用“子门输出到父门”，而不是把每个子门都直接连到故障事件。
    def emit_gate(gate: _PlannedGate) -> None:
        edges.append(
            FaultTreeEdge(
                source_uid=gate.uid,
                target_uid=gate.output_uid,
                relation_code="OUTPUT_TO",
            )
        )
        for gate_input in gate.inputs:
            edges.append(
                FaultTreeEdge(
                    source_uid=gate_input.uid,
                    target_uid=gate.uid,
                    relation_code="INPUT_OF",
                )
            )
            if gate_input.kind == "gate":
                emit_gate(planned_gates[gate_input.uid])

    for rule in rule_order:
        edges.append(
            FaultTreeEdge(
                source_uid=rule.effect_fault_uid,
                target_uid=rule.rule_uid,
                relation_code="HAS_CAUSE_RULE",
            )
        )
        gate = gate_by_rule.get(rule.rule_uid)
        if gate is not None:
            emit_gate(gate)
        else:
            event_uid = rule.expression.event_uid
            if event_uid:
                edges.append(
                    FaultTreeEdge(
                        source_uid=event_uid,
                        target_uid=rule.effect_fault_uid,
                        relation_code="CAUSES",
                    )
                )

    draft = FaultTreeDraft(
        draft_id=draft_id or f"TREE-{top_event_uid}-v{version}",
        tenant_id=tenant_id,
        top_event_uid=top_event_uid,
        nodes=nodes,
        edges=edges,
        validation=ValidationResult(valid=True, issues=[]),
        version=version,
        review_status=review_status,  # type: ignore[arg-type]
        ontology_version=ontology_version,
        created_at=created_at or datetime.now(timezone.utc),
        created_by=created_by,
    )
    issues = validate_fault_tree_draft(
        draft,
        {rule.rule_uid: rule for rule in rule_list},
        entity_index,
    )
    issues = [*warnings, *issues]
    errors = errors_only(issues)
    return draft.model_copy(
        update={
            "validation": ValidationResult(
                valid=not errors,
                issues=issues,
            )
        }
    )

#_select_rule 为当前结果故障选择一条因果规则：

# 没有关联规则：返回 None；
# 恰好一条：直接返回；
# 多条规则：尝试使用该故障实体上的 active_cause_rule_uid 选中一条；
# 多条且没有有效的 active 规则：抛出 AmbiguousRuleError。
def _select_rule(
    event_uid: str,
    *,
    entity_index: Mapping[str, EntityRecord],
    rules_by_effect: Mapping[str, list[FaultCauseRule]],
) -> FaultCauseRule | None:
    candidates = list(rules_by_effect.get(event_uid, ()))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    entity = entity_index.get(event_uid)
    active = (
        getattr(entity, "active_cause_rule_uid", None)
        if isinstance(entity, FaultEvent)
        else None
    )
    if active:
        for rule in candidates:
            if rule.rule_uid == active:
                return rule
    raise AmbiguousRuleError(
        f"event {event_uid!r} has {len(candidates)} causal rules "
        f"({', '.join(rule.rule_uid for rule in candidates)}); set "
        "active_cause_rule_uid on the FaultEvent or build the tree with an "
        "explicit rule selector"
    )

#_gate_uid 根据规则 UID 和门类型生成门 UID。
def _gate_uid(
    rule_uid: str,
    gate_type: str,
    used: set[str],
) -> str:
    key = rule_uid
    if key.upper().startswith("RULE-"):
        key = key[5:]
    suffix = f"-{gate_type.upper()}"
    if key.upper().endswith(suffix):
        key = key[: -len(suffix)]
    candidate = f"GATE-{gate_type.upper()}-{key}" if key else f"GATE-{gate_type.upper()}"
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-")
    if candidate not in used:
        return candidate
    index = 2
    while f"{candidate}-{index}" in used:
        index += 1
    return f"{candidate}-{index}"


__all__ = [
    "AmbiguousRuleError",
    "MissingRuleError",
    "TreeBuildError",
    "build_fault_tree_draft",
]
