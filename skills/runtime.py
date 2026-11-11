"""
skills/runtime.py 是三个 Skill 共用的运行时基础层：它不负责具体业务流程，而是
统一 MCP 工具访问方式、调用审计记录、响应格式和证据校验逻辑。

可以把它理解为 Skills 与 MCP 之间的“公共接口层”：
限定 Skill 只能调用只读 MCP 工具。
统一整理 MCP 返回的证据和故障树校验结果。
为每次 Skill 执行收集工具调用、警告和版本信息。
把成功和失败结果包装成统一的响应信封。
提供证据哈希校验、去重和基础字段转换功能。

代码里有一个容易混淆的边界：这个文件提供的是 Skill 运行时约束与处理逻辑，
不是 MCP 服务本身，也不直接连接数据库或 LightRAG。

一次调用大致的流转：
具体 Skill handler
    → SkillContext.call()
        → 检查只读白名单
        → ToolClient.call_tool()
        → 记录调用并更新本体版本
    → require_data() / error_from_envelope()
    → EvidenceRef / TreeValidation 转换与检查
    → success_response() 或 failure_response()
    → build_response() 生成统一信封
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

SKILL_RUNTIME_VERSION = "0.1.0"

#: The complete read-only tool surface exposed by ``mcp_server``.  A skill may
#: not call anything else, and the CLI refuses to start a session whose tool
#: set differs.
READ_ONLY_TOOLS = frozenset(
    {
        "get_ontology",
        "get_equipment",
        "search_faults",
        "get_fault_tree",
        "trace_evidence",
        "validate_fault_tree",
    }
)

#允许作为证据追溯目标的类型，如实体、事实、规则和逻辑门。
TARGET_TYPES = ("entity", "fact", "rule", "logic_gate", "decision_step")

#协议接口，要求客户端实现异步 call_tool(name, arguments)。
# 真实 MCP 客户端和测试用假客户端都可以满足这个接口。
@runtime_checkable
class ToolClient(Protocol):
    """The only data path available to a skill handler."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Return one MCP response envelope for ``name``."""
        ...

#统一错误结构，包含错误码、消息和可选详情。
class SkillErrorPayload(BaseModel):
    """A machine-readable skill error, mirroring the MCP error payload."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

#统一元数据模型，记录 Skill 名称与版本、本体版本、租户，以及只读标记。
class SkillMeta(BaseModel):
    """Versions and scope shared by every skill response."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    skill_version: str
    ontology_version: str | None = None
    tenant_id: str | None = None
    read_only: bool = True

