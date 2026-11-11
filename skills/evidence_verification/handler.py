"""
实现“证据核验”Skill。它接收一个或多个声明目标，通过 MCP 的 trace_evidence 获取证据，
然后检查证据原文、哈希、字符区间、来源文件和目标关联；也可以额外校验故障树。

这个 Skill 只核验记录是否有可追溯的逐字证据，不判断声明在语义上是否正确。
它不直接读取源文件，也不会修改证据或故障树。
一次执行链：
run()
 ├─ 验证 VerificationRequest
 ├─ 对每条 Claim 调用 _check_claim()
 │   ├─ MCP trace_evidence
 │   ├─ collect_evidence()
 │   ├─ _aggregate_checks()
 │   └─ _target_linked()
 ├─ 可选调用 _check_tree()
 │   └─ MCP validate_fault_tree
 ├─ _build_data()
 └─ 返回 verified / partially_verified / rejected，
    或 not_found / failed / invalid_input
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
    failure_response,
    require_data,
    success_response,
)

SKILL_NAME = "evidence_verification"
SKILL_VERSION = "0.1.0"
#说明本 Skill 会调用 trace_evidence 和 validate_fault_tree 两个只读 MCP 工具。
SKILL_TOOLS = ("trace_evidence", "validate_fault_tree")

#单条待核验声明，包含声明 ID、目标类型和目标 ID。
# 目标类型限定为实体、事实、规则、逻辑门或决策步骤。
class Claim(BaseModel):
    """One record whose evidence should be verified."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    target_type: Literal[
        "entity", "fact", "rule", "logic_gate", "decision_step"
    ]
    target_id: str = Field(min_length=1)

#输入模型，包含租户 ID、声明列表和可选故障树草案 ID。
# 类级校验器 _require_work 要求至少提供 claims 或 draft_id，不能提交空请求。
class VerificationRequest(BaseModel):
    """Input contract for the evidence-verification skill."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)
    draft_id: str | None = None

    @model_validator(mode="after")
    def _require_work(self) -> "VerificationRequest":
        if not self.claims and not self.draft_id:
            raise ValueError("provide at least one claim or a draft_id")
        return self

#单条声明的核验结果，记录目标是否存在、证据数量、是否有证据支持、检查结果、问题、警告和证据明细。
class ClaimCheck(BaseModel):
    """The verification result for one claim pointer."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    target_type: str
    target_id: str
    exists: bool
    evidence_count: int
    supported: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)

#整批核验结果，包括声明列表、通过和拒绝数量、可选故障树校验结果及文字结论。
class VerificationData(BaseModel):
    """Verification payload for all claims and the optional tree."""

    model_config = ConfigDict(extra="forbid")

    claims: list[ClaimCheck] = Field(default_factory=list)
    verified_count: int
    rejected_count: int
    tree_validation: TreeValidation | None = None
    verdict: str

#Skill 的统一响应信封，data 使用 VerificationData 类型。
class VerificationResponse(SkillResponse):
    """Skill envelope with a typed verification payload."""

    data: VerificationData | None = None

#供 Schema 导出等工具确定输入和输出模型。
REQUEST_MODEL = VerificationRequest
RESPONSE_MODEL = VerificationResponse

# run 是执行入口，负责协调声明核验、故障树校验和响应生成：

