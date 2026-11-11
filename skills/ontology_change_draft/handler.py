"""
“本体变更草案”Skill 的确定性实现。
它读取当前本体，检查用户提出的变更是否冲突，并返回：
    当前版本与建议版本；
    变更 diff；
    阻断或非阻断冲突；
    受影响的类、关系和 Schema；
    关联的来源证据；
    后续人工审核清单。
它只生成草案，不写入或发布本体。返回的 review.applied 始终为 false；真正的审核、发布和数据迁移属于 W8。

一次调用的执行链：
    run()
 ├─ 验证 OntologyChangeRequest
 ├─ MCP get_ontology
 ├─ _OntologySnapshot.from_payload()
 ├─ _evaluate()
 │   ├─ 按 change_type 调用对应评估函数
 │   └─ 生成 diff、conflicts、impact
 ├─ _propose_version()
 ├─ 对 source_targets 调用 MCP trace_evidence
 └─ success_response() / failure_response()
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from skills.runtime import (
    EvidenceRef,
    SkillContext,
    SkillErrorPayload,
    SkillExecutionError,
    SkillResponse,
    ToolClient,
    collect_evidence,
    error_from_envelope,
    failure_response,
    require_data,
    success_response,
)

SKILL_NAME = "ontology_change_draft"
SKILL_VERSION = "0.1.0"
SKILL_TOOLS = ("get_ontology", "trace_evidence")

SourceTargetType = Literal[
    "entity", "fact", "rule", "logic_gate", "decision_step"
]

#下面四个模型分别定义一种变更类型。
# 它们都设置 extra="forbid"，即不接受模型未声明的额外字段。

#新增本体类。包括类代码、标签、可选父类、必需字段、是否对提示词可见及相关标准。
class AddClassChange(BaseModel):
    """Propose a new ontology class."""

    model_config = ConfigDict(extra="forbid")

    change_type: Literal["add_class"]
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)
    parent: str | None = None
    required_fields: list[str] = Field(default_factory=list)
    prompt_visible: bool = True
    standards: list[str] = Field(default_factory=list)

#新增关系。要求至少有一个源类型和目标类型，并定义关系是否有向、是否必须有证据。
class AddRelationChange(BaseModel):
    """Propose a new ontology relation."""

    model_config = ConfigDict(extra="forbid")

    change_type: Literal["add_relation"]
    code: str = Field(min_length=1)
    source_types: list[str] = Field(min_length=1)
    target_types: list[str] = Field(min_length=1)
    directed: bool = True
    evidence_required: bool = True

#新增约束。要求约束 ID、适用类和规则文本；严重程度为 error 或 warning。
class AddConstraintChange(BaseModel):
    """Propose a new ontology constraint."""

    model_config = ConfigDict(extra="forbid")

    change_type: Literal["add_constraint"]
    id: str = Field(min_length=1)
    applies_to: list[str] = Field(min_length=1)
    rule: str = Field(min_length=1)
    severity: Literal["error", "warning"] = "error"

#废弃一个类，可选指定替代类。
class DeprecateClassChange(BaseModel):
    """Propose deprecating an existing ontology class."""

    model_config = ConfigDict(extra="forbid")

    change_type: Literal["deprecate_class"]
    code: str = Field(min_length=1)
    replacement: str | None = None

#是这四种请求模型的判别联合：由 change_type 字段决定具体使用哪个模型。
OntologyChange = Annotated[
    AddClassChange
    | AddRelationChange
    | AddConstraintChange
    | DeprecateClassChange,
    Field(discriminator="change_type"),
]

#表示支持变更动机的领域记录，包含目标类型和目标 ID。
class SourceTarget(BaseModel):
    """A domain record that motivates the change."""

    model_config = ConfigDict(extra="forbid")

    target_type: SourceTargetType
    target_id: str = Field(min_length=1)

#Skill 的完整输入，包括租户 ID、变更内容、非空动机和可选的来源目标。
class OntologyChangeRequest(BaseModel):
    """Input contract for the ontology-change-draft skill."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    change: OntologyChange
    rationale: str = Field(min_length=1)
    source_targets: list[SourceTarget] = Field(default_factory=list)

#下面8个为结果模型

#一条冲突记录，包含错误码、说明和是否阻断。
class Conflict(BaseModel):
    """One blocking or non-blocking conflict found during evaluation."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    blocking: bool

#diff 中的一项，记录变更类别、代码和定义内容。
class DiffEntry(BaseModel):
    """One readable diff line."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["class", "relation", "constraint"]
    code: str
    definition: dict[str, Any] = Field(default_factory=dict)