#一次 MCP 调用的审计记录，保存工具名、参数、成功状态及错误码。
class ToolCallTrace(BaseModel):
    """One audit record for a tool call made by a skill."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any]
    ok: bool
    error_code: str | None = None

#EvidenceRef 将 MCP 返回的证据整理成 Skill 内部统一使用的结构，
# 包括原文、哈希、来源文件、页码、字符位置、关联目标和审核状态。
class EvidenceRef(BaseModel):
    """A verbatim evidence fragment returned by ``trace_evidence``."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    quote: str
    quote_hash: str | None = None
    asset_id: str | None = None
    file_name: str | None = None
    page: int | None = None
    section_path: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    review_status: str | None = None
    targets: list[dict[str, str]] = Field(default_factory=list)
    via: list[str] = Field(default_factory=list)

    #from_mcp：把 MCP 的证据字典转换为 EvidenceRef。
    # 来源文件名从 asset.file_name 中读取；不符合类型要求的可选字段会被转换为 None。
    @classmethod
    def from_mcp(cls, item: Mapping[str, Any]) -> "EvidenceRef":
        """Project an MCP evidence entry onto the stable skill shape."""

        raw_asset = item.get("asset")
        asset = raw_asset if isinstance(raw_asset, Mapping) else {}
        return cls(
            evidence_id=str(item.get("evidence_id") or ""),
            quote=str(item.get("quote") or ""),
            quote_hash=_optional_text(item.get("quote_hash")),
            asset_id=_optional_text(item.get("asset_id")),
            file_name=_optional_text(asset.get("file_name")),
            page=_optional_int(item.get("page")),
            section_path=_optional_text(item.get("section_path")),
            char_start=_optional_int(item.get("char_start")),
            char_end=_optional_int(item.get("char_end")),
            review_status=_optional_text(item.get("review_status")),
            targets=_target_list(item.get("targets")),
            via=_string_list(item.get("via")),
        )

    #合并同一证据的两份信息。多数字段优先保留当前对象已有值；
    # via 会取并集，targets 仅在当前对象没有关联目标时才从另一份补上。
    def merge(self, other: "EvidenceRef") -> "EvidenceRef":
        """Return a copy carrying the union of both ``via`` lists."""

        merged = self.model_copy(deep=True)
        merged.quote = self.quote or other.quote
        merged.quote_hash = self.quote_hash or other.quote_hash
        merged.asset_id = self.asset_id or other.asset_id
        merged.file_name = self.file_name or other.file_name
        merged.page = self.page if self.page is not None else other.page
        merged.section_path = self.section_path or other.section_path
        merged.char_start = (
            self.char_start if self.char_start is not None else other.char_start
        )
        merged.char_end = (
            self.char_end if self.char_end is not None else other.char_end
        )
        merged.review_status = self.review_status or other.review_status
        if not merged.targets:
            merged.targets = list(other.targets)
        merged.via = sorted(set(self.via) | set(other.via))
        return merged

    #返回各项证据检查结果：quote 是否非空、哈希是否匹配、
    # 字符区间长度是否匹配、来源文件信息是否齐全，以及审核状态是否为 approved。
    def checks(self) -> dict[str, bool]:
        """Return the integrity checks a caller can act on."""

        return {
            "quote_present": bool(self.quote),
            "hash_matches": bool(self.quote_hash)
            and self.quote_hash == quote_digest(self.quote),
            "span_matches": (
                self.char_start is not None
                and self.char_end is not None
                and self.char_end - self.char_start == len(self.quote)
            ),
            "asset_present": bool(self.asset_id and self.file_name),
            "review_approved": self.review_status == "approved",
        }

    #综合判断 quote、哈希、区间和来源信息是否通过。
    # 审核状态不属于这个完整性判断；审核状态会单独出现在 checks() 结果里。
    def integrity_ok(self) -> bool:
        """True when the quote, hash, span and source asset all agree."""

        checks = self.checks()
        return all(
            checks[name]
            for name in (
                "quote_present",
                "hash_matches",
                "span_matches",
                "asset_present",
            )
        )

#只保留 MCP 故障树校验结果中 Skill 需要的字段，
# 如草案 ID、顶事件、是否合法、错误/警告数量和问题列表。
class TreeValidation(BaseModel):
    """The subset of ``validate_fault_tree`` output a skill reports."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str | None = None
    top_event_uid: str | None = None
    valid: bool
    error_count: int
    warning_count: int
    issues: list[dict[str, Any]] = Field(default_factory=list)

    #from_mcp：将 MCP 校验结果映射为 TreeValidation，并过滤掉格式不是字典的问题项。
    @classmethod
    def from_mcp(cls, data: Mapping[str, Any]) -> "TreeValidation":
        return cls(
            draft_id=_optional_text(data.get("draft_id")),
            top_event_uid=_optional_text(data.get("top_event_uid")),
            valid=bool(data.get("valid")),
            error_count=int(data.get("error_count") or 0),
            warning_count=int(data.get("warning_count") or 0),
            issues=[
                item
                for item in data.get("issues") or []
                if isinstance(item, dict)
            ],
        )

#SkillResponse：所有 Skill 共用的响应信封基类，
# 包含 ok、status、业务数据、错误、警告、工具调用记录和元数据。
class SkillResponse(BaseModel):
    """The common envelope returned by every skill."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    status: str
    data: dict[str, Any] | None = None
    error: SkillErrorPayload | None = None
    warnings: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    meta: SkillMeta