# 验证输入：将传入字典转成 VerificationRequest。校验失败时返回 invalid_input 和 invalid_argument，不发起 MCP 调用。
# 建立执行上下文：创建 SkillContext，用于调用工具、记录调用轨迹、警告和版本信息。
# 逐条核验声明：对每条 Claim 调用 _check_claim。
# 可选校验故障树：如果传入 draft_id，调用 _check_tree。
# 处理过程中断：捕获 SkillExecutionError。如错误发生前已有核验结果，会把已完成部分放进响应数据；not_found 返回 not_found，其他错误返回 failed。
# 汇总结果并确定状态：
# 所有声明通过，且故障树也通过：verified
# 有声明通过，但未全部通过：partially_verified
# 没有声明通过：rejected
# 只有故障树请求时，根据树是否通过返回 verified 或 rejected
# 这里的 ok 表示核验流程是否顺利完成，而非核验结果是否为“通过”。因此 rejected 仍然可能是 ok=true。
async def run(
    request: VerificationRequest | Mapping[str, Any],
    tools: ToolClient,
) -> VerificationResponse:
    """Verify claim evidence without generating any new claim text."""

    try:
        req = (
            request
            if isinstance(request, VerificationRequest)
            else VerificationRequest.model_validate(request)
        )
    except ValidationError as exc:
        ctx = SkillContext(
            tools,
            skill=SKILL_NAME,
            version=SKILL_VERSION,
            tenant_id=None,
        )
        return failure_response(
            VerificationResponse,
            ctx,
            status="invalid_input",
            error=SkillErrorPayload(
                code="invalid_argument",
                message=(
                    "request does not match the evidence-verification input "
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
    checks: list[ClaimCheck] = []
    tree_validation: TreeValidation | None = None

    try:
        for claim in req.claims:
            checks.append(await _check_claim(ctx, req.tenant_id, claim))

        if req.draft_id:
            tree_validation = await _check_tree(
                ctx, req.tenant_id, req.draft_id
            )
    except SkillExecutionError as exc:
        data = (
            _build_data(checks, tree_validation)
            if checks or tree_validation is not None
            else None
        )
        return failure_response(
            VerificationResponse,
            ctx,
            status="not_found" if exc.code == "not_found" else "failed",
            error=SkillErrorPayload(
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
            data=data.model_dump(mode="json") if data else None,
        )

    data = _build_data(checks, tree_validation)
    tree_ok = tree_validation is None or tree_validation.valid
    if checks:
        if data.verified_count == len(checks) and tree_ok:
            status = "verified"
        elif data.verified_count > 0:
            status = "partially_verified"
        else:
            status = "rejected"
    else:
        status = "verified" if tree_ok else "rejected"
    if not tree_ok:
        ctx.warning(
            f"故障树 {tree_validation.draft_id} 未通过校验，声明核验结果不能替代树校验。"
        )
    return success_response(
        VerificationResponse,
        ctx,
        status=status,
        data=data.model_dump(mode="json"),
    )

#_check_claim 是核心核验逻辑：

# 调用 trace_evidence 查询声明目标的证据。
# 如果目标返回 not_found，生成一条 exists=False、supported=False 的结果，并把 target_not_found 记为问题；继续处理其他声明。
# 如果是其他 MCP 错误，则抛出 SkillExecutionError，中断后续流程并由 run() 生成失败响应。
# 查询成功时，用 collect_evidence() 把证据整理、去重。
# 调用 _aggregate_checks 汇总各证据项的检查结果，再逐项检查缺失项和目标关联。
# 计算 supported：要求存在证据，而且所有证据都有 quote、哈希和字符区间匹配、来源文件信息完整、目标关联正确。
# 若不支持，会把声明核验失败写入 Skill 级警告；同时返回这条声明的完整检查结果。
async def _check_claim(
    ctx: SkillContext,
    tenant_id: str,
    claim: Claim,
) -> ClaimCheck:
    envelope = await ctx.call(
        "trace_evidence",
        {
            "tenant_id": tenant_id,
            "target_type": claim.target_type,
            "target_id": claim.target_id,
        },
    )
    if not envelope.get("ok"):
        error = error_from_envelope(
            envelope,
            fallback_message="evidence lookup failed",
        )
        if error.code != "not_found":
            raise SkillExecutionError(
                error.code, error.message, error.details
            )
        return ClaimCheck(
            claim_id=claim.claim_id,
            target_type=claim.target_type,
            target_id=claim.target_id,
            exists=False,
            evidence_count=0,
            supported=False,
            checks={
                "evidence_present": False,
                "all_quotes_present": False,
                "all_hashes_match": False,
                "all_spans_match": False,
                "all_assets_present": False,
                "all_targets_linked": False,
                "all_review_approved": False,
            },
            issues=["target_not_found"],
        )

    data = require_data(envelope, "trace_evidence")
    evidence = collect_evidence(data.get("evidence"))
    checks = _aggregate_checks(evidence)
    checks["all_targets_linked"] = bool(evidence) and all(
        _target_linked(ref, claim) for ref in evidence
    )
    issues: list[str] = []
    warnings: list[str] = []
    if not evidence:
        issues.append("no_evidence_returned")
    for ref in evidence:
        per_evidence = ref.checks()
        for name in (
            "quote_present",
            "hash_matches",
            "span_matches",
            "asset_present",
        ):
            if not per_evidence[name]:
                issues.append(f"{ref.evidence_id}:{name}")
        if not _target_linked(ref, claim):
            issues.append(f"{ref.evidence_id}:target_not_linked")
        if not per_evidence["review_approved"]:
            warnings.append(
                f"{ref.evidence_id}:review_status={ref.review_status or 'missing'}"
            )

    supported = (
        checks["evidence_present"]
        and checks["all_quotes_present"]
        and checks["all_hashes_match"]
        and checks["all_spans_match"]
        and checks["all_assets_present"]
        and checks["all_targets_linked"]
    )
    if not supported:
        ctx.warning(
            f"声明 {claim.claim_id}（{claim.target_type}:{claim.target_id}）未通过证据核验。"
        )
    return ClaimCheck(
        claim_id=claim.claim_id,
        target_type=claim.target_type,
        target_id=claim.target_id,
        exists=True,
        evidence_count=len(evidence),
        supported=supported,
        checks=checks,
        issues=issues,
        warnings=warnings,
        evidence=evidence,
    )

#_check_tree 调用 validate_fault_tree 校验指定 draft_id：

# MCP 成功时，将 data 转换成共享的 TreeValidation。
# MCP 失败时，将错误转换成 SkillExecutionError，交由 run() 统一处理。因此草案不存在通常会让整体响应成为 not_found。
async def _check_tree(
    ctx: SkillContext,
    tenant_id: str,
    draft_id: str,
) -> TreeValidation:
    envelope = await ctx.call(
        "validate_fault_tree",
        {"tenant_id": tenant_id, "draft_id": draft_id},
    )
    if not envelope.get("ok"):
        error = error_from_envelope(
            envelope,
            fallback_message="fault-tree validation failed",
        )
        raise SkillExecutionError(error.code, error.message, error.details)
    return TreeValidation.from_mcp(
        require_data(envelope, "validate_fault_tree")
    )

#_aggregate_checks 将一条声明的多条证据检查结果汇总成布尔值，例如是否都有 quote、哈希匹配、区间匹配、来源文件齐全，以及是否都已审核。

# 没有证据时，各项结果都为 False。需要注意：此函数返回的 all_targets_linked 初始值也为 False；
# 在 _check_claim() 中会再单独计算并覆盖该字段。
def _aggregate_checks(evidence: list[EvidenceRef]) -> dict[str, bool]:
    if not evidence:
        return {
            "evidence_present": False,
            "all_quotes_present": False,
            "all_hashes_match": False,
            "all_spans_match": False,
            "all_assets_present": False,
            "all_targets_linked": False,
            "all_review_approved": False,
        }
    per = [ref.checks() for ref in evidence]
    return {
        "evidence_present": True,
        "all_quotes_present": all(item["quote_present"] for item in per),
        "all_hashes_match": all(item["hash_matches"] for item in per),
        "all_spans_match": all(item["span_matches"] for item in per),
        "all_assets_present": all(item["asset_present"] for item in per),
        "all_review_approved": all(item["review_approved"] for item in per),
    }

#_target_linked 判断证据是否指向当前声明目标：

# 如果证据的 via 中包含 "record"，视为由记录本身持有，直接判定关联。
# 否则，要求 targets 中存在 target_type 和 target_id 都匹配的目标。
def _target_linked(ref: EvidenceRef, claim: Claim) -> bool:
    if "record" in ref.via:
        return True
    return any(
        target.get("target_type") == claim.target_type
        and target.get("target_id") == claim.target_id
        for target in ref.targets
    )

# _build_data 统计 supported=True 的声明数和未通过数，并生成 verdict：

# 有声明时，根据通过情况生成汇总结论。
# 只有故障树时，结论包含树 ID、校验状态、错误数和警告数。
# 两者都没有时，返回“没有可核验的目标”。
# 它只负责构造数据对象，不决定 MCP 调用或响应信封。
def _build_data(
    checks: list[ClaimCheck],
    tree_validation: TreeValidation | None,
) -> VerificationData:
    verified = sum(1 for item in checks if item.supported)
    rejected = len(checks) - verified
    if checks:
        if rejected == 0 and (
            tree_validation is None or tree_validation.valid
        ):
            verdict = (
                f"全部 {len(checks)} 条声明均通过逐字证据核验；"
                "quote 哈希、字符区间和证据目标一致。"
            )
        elif verified > 0:
            verdict = (
                f"{verified} 条声明通过，{rejected} 条未通过；"
                "未通过的声明没有可用的完整逐字证据。"
            )
        else:
            verdict = (
                "没有声明通过核验；未生成任何超出 MCP 工具返回内容的结论。"
            )
    elif tree_validation is not None:
        verdict = (
            f"故障树 {tree_validation.draft_id} 校验"
            f"{'通过' if tree_validation.valid else '未通过'}，"
            f"错误 {tree_validation.error_count} 个、"
            f"警告 {tree_validation.warning_count} 个。"
        )
    else:
        verdict = "没有可核验的目标。"
    return VerificationData(
        claims=checks,
        verified_count=verified,
        rejected_count=rejected,
        tree_validation=tree_validation,
        verdict=verdict,
    )

# _validation_errors 将 Pydantic 错误转换成字段路径、错误消息和错误类型，最多返回前 10 条，用于放入结构化输入错误中。

# 文件末尾的 all 指定模块对外公开的名称，包括请求/响应模型、声明模型、结果模型和 run()。
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
    "Claim",
    "ClaimCheck",
    "VerificationData",
    "VerificationRequest",
    "VerificationResponse",
    "run",
]
