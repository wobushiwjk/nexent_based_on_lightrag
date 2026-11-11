"""Canonical data contracts for the industrial fault-tree domain.

The extraction payloads in :mod:`lightrag.fault_tree.schemas` describe what an
LLM may emit for one chunk.  The models here describe the records that survive
validation and review: assets, evidence, facts, causal rules, a fault-tree
draft, and a decision trace.  They are deliberately storage-agnostic so the
same records can be persisted in LightRAG graph fields, a relational database,
or exported as JSON.
"""

from __future__ import annotations  #延迟类型注解

#只有日期       日期+时间+时区
from datetime import date, datetime
#给类型加附加消息   任意类型    字面量枚举（字符串enum）      联合类型（类型enum）
from typing import Annotated, Any, Literal, Union
#Pydantic数据校验类型       模型级配置      字段级约束  校验多个字段之间的关系
from pydantic import BaseModel, ConfigDict, Field, model_validator

ONTOLOGY_VERSION = "industrial-fault-v0.1"
SCHEMA_VERSION = "industrial-fault-schema-v0.1"

ReviewStatus = Literal[
    "proposed",
    "approved",
    "rejected",
    "merged",
    "deprecated",
    "needs_more_evidence",
]

Modality = Literal["text", "table", "image", "pdf", "office"]
Severity = Literal["low", "medium", "high", "critical", "unknown"]
LifecycleStatus = Literal["in_service", "stopped", "retired"]
ParseStatus = Literal["uploaded", "parsing", "parsed", "failed"]
ParseEngine = Literal["native", "legacy", "mineru", "docling"]

#其他数据模型的共同基类，两条规则：
#拒绝模型没有声明过的字段
#允许按字段名填值，即便该字段另有其他序列化别名 （序列化：Python-->json, 反序列化即相反）
class ContractModel(BaseModel):
    """Base model for records that cross a service boundary."""

    #禁止多余字段   允许用字段名填充（alias（外部json）和Python字段名（代码内部）都可）
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

#所有领域实体共用的字段模板
class EntityBase(ContractModel):
    """Fields shared by every reviewed entity in the knowledge graph."""

    uid: str = Field(min_length=1)
    class_code: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    ontology_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    review_status: ReviewStatus = "proposed"
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_uids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str = Field(min_length=1)

    #校验多个字段之间的关系
    #之里面时valid_from 不能晚于 valid_to
    @model_validator(mode="after")
    def validate_lifetime(self) -> "EntityBase":
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not be earlier than valid_from")
        return self


class Department(EntityBase):
    """An organisational container for equipment and files."""

    class_code: Literal["Department"] = "Department"
    department_code: str | None = None
    department_path: str | None = None
    status: Literal["active", "inactive"] = "active"
    external_refs: dict[str, Any] = Field(default_factory=dict)


class FileAsset(EntityBase):
    """A source document, image, table, or office file."""

    class_code: Literal["File"] = "File"
    file_name: str = Field(min_length=1)
    file_extension: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    modality: Modality
    object_uri: str = Field(min_length=1)
    checksum: str = Field(min_length=1)
    file_size: int = Field(ge=0)
    version: str | None = None
    language: str | None = None
    source_system: str | None = None
    uploader_id: str = Field(min_length=1)
    upload_time: datetime
    parse_status: ParseStatus
    parse_engine: ParseEngine | None = None
    parse_error: str | None = None
    page_count: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)
    last_parsed_at: datetime | None = None


class Equipment(EntityBase):
    """A physical asset that can carry faults."""

    class_code: Literal["Equipment"] = "Equipment"
    equipment_tag: str = Field(min_length=1)
    equipment_name: str = Field(min_length=1)
    equipment_type: str = Field(min_length=1)
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    functional_location: str | None = None
    lifecycle_status: LifecycleStatus = "in_service"
    commissioning_date: date | None = None
    retirement_date: date | None = None
    external_refs: dict[str, Any] = Field(default_factory=dict)


class FaultEvent(EntityBase):
    """A fault, symptom, or intermediate event in a fault tree."""

    class_code: Literal["FaultEvent"] = "FaultEvent"
    fault_name: str = Field(min_length=1)
    severity: Severity = "unknown"
    symptoms: list[str] = Field(default_factory=list)
    maintenance_measures: list[str] = Field(default_factory=list)
    operating_conditions: dict[str, Any] = Field(default_factory=dict)
    active_cause_rule_uid: str | None = None

#不是一个类，而是一个类型别名，表明实体记录只能是Department、FileAsset、Equipment 或 FaultEvent 之一。
EntityRecord = Annotated[
    Union[Department, FileAsset, Equipment, FaultEvent],
    Field(discriminator="class_code"),
]

#描述一条证据关联到哪个目标，以及关联含义
class EvidenceTarget(ContractModel):
    """The record an evidence fragment supports or contradicts."""

    target_type: Literal[
        "entity",
        "fact",
        "rule",
        "logic_gate",
        "decision_step",
    ]
    target_id: str = Field(min_length=1)
    role: Literal["supports", "contradicts", "mentions"] = "supports"

#保存一段原文证据及其来源信息
class Evidence(ContractModel):
    """A verbatim source fragment and the records it supports."""

    evidence_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    section_path: str | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    quote: str = Field(min_length=1)
    quote_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    modality: Modality = "text"
    targets: list[EvidenceTarget] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    extractor: str | None = None
    extraction_run_id: str | None = None
    review_status: ReviewStatus = "proposed"

    #如果字符串起止位置都提供了，char_end必须大于char_start
    @model_validator(mode="after")
    def validate_span(self) -> "Evidence":
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end <= self.char_start
        ):
            raise ValueError("char_end must be greater than char_start")
        return self