#限制在 SkillResponse 及其子类范围内的泛型类型，帮助响应构造函数保留各 Skill 的具体响应类型
SkillResponseT = TypeVar("SkillResponseT", bound=SkillResponse)

#Skill 内部使用的结构化异常；除了异常消息，也保存业务错误码和详情。
class SkillExecutionError(Exception):
    """Raised internally when a skill cannot continue."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

#一次 Skill 执行的上下文状态，保存客户端、Skill 和租户信息、本体版本、警告列表及工具调用记录。
class SkillContext:
    """Per-invocation state: tool access, trace, warnings and versions."""

    def __init__(
        self,
        tools: ToolClient,
        *,
        skill: str,
        version: str,
        tenant_id: str | None = None,
    ) -> None:
        self.tools = tools
        self.skill = skill
        self.version = version
        self.tenant_id = tenant_id
        self.ontology_version: str | None = None
        self.warnings: list[str] = []
        self.tool_calls: list[ToolCallTrace] = []

    #call：核心调用方法。先检查工具是否在只读白名单中，再调用客户端；
    # 接着检查返回值是否具有 MCP 信封的基本形状，记录调用结果，并从元数据中更新本体版本。
    # 越权工具会抛出 unsupported，不符合信封格式则抛出 upstream_error。
    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call one allow-listed read-only MCP tool and record the call."""

        if tool not in READ_ONLY_TOOLS:
            raise SkillExecutionError(
                "unsupported",
                f"tool {tool!r} is not in the read-only allowlist",
                {"tool": tool, "allowed": sorted(READ_ONLY_TOOLS)},
            )
        envelope = await self.tools.call_tool(tool, arguments)
        if not isinstance(envelope, Mapping) or not isinstance(
            envelope.get("ok"), bool
        ):
            raise SkillExecutionError(
                "upstream_error",
                f"{tool} returned a payload that is not an MCP envelope",
                {"tool": tool},
            )
        error = envelope.get("error")
        error_code = (
            error.get("code")
            if isinstance(error, Mapping) and not envelope["ok"]
            else None
        )
        self.tool_calls.append(
            ToolCallTrace(
                tool=tool,
                arguments=dict(arguments),
                ok=bool(envelope["ok"]),
                error_code=error_code if isinstance(error_code, str) else None,
            )
        )
        meta = envelope.get("meta")
        if isinstance(meta, Mapping):
            version = meta.get("ontology_version")
            if isinstance(version, str) and version:
                self.ontology_version = version
        return dict(envelope)

    #追加警告，并避免同一条警告重复出现。
    def warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

#要求 MCP 调用成功并取出 data。若调用失败，转换为 SkillExecutionError，保留上游错误码、消息和详情，并补充工具名。
def require_data(
    envelope: Mapping[str, Any],
    tool: str,
) -> dict[str, Any]:
    """Return envelope data or raise with the MCP error code preserved."""

    if envelope.get("ok"):
        data = envelope.get("data")
        return dict(data) if isinstance(data, Mapping) else {}
    error = envelope.get("error")
    error = error if isinstance(error, Mapping) else {}
    details = error.get("details")
    details = dict(details) if isinstance(details, Mapping) else {}
    details["tool"] = tool
    raise SkillExecutionError(
        str(error.get("code") or "upstream_error"),
        str(error.get("message") or f"{tool} failed"),
        details,
    )

#将 MCP 的错误字段转换成统一的 SkillErrorPayload，上游缺少错误信息时使用调用方提供的默认消息。
def error_from_envelope(
    envelope: Mapping[str, Any],
    *,
    fallback_message: str,
) -> SkillErrorPayload:
    """Copy an MCP error into the skill error shape."""

    error = envelope.get("error")
    error = error if isinstance(error, Mapping) else {}
    details = error.get("details")
    return SkillErrorPayload(
        code=str(error.get("code") or "upstream_error"),
        message=str(error.get("message") or fallback_message),
        details=dict(details) if isinstance(details, Mapping) else {},
    )

