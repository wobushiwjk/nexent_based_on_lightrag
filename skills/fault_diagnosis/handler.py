""" 
实现了一个确定性的故障诊断 Skill：
输入租户、设备标识和可选症状，通过 MCP 查询设备与故障候选，再为候选追溯证据，并可附带故障树及其校验结果。
它本身不调用大模型、不直接访问数据库或 LightRAG。
诊断结论只总结 MCP 返回的信息；若找不到候选或证据不足，会返回结构化结果，而不是猜测原因。

一次执行的调用链：
run()
 ├─ 验证请求
 ├─ get_ontology
 ├─ get_equipment
 ├─ search_faults
 ├─ 对每个候选调用 _build_candidate()
 │   ├─ trace_evidence：故障实体
 │   ├─ trace_evidence：最多两条原因规则
 │   └─ 检查并合并证据
 ├─ 可选调用 _load_tree()
 │   ├─ get_fault_tree
 │   └─ validate_fault_tree
 ├─ _conclusion()
 └─ 返回 diagnosed 或结构化失败/证据不足响应
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from skills.runtime import (
    EvidenceRef,
    SkillContext,
    SkillErrorPayload,
    SkillExecutionError,
    SkillResponse,
    ToolClient,
    TreeValidation,
    collect_evidence,
    error_from_envelope,
    evidence_ids,
    failure_response,
    require_data,
    success_response,
)

SKILL_NAME = "fault_diagnosis"
SKILL_VERSION = "0.1.0"
SKILL_TOOLS = ("get_ontology", "get_equipment", "search_faults", "trace_evidence", "get_fault_tree", "validate_fault_tree")

#接下来七个为一组Pydantic模型，用于限定请求和响应的字段、类型与范围

#输入模型。tenant_id 和 equipment_ref 
# 必填；symptom 
# 可选；候选数量限制为 1–10，默认 5；include_tree 默认开启。
class DiagnosisRequest(BaseModel):
    """Input contract for the fault-diagnosis skill."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    equipment_ref: str = Field(min_length=1)
    symptom: str | None = None
    max_candidates: int = Field(default=5, ge=1, le=10)
    include_tree: bool = True

#诊断结果里保留的设备摘要，例如 UID、位号、名称和匹配方式。
class EquipmentSummary(BaseModel):
    """The identifying fields copied from ``get_equipment``."""

    model_config = ConfigDict(extra="forbid")

    uid: str
    equipment_tag: str | None = None
    equipment_name: str | None = None
    equipment_type: str | None = None
    lifecycle_status: str | None = None
    matched_by: str | None = None

#一个候选故障及其证据、原因规则、是否与设备直接关联、证据完整性和搜索信息。
class Candidate(BaseModel):
    """One candidate fault with its verbatim evidence."""

    model_config = ConfigDict(extra="forbid")

    fault_uid: str
    canonical_name: str
    severity: str | None = None
    evidence_count: int
    evidence: list[EvidenceRef] = Field(default_factory=list)
    cause_rule_uids: list[str] = Field(default_factory=list)
    appears_in_rule_uids: list[str] = Field(default_factory=list)
    linked_by_equipment: bool = False
    evidence_complete: bool
    review_statuses: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    score: int | None = None