#表示一条具体事实
class Fact(ContractModel):
    """A binary assertion such as ``filter blocked CAUSES low pressure``."""

    fact_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    subject_uid: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_uid: str = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    review_status: ReviewStatus = "proposed"
    ontology_version: str = Field(min_length=1)

#递归表示故障原因表达式
class UidExpression(ContractModel):
    """A recursive AND/OR expression over durable fault-event UIDs."""

    op: Literal["AND", "OR"] | None = None
    event_uid: str | None = None
    children: list["UidExpression"] = Field(default_factory=list)

    #检查3个地方：
    #1.每个系欸但必须是故障事件叶子或AND/OR运算节点，不能两者都有或都没有
    #2.AND/OR至少有两个子项
    #3.叶子不能再有子项
    @model_validator(mode="after")
    def validate_shape(self) -> "UidExpression":
        has_event = bool(self.event_uid and self.event_uid.strip())
        has_operator = self.op is not None
        if has_event == has_operator:
            raise ValueError(
                "expression must contain exactly one of event_uid or op"
            )
        if has_operator and len(self.children) < 2:
            raise ValueError("AND/OR expressions require at least two children")
        if has_event and self.children:
            raise ValueError("event leaves cannot contain children")
        return self

#表示哪些原因以什么逻辑导致哪个结果故障
class FaultCauseRule(ContractModel):
    """The reviewed causal rule that produces one fault event."""

    rule_uid: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    effect_fault_uid: str = Field(min_length=1)
    expression: UidExpression
    scenario: str | None = None
    evidence_ids: list[str] = Field(min_length=1)
    review_status: ReviewStatus = "proposed"
    ontology_version: str = Field(min_length=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime
    created_by: str = Field(min_length=1)

    #检查表达式的根事件不能直接等于结果故障，避免自我导致关系
    @model_validator(mode="after")
    def validate_root_is_not_effect(self) -> "FaultCauseRule":
        if self.expression.event_uid == self.effect_fault_uid:
            raise ValueError("root expression cannot reference the effect itself")
        return self

#下面两个是校验结果模型

#一条问题记录，包含错误代码、严重程度、描述和受影响的ID
class ValidationIssue(ContractModel):
    """One structural or evidence problem found in a bundle or tree."""

    code: str = Field(min_length=1)
    severity: Literal["error", "warning", "info"]
    message: str = Field(min_length=1)
    target_id: str | None = None

#一组校验问题，以及整体是否有效
class ValidationResult(ContractModel):
    """The result of validating a fault-tree draft."""

    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

#下面三个是故障树模型

#树中的一个节点，可以是FaultEvent，FaultCauseRule或LogicGate
class FaultTreeNode(ContractModel):
    """A node in a projected fault-tree draft."""

    uid: str = Field(min_length=1)
    class_code: Literal["FaultEvent", "FaultCauseRule", "LogicGate"]
    label: str = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)

#节点之间的有向关系，只允许CAUSES，HAS_CAUSE_RULE,INPUT_OF,OUTPUT_TO
class FaultTreeEdge(ContractModel):
    """A directed edge in a projected fault-tree draft."""

    source_uid: str = Field(min_length=1)
    target_uid: str = Field(min_length=1)
    relation_code: Literal[
        "CAUSES",
        "HAS_CAUSE_RULE",
        "INPUT_OF",
        "OUTPUT_TO",
    ]
    properties: dict[str, Any] = Field(default_factory=dict)

#一份故障树草案，包含顶事件、节点、边、校验结果、版本和审核状态。
#这里的Draft是待检查/审核的树形数据，不是抽取模型输出的一个领域实体
class FaultTreeDraft(ContractModel):
    """A reviewable projection of rules and facts into a fault tree."""

    draft_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    top_event_uid: str = Field(min_length=1)
    nodes: list[FaultTreeNode] = Field(min_length=1)
    edges: list[FaultTreeEdge] = Field(default_factory=list)
    validation: ValidationResult
    version: int = Field(default=1, ge=1)
    review_status: ReviewStatus = "proposed"
    ontology_version: str = Field(min_length=1)
    created_at: datetime
    created_by: str = Field(min_length=1)

#下面三个是诊断过程和打包

#诊断过程中的一步，例如检索、推理、核验或结论；可关联MCP工具和证据
class DecisionStep(ContractModel):
    """One retrieve / reason / verify / conclude step in a decision trace."""

    step_id: str = Field(min_length=1)
    step_type: Literal["retrieve", "reason", "verify", "conclude"]
    summary: str = Field(min_length=1)
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_output_ref: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    hypothesis_status: Literal["supported", "rejected", "unknown"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

#把用户问题、诊断步骤、最终结论和证据ID串成可审计的过程记录
class DecisionTrace(ContractModel):
    """The auditable chain from a question to a supported conclusion."""

    trace_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    steps: list[DecisionStep] = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    ontology_version: str = Field(min_length=1)
    created_at: datetime
    created_by: str = Field(min_length=1)

#把某个租户的数据打成一个整体快照，包含实体、证据、事实、规则，还可选包含故障树和诊断逻辑
class DomainBundle(ContractModel):
    """A self-contained, reviewable snapshot of one domain workspace."""

    bundle_version: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    entities: list[EntityRecord] = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    rules: list[FaultCauseRule] = Field(default_factory=list)
    tree: FaultTreeDraft | None = None
    trace: DecisionTrace | None = None

#Pydantic的初始化调用
UidExpression.model_rebuild()