#构造 Skill 响应对象，把业务结果与上下文中的警告、工具调用记录和版本/租户元数据组合起来。
def build_response(
    response_cls: type[SkillResponseT],
    ctx: SkillContext,
    *,
    ok: bool,
    status: str,
    data: dict[str, Any] | None = None,
    error: SkillErrorPayload | None = None,
) -> SkillResponseT:
    """Build a skill-specific response carrying the invocation trace."""

    return response_cls(
        ok=ok,
        status=status,
        data=data,
        error=error,
        warnings=list(ctx.warnings),
        tool_calls=list(ctx.tool_calls),
        meta=SkillMeta(
            skill=ctx.skill,
            skill_version=ctx.version,
            ontology_version=ctx.ontology_version,
            tenant_id=ctx.tenant_id,
            read_only=True,
        ),
    )

#失败响应的便捷包装，内部调用 build_response() 并固定 ok=False。
def failure_response(
    response_cls: type[SkillResponseT],
    ctx: SkillContext,
    *,
    status: str,
    error: SkillErrorPayload,
    data: dict[str, Any] | None = None,
) -> SkillResponseT:
    return build_response(
        response_cls,
        ctx,
        ok=False,
        status=status,
        data=data,
        error=error,
    )

#成功响应的便捷包装，内部调用 build_response() 并固定 ok=True。
# 像“核验完成但结论不通过”这类业务结果，仍可以是成功执行的响应。
def success_response(
    response_cls: type[SkillResponseT],
    ctx: SkillContext,
    *,
    status: str,
    data: dict[str, Any],
) -> SkillResponseT:
    return build_response(
        response_cls,
        ctx,
        ok=True,
        status=status,
        data=data,
    )

#对 quote 的 UTF-8 字节计算 SHA-256，并返回带 sha256: 前缀的格式。
def quote_digest(quote: str) -> str:
    """Return the contract hash form ``sha256:<hex>`` for a quote."""

    return "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()

#把 MCP 返回的一组证据转换为 EvidenceRef，跳过非字典项和缺少证据 ID 的项；
# 按证据 ID 去重，重复项通过 merge() 合并，最后按来源文件、字符起点和 ID 排序。
def collect_evidence(
    items: Any,
) -> list[EvidenceRef]:
    """Project MCP evidence entries and de-duplicate by evidence ID."""

    merged: dict[str, EvidenceRef] = {}
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        ref = EvidenceRef.from_mcp(item)
        if not ref.evidence_id:
            continue
        merged[ref.evidence_id] = (
            merged[ref.evidence_id].merge(ref)
            if ref.evidence_id in merged
            else ref
        )
    return sorted(
        merged.values(),
        key=lambda ref: (
            ref.file_name or "",
            ref.char_start if ref.char_start is not None else -1,
            ref.evidence_id,
        ),
    )

#提取一组证据的唯一 ID，并排序后返回
def evidence_ids(refs: list[EvidenceRef]) -> list[str]:
    return sorted({ref.evidence_id for ref in refs})

#字符串且非空时返回原值，否则返回 None。
def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

#仅接受整数；特别排除 Python 中 bool 是 int 子类这一情况。
def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value

#仅保留列表中的字符串元素；输入不是列表时返回空列表。
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]

#从目标关联列表中提取 target_type、target_id 和 role；忽略非映射项，并将缺失值规范为空字符串
def _target_list(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "target_type": str(item.get("target_type") or ""),
                "target_id": str(item.get("target_id") or ""),
                "role": str(item.get("role") or ""),
            }
        )
    return result


__all__ = [
    "READ_ONLY_TOOLS",
    "TARGET_TYPES",
    "EvidenceRef",
    "SkillContext",
    "SkillErrorPayload",
    "SkillExecutionError",
    "SkillMeta",
    "SkillResponse",
    "ToolCallTrace",
    "ToolClient",
    "TreeValidation",
    "build_response",
    "collect_evidence",
    "error_from_envelope",
    "evidence_ids",
    "failure_response",
    "quote_digest",
    "require_data",
    "success_response",
]