#把 diff 分为新增、移除和修改三组。
class OntologyDiff(BaseModel):
    """Added, removed and modified definitions."""

    model_config = ConfigDict(extra="forbid")

    added: list[DiffEntry] = Field(default_factory=list)
    removed: list[DiffEntry] = Field(default_factory=list)
    modified: list[DiffEntry] = Field(default_factory=list)

#记录影响范围，包括受影响的类、关系、Schema、是否需要实例迁移和补充说明。
class Impact(BaseModel):
    """What the change touches downstream."""

    model_config = ConfigDict(extra="forbid")

    affected_classes: list[str] = Field(default_factory=list)
    affected_relations: list[str] = Field(default_factory=list)
    affected_schemas: list[str] = Field(default_factory=list)
    instance_migration_required: bool = False
    notes: list[str] = Field(default_factory=list)

#ReviewInfo：记录审核要求、审核清单和后续工作流。默认要求审核，且 applied=False、next_workflow="W8"。
class ReviewInfo(BaseModel):
    """The human review gate that must run after this draft."""

    model_config = ConfigDict(extra="forbid")

    required: bool = True
    applied: bool = False
    checklist: list[str] = Field(default_factory=list)
    next_workflow: str = "W8"

#整合草案的版本、变更、diff、冲突、影响、来源证据和审核信息。
class OntologyDraftData(BaseModel):
    """Draft payload returned to the agent and the review UI."""

    model_config = ConfigDict(extra="forbid")

    current_version: str
    proposed_version: str
    change: dict[str, Any]
    diff: OntologyDiff
    conflicts: list[Conflict] = Field(default_factory=list)
    impact: Impact
    source_evidence: list[EvidenceRef] = Field(default_factory=list)
    review: ReviewInfo

#继承通用 Skill 响应信封，并将 data 定义为 OntologyDraftData。
class OntologyDraftResponse(SkillResponse):
    """Skill envelope with a typed ontology-diff payload."""

    data: OntologyDraftData | None = None

#告诉 Schema 生成器分别使用哪个输入和输出模型。
REQUEST_MODEL = OntologyChangeRequest
RESPONSE_MODEL = OntologyDraftResponse

# run 是外部调用入口，按顺序完成以下工作：