#故障树摘要，包括草案 ID、顶事件、节点/边数量和校验结果。
class TreeSummary(BaseModel):
    """The fault-tree facts a diagnosis cites."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    top_event_uid: str
    counts: dict[str, int] = Field(default_factory=dict)
    validation: TreeValidation | None = None

#诊断业务数据，包含设备、搜索词、候选、故障树、结论和证据 ID。
class DiagnosisData(BaseModel):
    """Successful or degraded diagnosis payload."""

    model_config = ConfigDict(extra="forbid")

    equipment: EquipmentSummary
    query: str | None
    candidates: list[Candidate] = Field(default_factory=list)
    fault_tree: TreeSummary | None = None
    conclusion: str
    evidence_ids: list[str] = Field(default_factory=list)

#继承通用 Skill 响应信封，将 data 指定为 DiagnosisData。
class DiagnosisResponse(SkillResponse):
    """Skill envelope with a typed diagnosis payload."""

    data: DiagnosisData | None = None

#供 Schema 生成等工具识别输入和输出模型。
REQUEST_MODEL = DiagnosisRequest
RESPONSE_MODEL = DiagnosisResponse

# run 负责串起完整诊断流程：

# 验证输入：将传入字典转换为 DiagnosisRequest。不符合模型时，返回 invalid_input，不会调用 MCP。
# 建立执行上下文：创建 SkillContext，用于调用 MCP、记录警告、调用轨迹和本体版本。
# 读取本体版本：调用 get_ontology，让响应元数据记录当前本体版本。
# 查找设备：调用 get_equipment。设备未找到返回 not_found；设备名有歧义时返回 invalid_input，保留上游错误详情。
# 准备故障候选：先收集设备关联故障，再根据症状或设备信息调用 search_faults，并按故障 UID 去重。
# 控制候选数量：保留前 max_candidates 个候选。设备关联故障先加入，所以它们通常排在搜索结果之前。
# 逐个收集候选证据：调用 _build_candidate。
# 可选加载故障树：如果启用 include_tree，以第一个有证据的候选为顶事件尝试查询故障树。
# 生成结论：汇总候选名、证据条数和故障树信息。
# 决定响应状态：有候选且至少一个候选有证据时返回 diagnosed；
# 候选都没有证据时返回 needs_more_evidence，同时保留已知候选信息。
# 内部 SkillExecutionError 会转成结构化错误响应。
async def run(
    request: DiagnosisRequest | Mapping[str, Any],
    tools: ToolClient,
) -> DiagnosisResponse:
    """Diagnose one equipment reference using only MCP read tools."""

    try:
        req = (
            request
            if isinstance(request, DiagnosisRequest)
            else DiagnosisRequest.model_validate(request)
        )
    except ValidationError as exc:
        ctx = SkillContext(
            tools,
            skill=SKILL_NAME,
            version=SKILL_VERSION,
            tenant_id=None,
        )
        return failure_response(
            DiagnosisResponse,
            ctx,
            status="invalid_input",
            error=SkillErrorPayload(
                code="invalid_argument",
                message="request does not match the fault-diagnosis input schema",
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
        require_data(ontology_envelope, "get_ontology")

        equipment_envelope = await ctx.call(
            "get_equipment",
            {
                "tenant_id": req.tenant_id,
                "equipment_ref": req.equipment_ref,
            },
        )
        if not equipment_envelope.get("ok"):
            error = error_from_envelope(
                equipment_envelope,
                fallback_message="equipment lookup failed",
            )
            status = (
                "not_found"
                if error.code == "not_found"
                else "invalid_input"
            )
            return failure_response(
                DiagnosisResponse,
                ctx,
                status=status,
                error=error,
            )
        equipment_data = require_data(equipment_envelope, "get_equipment")
        equipment_summary = _equipment_summary(equipment_data)

        linked_faults = _dict_list(equipment_data.get("faults"))
        candidates: dict[str, dict[str, Any]] = {}
        for item in linked_faults:
            uid = item.get("uid")
            if isinstance(uid, str) and uid:
                candidates[uid] = {"summary": item, "linked": True}

        query = _diagnosis_query(req, equipment_data)
        if query is None:
            ctx.warning(
                "设备记录没有关联故障，且请求未提供 symptom；先用 search_faults 找到候选故障。"
            )
            search_data: dict[str, Any] = {
                "count": 0,
                "results": [],
                "hint": "provide a symptom",
            }
        else:
            search_envelope = await ctx.call(
                "search_faults",
                {
                    "tenant_id": req.tenant_id,
                    "query": query,
                    "limit": max(req.max_candidates, 10),
                },
            )
            if not search_envelope.get("ok"):
                error = error_from_envelope(
                    search_envelope,
                    fallback_message="fault search failed",
                )
                return failure_response(
                    DiagnosisResponse,
                    ctx,
                    status="invalid_input"
                    if error.code == "invalid_argument"
                    else "failed",
                    error=error,
                )
            search_data = require_data(search_envelope, "search_faults")

        for item in _dict_list(search_data.get("results")):
            uid = item.get("uid")
            if isinstance(uid, str) and uid and uid not in candidates:
                candidates[uid] = {"summary": item, "linked": False}

        ordered = list(candidates.values())[: req.max_candidates]
        if not ordered:
            ctx.warning(
                "没有候选故障匹配该设备/症状；不生成诊断结论，也不猜测原因。"
            )
            return _degraded(
                ctx,
                equipment_summary,
                query,
                candidates=[],
                tree=None,
                message=(
                    "no fault candidate matched the equipment and symptom; "
                    "provide a symptom, a fault alias or a fault UID"
                ),
                details={"query": query},
            )

        built: list[Candidate] = []
        for item in ordered:
            built.append(await _build_candidate(ctx, req, item, query))

        top_uid = next(
            (item.fault_uid for item in built if item.evidence_count > 0),
            built[0].fault_uid,
        )
        tree = (
            await _load_tree(ctx, req, top_uid)
            if req.include_tree
            else None
        )

        data = DiagnosisData(
            equipment=equipment_summary,
            query=query,
            candidates=built,
            fault_tree=tree,
            conclusion=_conclusion(equipment_summary, built, tree),
            evidence_ids=evidence_ids(
                [ref for item in built for ref in item.evidence]
            ),
        )
        evidenced = [item for item in built if item.evidence_count > 0]
        if not evidenced:
            ctx.warning(
                "候选故障存在，但都没有逐字证据；结果标记为证据不足，不作为诊断结论。"
            )
            return failure_response(
                DiagnosisResponse,
                ctx,
                status="needs_more_evidence",
                error=SkillErrorPayload(
                    code="insufficient_evidence",
                    message=(
                        "candidate faults were found but none has verbatim "
                        "evidence in this tenant"
                    ),
                    details={
                        "candidates": [item.fault_uid for item in built],
                    },
                ),
                data=data.model_dump(mode="json"),
            )
        return success_response(
            DiagnosisResponse,
            ctx,
            status="diagnosed",
            data=data.model_dump(mode="json"),
        )
    except SkillExecutionError as exc:
        if exc.code == "not_found":
            status = "not_found"
        elif exc.code == "invalid_argument":
            status = "invalid_input"
        else:
            status = "failed"
        return failure_response(
            DiagnosisResponse,
            ctx,
            status=status,
            error=SkillErrorPayload(
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
        )

# _build_candidate 为一个故障候选收集并整理证据：

# 调用 trace_evidence(target_type="entity") 查询故障本身的证据。
# 对 cause_rule_uids 中最多前两条规则，分别查询规则证据。
# 用证据 ID 合并重复证据，再按来源文件、字符起点和 ID 排序。
# 调用 EvidenceRef.integrity_ok() 检查 quote、哈希、字符区间和来源信息。
# 创建 Candidate，带上证据、规则、名称、严重程度和搜索相关信息。
async def _build_candidate(
    ctx: SkillContext,
    req: DiagnosisRequest,
    item: Mapping[str, Any],
    query: str | None,
) -> Candidate:
    summary = item["summary"]
    uid = str(summary.get("uid"))
    refs: dict[str, EvidenceRef] = {}

    entity_envelope = await ctx.call(
        "trace_evidence",
        {
            "tenant_id": req.tenant_id,
            "target_type": "entity",
            "target_id": uid,
        },
    )
    if entity_envelope.get("ok"):
        entity_data = require_data(entity_envelope, "trace_evidence")
        for ref in collect_evidence(entity_data.get("evidence")):
            refs[ref.evidence_id] = ref
    else:
        error = error_from_envelope(
            entity_envelope,
            fallback_message="evidence lookup failed",
        )
        ctx.warning(
            f"候选故障 {uid} 的证据查询失败（{error.code}），该候选标记为证据不完整。"
        )

    for rule_uid in _string_list(summary.get("cause_rule_uids"))[:2]:
        rule_envelope = await ctx.call(
            "trace_evidence",
            {
                "tenant_id": req.tenant_id,
                "target_type": "rule",
                "target_id": rule_uid,
            },
        )
        if not rule_envelope.get("ok"):
            error = error_from_envelope(
                rule_envelope,
                fallback_message="rule evidence lookup failed",
            )
            ctx.warning(
                f"规则 {rule_uid} 的证据查询失败（{error.code}）。"
            )
            continue
        rule_data = require_data(rule_envelope, "trace_evidence")
        for ref in collect_evidence(rule_data.get("evidence")):
            refs[ref.evidence_id] = (
                refs[ref.evidence_id].merge(ref)
                if ref.evidence_id in refs
                else ref
            )

    evidence = sorted(
        refs.values(),
        key=lambda ref: (
            ref.file_name or "",
            ref.char_start if ref.char_start is not None else -1,
            ref.evidence_id,
        ),
    )
    complete = bool(evidence) and all(ref.integrity_ok() for ref in evidence)
    if evidence and not complete:
        bad = [ref.evidence_id for ref in evidence if not ref.integrity_ok()]
        ctx.warning(
            f"候选故障 {uid} 有证据未通过 quote/hash/字符区间校验：{', '.join(bad)}。"
        )

    return Candidate(
        fault_uid=uid,
        canonical_name=str(
            summary.get("canonical_name") or summary.get("fault_name") or uid
        ),
        severity=_optional_text(summary.get("severity")),
        evidence_count=len(evidence),
        evidence=evidence,
        cause_rule_uids=_string_list(summary.get("cause_rule_uids")),
        appears_in_rule_uids=_string_list(
            summary.get("appears_in_rule_uids")
        ),
        linked_by_equipment=bool(item.get("linked")),
        evidence_complete=complete,
        review_statuses=sorted(
            {
                ref.review_status
                for ref in evidence
                if ref.review_status is not None
            }
        ),
        matched_terms=_string_list(summary.get("matched_terms")),
        score=_optional_int(summary.get("score")),
    )

#_load_tree 查询并校验与候选故障相关的故障树：

# 先用候选 UID 查询 get_fault_tree。
# 只有顶事件查询返回 not_found 时，才重试不指定顶事件的租户故障树查询。
# 读取草案 ID 和顶事件 UID。响应缺少草案 ID 时，记录警告并不返回树。
# 调用 validate_fault_tree 校验树。校验不通过或调用失败会产生警告，不会直接让诊断失败。
# 从响应中提取节点数和边数，构造 TreeSummary。
async def _load_tree(
    ctx: SkillContext,
    req: DiagnosisRequest,
    top_uid: str,
) -> TreeSummary | None:
    envelope = await ctx.call(
        "get_fault_tree",
        {"tenant_id": req.tenant_id, "top_event_uid": top_uid},
    )
    if not envelope.get("ok"):
        first_error = error_from_envelope(
            envelope,
            fallback_message="fault-tree lookup failed",
        )
        if first_error.code != "not_found":
            ctx.warning(
                f"故障树查询失败（{first_error.code}），诊断只包含候选和证据。"
            )
            return None
        envelope = await ctx.call(
            "get_fault_tree",
            {"tenant_id": req.tenant_id},
        )
        if not envelope.get("ok"):
            ctx.warning(
                "该租户没有与候选故障匹配的故障树；诊断不引用树结构。"
            )
            return None

    data = require_data(envelope, "get_fault_tree")
    draft = data.get("draft")
    draft = draft if isinstance(draft, Mapping) else {}
    draft_id = draft.get("draft_id")
    top_event_uid = draft.get("top_event_uid")
    if not isinstance(draft_id, str) or not draft_id:
        ctx.warning("故障树响应缺少 draft_id，诊断不引用树结构。")
        return None
    if isinstance(top_event_uid, str) and top_event_uid != top_uid:
        ctx.warning(
            f"返回的故障树顶事件是 {top_event_uid}，与候选故障 {top_uid} 不同；"
            "树只作为背景结构，不改变候选排序。"
        )

    validation = None
    validation_envelope = await ctx.call(
        "validate_fault_tree",
        {"tenant_id": req.tenant_id, "draft_id": draft_id},
    )
    if validation_envelope.get("ok"):
        validation = TreeValidation.from_mcp(
            require_data(validation_envelope, "validate_fault_tree")
        )
        if not validation.valid:
            ctx.warning(
                f"故障树 {draft_id} 校验未通过（{validation.error_count} 个错误）。"
            )
    else:
        error = error_from_envelope(
            validation_envelope,
            fallback_message="fault-tree validation failed",
        )
        ctx.warning(f"故障树 {draft_id} 校验失败（{error.code}）。")

    counts = data.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    return TreeSummary(
        draft_id=draft_id,
        top_event_uid=(
            top_event_uid if isinstance(top_event_uid, str) else top_uid
        ),
        counts={
            str(key): int(value)
            for key, value in counts.items()
            if isinstance(value, int) and not isinstance(value, bool)
        },
        validation=validation,
    )

#构造 needs_more_evidence 响应。
# 保留设备、搜索词、候选和故障树等已知数据，并明确说明未生成诊断结论
def _degraded(
    ctx: SkillContext,
    equipment: EquipmentSummary,
    query: str | None,
    *,
    candidates: list[Candidate],
    tree: TreeSummary | None,
    message: str,
    details: dict[str, Any],
) -> DiagnosisResponse:
    data = DiagnosisData(
        equipment=equipment,
        query=query,
        candidates=candidates,
        fault_tree=tree,
        conclusion=(
            f"设备 {equipment.equipment_tag or equipment.uid} 没有可用的候选故障，"
            "未生成诊断结论。"
        ),
        evidence_ids=evidence_ids(
            [ref for item in candidates for ref in item.evidence]
        ),
    )
    return failure_response(
        DiagnosisResponse,
        ctx,
        status="needs_more_evidence",
        error=SkillErrorPayload(
            code="insufficient_evidence",
            message=message,
            details=details,
        ),
        data=data.model_dump(mode="json"),
    )

#从 get_equipment 返回数据中提取设备摘要。
# 设备响应缺少有效 UID 时抛出 upstream_error。
def _equipment_summary(data: Mapping[str, Any]) -> EquipmentSummary:
    equipment = data.get("equipment")
    equipment = equipment if isinstance(equipment, Mapping) else {}
    match = data.get("match")
    match = match if isinstance(match, Mapping) else {}
    uid = equipment.get("uid")
    if not isinstance(uid, str) or not uid:
        raise SkillExecutionError(
            "upstream_error",
            "get_equipment returned no equipment UID",
            {"tool": "get_equipment"},
        )
    return EquipmentSummary(
        uid=uid,
        equipment_tag=_optional_text(equipment.get("equipment_tag")),
        equipment_name=_optional_text(
            equipment.get("equipment_name")
        )
        or _optional_text(equipment.get("canonical_name")),
        equipment_type=_optional_text(equipment.get("equipment_type")),
        lifecycle_status=_optional_text(
            equipment.get("lifecycle_status")
        ),
        matched_by=_optional_text(match.get("matched_by")),
    )

#确定故障搜索词。优先使用用户提供的非空症状；
# 否则依次尝试设备位号、设备名称和规范名称。都没有可用值时返回 None。
def _diagnosis_query(
    req: DiagnosisRequest,
    equipment_data: Mapping[str, Any],
) -> str | None:
    symptom = req.symptom.strip() if req.symptom else ""
    if symptom:
        return symptom
    equipment = equipment_data.get("equipment")
    equipment = equipment if isinstance(equipment, Mapping) else {}
    for value in (
        equipment.get("equipment_tag"),
        equipment.get("equipment_name"),
        equipment.get("canonical_name"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

# 根据设备、候选故障名称、证据条数和可选故障树摘要，生成简短结论，并声明内容只依据 MCP 返回
def _conclusion(
    equipment: EquipmentSummary,
    candidates: list[Candidate],
    tree: TreeSummary | None,
) -> str:
    label = equipment.equipment_tag or equipment.uid
    if equipment.equipment_name:
        label = f"{label}（{equipment.equipment_name}）"
    names = "、".join(item.canonical_name for item in candidates)
    counts = "、".join(str(item.evidence_count) for item in candidates)
    parts = [
        f"设备 {label} 有 {len(candidates)} 个候选故障：{names}。",
        f"候选故障的证据条数依次为 {counts}。",
    ]
    if tree is not None:
        state = (
            "通过"
            if tree.validation is not None and tree.validation.valid
            else "未通过或未校验"
        )
        parts.append(
            f"故障树 {tree.draft_id} 包含 "
            f"{tree.counts.get('nodes', 0)} 个节点、"
            f"{tree.counts.get('edges', 0)} 条边，校验{state}。"
        )
    parts.append(
        "以上内容只引用 MCP 工具返回的故障记录和逐字证据，不包含工具数据之外的推断。"
    )
    return "".join(parts)

#把 Pydantic 输入错误转换成字段路径、消息和错误类型，最多保留十条。
def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "loc": ".".join(str(part) for part in error.get("loc", ())),
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()[:10]
    ]

#只保留列表中的字典项；其他值返回空列表。
def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]

#只保留列表中的字符串元素。
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]

#非空字符串原样返回；其他情况返回 None。
def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

#只接受整数，并特别排除布尔值，因为 Python 中 bool 是 int 的子类。
def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


__all__ = [
    "SKILL_NAME",
    "SKILL_TOOLS",
    "SKILL_VERSION",
    "Candidate",
    "DiagnosisData",
    "DiagnosisRequest",
    "DiagnosisResponse",
    "EquipmentSummary",
    "TreeSummary",
    "run",
]