# 验证请求：将传入字典转成 OntologyChangeRequest。验证失败时，返回 invalid_input 和 invalid_argument，并且不会调用 MCP。
# 建立执行上下文：创建 SkillContext，记录 Skill 版本和租户。
# 读取当前本体：调用 get_ontology，再由 _OntologySnapshot.from_payload() 转成本地快照。
# 评估变更：调用 _evaluate()，生成变更 diff、冲突和影响分析。
# 建议版本号：调用 _propose_version()；无法解析版本时，把警告加入响应。
# 收集来源证据：对每个 source_targets 调用 trace_evidence。查到的证据按 ID 整理；来源查询失败时添加警告，但草案仍可继续生成。
# 确定草案状态：有阻断冲突则为 rejected，否则为 draft。这两种状态表示评估已完成，因此仍返回 ok=true。
# 生成响应：附上 diff、影响、证据和审核信息。捕获到 SkillExecutionError 时，返回结构化错误；not_found 会映射为 not_found 状态，其他错误映射为 failed。
# 这里的 rejected 与执行失败不同：前者表示“草案评估成功，但发现阻断冲突”；后者表示 Skill 没能完成评估。
async def run(
    request: OntologyChangeRequest | Mapping[str, Any],
    tools: ToolClient,
) -> OntologyDraftResponse:
    """Evaluate one proposed ontology change without applying it."""

    try:
        req = (
            request
            if isinstance(request, OntologyChangeRequest)
            else OntologyChangeRequest.model_validate(request)
        )
    except ValidationError as exc:
        ctx = SkillContext(
            tools,
            skill=SKILL_NAME,
            version=SKILL_VERSION,
            tenant_id=None,
        )
        return failure_response(
            OntologyDraftResponse,
            ctx,
            status="invalid_input",
            error=SkillErrorPayload(
                code="invalid_argument",
                message=(
                    "request does not match the ontology-change-draft input "
                    "schema"
                ),
                details={"errors": _validation_errors(exc)},
            ),
        )

    ctx = SkillContext(
        tools,
        skill=SKILL_NAME,
        version=SKILL_VERSION,
        tenant_id=req.tenant_id,
    )

    try:
        ontology_envelope = await ctx.call("get_ontology", {})
        ontology = require_data(ontology_envelope, "get_ontology")
        snapshot = _OntologySnapshot.from_payload(ontology)
        evaluation = _evaluate(req.change, snapshot)
        proposed_version, version_warning = _propose_version(
            snapshot.version
        )
        if version_warning:
            ctx.warning(version_warning)

        source_evidence: dict[str, EvidenceRef] = {}
        for target in req.source_targets:
            envelope = await ctx.call(
                "trace_evidence",
                {
                    "tenant_id": req.tenant_id,
                    "target_type": target.target_type,
                    "target_id": target.target_id,
                },
            )
            if envelope.get("ok"):
                data = require_data(envelope, "trace_evidence")
                for ref in collect_evidence(data.get("evidence")):
                    source_evidence[ref.evidence_id] = ref
            else:
                error = error_from_envelope(
                    envelope,
                    fallback_message="source evidence lookup failed",
                )
                ctx.warning(
                    f"来源记录 {target.target_type}:{target.target_id} 无法追溯"
                    f"（{error.code}），草案仍生成但缺少该来源证据。"
                )
        if not source_evidence and req.source_targets:
            ctx.warning(
                "所有 source_targets 都没有返回逐字证据；请先补证据再进入人工审核。"
            )

        blocking = [item for item in evaluation.conflicts if item.blocking]
        status = "rejected" if blocking else "draft"
        data = OntologyDraftData(
            current_version=snapshot.version,
            proposed_version=proposed_version,
            change=req.change.model_dump(mode="json"),
            diff=evaluation.diff,
            conflicts=evaluation.conflicts,
            impact=evaluation.impact,
            source_evidence=sorted(
                source_evidence.values(),
                key=lambda ref: (ref.evidence_id,),
            ),
            review=ReviewInfo(
                required=True,
                applied=False,
                checklist=_REVIEW_CHECKLIST,
                next_workflow="W8",
            ),
        )
        return success_response(
            OntologyDraftResponse,
            ctx,
            status=status,
            data=data.model_dump(mode="json"),
        )
    except SkillExecutionError as exc:
        return failure_response(
            OntologyDraftResponse,
            ctx,
            status="not_found" if exc.code == "not_found" else "failed",
            error=SkillErrorPayload(
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
        )

#将 MCP 返回的本体整理成适合查重和关联检查的内存结构，分别保存版本、类、关系和约束。
class _OntologySnapshot:
    def __init__(
        self,
        version: str,
        classes: dict[str, dict[str, Any]],
        relations: dict[str, dict[str, Any]],
        constraints: dict[str, dict[str, Any]],
    ) -> None:
        self.version = version
        self.classes = classes
        self.relations = relations
        self.constraints = constraints

    #从 MCP 返回数据创建快照。缺少有效 ontology_version 时抛出 upstream_error；
    # 类和关系按 code 建索引，约束按 id 建索引，实际整理由 _by_code() 完成。
    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "_OntologySnapshot":
        version = payload.get("ontology_version")
        if not isinstance(version, str) or not version:
            raise SkillExecutionError(
                "upstream_error",
                "get_ontology returned no ontology_version",
                {"tool": "get_ontology"},
            )
        return cls(
            version=version,
            classes=_by_code(payload.get("classes")),
            relations=_by_code(payload.get("relations")),
            constraints=_by_code(payload.get("constraints"), key="id"),
        )

#用来累积一次变更评估的 diff、冲突和影响。
class _Evaluation:
    def __init__(self) -> None:
        self.diff = OntologyDiff()
        self.conflicts: list[Conflict] = []
        self.impact = Impact()

    #将一条冲突追加到评估结果，并记录它是否会阻断草案。
    def add_conflict(
        self,
        code: str,
        message: str,
        *,
        blocking: bool,
    ) -> None:
        self.conflicts.append(
            Conflict(code=code, message=message, blocking=blocking)
        )

#创建 _Evaluation，根据变更模型类型调用对应的检查函数，最后返回汇总结果：
# 新增类 → _evaluate_add_class()
# 新增关系 → _evaluate_add_relation()
# 新增约束 → _evaluate_add_constraint()
# 废弃类 → _evaluate_deprecate_class()
def _evaluate(
    change: OntologyChange,
    snapshot: _OntologySnapshot,
) -> _Evaluation:
    result = _Evaluation()
    if isinstance(change, AddClassChange):
        _evaluate_add_class(change, snapshot, result)
    elif isinstance(change, AddRelationChange):
        _evaluate_add_relation(change, snapshot, result)
    elif isinstance(change, AddConstraintChange):
        _evaluate_add_constraint(change, snapshot, result)
    else:
        _evaluate_deprecate_class(change, snapshot, result)
    return result

#检查类代码是否已存在、父类是否存在。
# 没有声明必需字段时加一条非阻断警告类冲突。
# 无论是否冲突，都会生成新增类的 diff 和对应的 Schema/提示词影响说明。
def _evaluate_add_class(
    change: AddClassChange,
    snapshot: _OntologySnapshot,
    result: _Evaluation,
) -> None:
    if change.code in snapshot.classes:
        result.add_conflict(
            "class_exists",
            f"class {change.code!r} already exists",
            blocking=True,
        )
    if change.parent and change.parent not in snapshot.classes:
        result.add_conflict(
            "unknown_parent",
            f"parent class {change.parent!r} is not defined",
            blocking=True,
        )
    if not change.required_fields:
        result.add_conflict(
            "empty_required_fields",
            f"class {change.code!r} declares no required fields",
            blocking=False,
        )
    result.diff.added.append(
        DiffEntry(
            kind="class",
            code=change.code,
            definition=change.model_dump(mode="json", exclude_none=True),
        )
    )
    result.impact = Impact(
        affected_classes=[change.code],
        affected_schemas=[
            f"schemas/entity.schema.json#/$defs/{change.code}"
        ],
        notes=[
            "Regenerate JSON Schemas and update the ontology prompt profile.",
            "Existing instances are unaffected until the class is published.",
        ],
    )

#检查关系是否已存在，源类型和目标类型是否都是已知类。
# 若关系不要求证据，添加非阻断冲突提示；
# 同时生成关系 diff 和受影响类/关系信息。
def _evaluate_add_relation(
    change: AddRelationChange,
    snapshot: _OntologySnapshot,
    result: _Evaluation,
) -> None:
    if change.code in snapshot.relations:
        result.add_conflict(
            "relation_exists",
            f"relation {change.code!r} already exists",
            blocking=True,
        )
    unknown = sorted(
        {
            code
            for code in [*change.source_types, *change.target_types]
            if code not in snapshot.classes
        }
    )
    if unknown:
        result.add_conflict(
            "unknown_endpoint",
            f"relation endpoints are not defined: {', '.join(unknown)}",
            blocking=True,
        )
    if not change.evidence_required:
        result.add_conflict(
            "evidence_not_required",
            "the proposed relation does not require evidence; factual "
            "relations should keep evidence_required=true",
            blocking=False,
        )
    result.diff.added.append(
        DiffEntry(
            kind="relation",
            code=change.code,
            definition=change.model_dump(mode="json", exclude_none=True),
        )
    )
    result.impact = Impact(
        affected_classes=sorted(
            set(change.source_types) | set(change.target_types)
        ),
        affected_relations=[change.code],
        notes=[
            "Update the relation allowlist in the ontology prompt profile.",
            "Re-run domain validation after publication.",
        ],
    )

#检查约束 ID 是否重复、适用类是否存在；
# 生成新增约束的 diff，并提示该约束可能影响已存在的数据校验。
def _evaluate_add_constraint(
    change: AddConstraintChange,
    snapshot: _OntologySnapshot,
    result: _Evaluation,
) -> None:
    if change.id in snapshot.constraints:
        result.add_conflict(
            "constraint_exists",
            f"constraint {change.id!r} already exists",
            blocking=True,
        )
    unknown = sorted(
        code for code in change.applies_to if code not in snapshot.classes
    )
    if unknown:
        result.add_conflict(
            "unknown_constraint_target",
            f"constraint applies to unknown classes: {', '.join(unknown)}",
            blocking=True,
        )
    result.diff.added.append(
        DiffEntry(
            kind="constraint",
            code=change.id,
            definition=change.model_dump(mode="json", exclude_none=True),
        )
    )
    result.impact = Impact(
        affected_classes=sorted(change.applies_to),
        notes=[
            "Constraint changes can reject previously valid bundles; run "
            "validate_domain_bundle against every tenant before publication.",
        ],
    )

# 检查待废弃类是否存在、替代类是否有效、是否仍有子类或关系引用。
# 类不存在、替代类等于自身或替代类不存在、仍有子类：阻断。
# 仍被关系引用：非阻断提示。
# 可以继续评估时，生成“修改” diff，标记 deprecated=True，并将实例迁移标记为必需。
# 类不存在时会直接返回，因此不会再生成该类的 diff 或影响信息。
def _evaluate_deprecate_class(
    change: DeprecateClassChange,
    snapshot: _OntologySnapshot,
    result: _Evaluation,
) -> None:
    if change.code not in snapshot.classes:
        result.add_conflict(
            "class_missing",
            f"class {change.code!r} is not defined",
            blocking=True,
        )
        return
    if change.replacement:
        if change.replacement == change.code:
            result.add_conflict(
                "self_replacement",
                "replacement must differ from the deprecated class",
                blocking=True,
            )
        elif change.replacement not in snapshot.classes:
            result.add_conflict(
                "unknown_replacement",
                f"replacement class {change.replacement!r} is not defined",
                blocking=True,
            )
    children = sorted(
        code
        for code, item in snapshot.classes.items()
        if item.get("parent") == change.code
    )
    if children:
        result.add_conflict(
            "child_classes_present",
            f"class {change.code!r} still has child classes: "
            f"{', '.join(children)}; reparent them first",
            blocking=True,
        )
    affected_relations = sorted(
        code
        for code, item in snapshot.relations.items()
        if change.code in (item.get("source_types") or [])
        or change.code in (item.get("target_types") or [])
    )
    if affected_relations:
        result.add_conflict(
            "relations_reference_class",
            f"{len(affected_relations)} relation(s) still reference "
            f"{change.code!r}; publish replacements before removal",
            blocking=False,
        )
    result.diff.modified.append(
        DiffEntry(
            kind="class",
            code=change.code,
            definition={
                "deprecated": True,
                "replacement": change.replacement,
            },
        )
    )
    result.impact = Impact(
        affected_classes=sorted({change.code, *children}),
        affected_relations=affected_relations,
        affected_schemas=[
            f"schemas/entity.schema.json#/$defs/{code}"
            for code in sorted({change.code, *children})
        ],
        instance_migration_required=True,
        notes=[
            "Deprecation is not deletion: existing instances stay readable.",
            "Produce a migration plan for every tenant in W8.",
        ],
    )

#固定的审核清单，提示审核者检查来源证据、生成 Schema、更新提示词配置、规划迁移并运行相关测试。
_REVIEW_CHECKLIST = [
    "确认变更动机有 source_targets 指向的逐字证据支持",
    "运行 scripts/export_domain_schemas.py 并提交 schema 变更",
    "更新 ontology prompt profile 和领域包 README",
    "在 W8 生成实例迁移计划并完成人工审核",
    "重新运行 tests/domain、tests/extraction 和 tests/skills",
]

#若版本以 v主版本.次版本 结尾，则递增次版本；
# 否则保留原版本并追加 +proposal，同时返回警告。
def _propose_version(version: str) -> tuple[str, str | None]:
    match = re.search(r"v(\d+)\.(\d+)$", version)
    if not match:
        return f"{version}+proposal", (
            f"ontology version {version!r} has no vMAJOR.MINOR suffix; "
            "proposed +proposal instead of a numeric bump"
        )
    major = int(match.group(1))
    minor = int(match.group(2))
    proposed = f"{version[: match.start()]}v{major}.{minor + 1}"
    return proposed, None

#将条目列表转换为字典索引。默认用 code 作为键；约束可传 key="id"。
# 非列表、非字典项和无效键会被跳过。
def _by_code(
    value: Any,
    *,
    key: str = "code",
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        code = item.get(key)
        if isinstance(code, str) and code:
            result[code] = item
    return result

#把 Pydantic 输入错误转成简洁的字段路径、错误消息和错误类型列表，最多保留前 10 条。
def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "loc": ".".join(str(part) for part in error.get("loc", ())),
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()[:10]
    ]


__all__ = [
    "SKILL_NAME",
    "SKILL_TOOLS",
    "SKILL_VERSION",
    "AddClassChange",
    "AddConstraintChange",
    "AddRelationChange",
    "DeprecateClassChange",
    "OntologyChange",
    "OntologyChangeRequest",
    "OntologyDraftData",
    "OntologyDraftResponse",
    "run",
]
