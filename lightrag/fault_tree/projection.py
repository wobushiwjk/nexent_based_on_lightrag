"""
把按文本片段组织的抽取候选，转换成带稳定ID、领域类型和证据引用的正式ID
它主要输出四类数据：
EntityRecord：设备、故障事件、部门等实体；
Fact：例如“过滤器堵塞导致出口压力不足”；
FaultCauseRule：带 AND/OR 表达式的故障因果规则；
Evidence：来源明确、可追溯的原文证据。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from lightrag.fault_tree.contracts import (
    SCHEMA_VERSION,
    Department,
    EntityRecord,
    Equipment,
    Evidence,
    EvidenceTarget,
    Fact,
    FaultCauseRule,
    FaultEvent,
    ReviewStatus,
    UidExpression,
)
from lightrag.fault_tree.domain_validation import quote_hash
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.fault_tree.schemas import (
    EvidenceCandidate,
    Expression,
    FaultCandidateBatch,
    FaultEntityPayload,
)

Modality = Literal["text", "table", "image", "pdf", "office"]

_ENTITY_PREFIX = {
    "Department": "DEPT",
    "File": "FILE",
    "Equipment": "EQ",
    "FaultEvent": "FAULT",
}
_SYSTEM_CLASSES = {"FaultCauseRule", "LogicGate"}


class ProjectionError(ValueError):
    """A projection that cannot produce a contract-valid domain snapshot."""

    def __init__(self, message: str, *, issues: list[Any] | None = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])

#一个源文本片段及其来源信息，如文件 ID、片段 ID、页码和字符偏移。
@dataclass(frozen=True)
class SourceChunk:
    """The source text and locator context for one extraction batch."""

    asset_id: str
    chunk_id: str
    text: str
    page: int | None = None
    section_path: str | None = None
    modality: Modality = "text"
    char_base: int = 0

#把一个 SourceChunk 和该片段的抽取候选放在一起。
@dataclass(frozen=True)
class CandidateBatchInput:
    """One validator batch together with the text it was extracted from."""

    chunk: SourceChunk
    batch: FaultCandidateBatch

#投影完成的实体、证据、事实、规则和警告。
@dataclass
class ProjectedDomain:
    """Records produced from extraction candidates, before a tree is built."""

    entities: list[EntityRecord]
    evidence: list[Evidence]
    facts: list[Fact]
    rules: list[FaultCauseRule]
    warnings: list[str] = field(default_factory=list)

#_EvidenceDraft、_FactSpec、_RuleSpec：
# 投影过程中的临时记录，便于先聚合、再生成最终契约对象。
@dataclass
class _EvidenceDraft:
    evidence_id: str
    asset_id: str
    chunk_id: str
    page: int | None
    section_path: str | None
    quote: str
    char_start: int
    char_end: int
    modality: Modality
    confidence: float | None
    extractor: str | None
    extraction_run_id: str | None
    review_status: ReviewStatus
    targets: dict[tuple[str, str, str], EvidenceTarget] = field(
        default_factory=dict
    )


@dataclass
class _FactSpec:
    fact_id: str
    subject_uid: str
    predicate: str
    object_uid: str
    properties: dict[str, Any]
    confidence: float | None
    review_status: ReviewStatus
    evidence_ids: set[str] = field(default_factory=set)


@dataclass
class _RuleSpec:
    rule_uid: str
    effect_fault_uid: str
    expression: UidExpression
    scenario: str | None
    review_status: ReviewStatus
    evidence_ids: set[str] = field(default_factory=set)

# 文件的主函数。它接收本体、租户 ID、抽取批次和文件资产，返回 ProjectedDomain。主要分为几步：
# 准备输入：检查租户 ID 和资产；确认片段 ID 不重复，每个片段对应的文件资产存在。
# 投影实体：把候选中的类别名解析为本体类别，生成规范实体。未知类、系统生成类和候选中的 File 会被忽略并记录 warning；文件资产应由输入的 assets 提供。
# 合并实体：同一稳定 UID 在多个片段出现时合并信息。
# 投影事实：解析关系两端，检查关系是否符合本体，再生成 Fact，并关联该关系的证据。
# 投影规则：将候选中的局部 AND/OR 表达式转换为使用稳定事件 UID 的规则，并关联证据。
# 整理证据：把临时证据整理成 Evidence，生成 quote 哈希，保留目标记录关系。
# 剔除无证据事实和规则：没有逐字证据的事实或规则不会成为最终输出，会产生 warning。
# 补齐实体的证据 ID：根据证据目标给实体填写 evidence_ids。
# 有些严重输入问题会直接抛出 ProjectionError；部分无法解析的候选则以 warning 记录并跳过。
def project_domain(
    ontology: OntologyRegistry,
    *,
    tenant_id: str,
    batches: list[CandidateBatchInput],
    assets: list[EntityRecord] | None = None,
    schema_version: str | None = None,
    review_status: ReviewStatus = "proposed",
    created_by: str = "extraction",
    created_at: datetime | None = None,
    extractor: str | None = None,
    extraction_run_id: str | None = None,
) -> ProjectedDomain:
    """Turn validator batches into canonical entities, evidence, facts, rules."""

    tenant_id = tenant_id.strip()
    if not tenant_id:
        raise ValueError("tenant_id must not be empty")
    timestamp = created_at or datetime.now(timezone.utc)
    schema_version = schema_version or ontology.schema_version or SCHEMA_VERSION

    asset_records = list(assets or [])
    asset_index: dict[str, EntityRecord] = {}
    for asset in asset_records:
        if asset.uid in asset_index:
            raise ProjectionError(f"duplicate asset uid {asset.uid!r}")
        if asset.tenant_id != tenant_id:
            raise ProjectionError(
                f"asset {asset.uid!r} belongs to tenant {asset.tenant_id!r}"
            )
        asset_index[asset.uid] = asset

    entities: dict[str, EntityRecord] = {}
    local_to_uid: dict[tuple[str, str], str] = {}
    name_to_uid: dict[tuple[str, str], str] = {}
    chunk_by_id: dict[str, SourceChunk] = {}
    batch_by_chunk: dict[str, FaultCandidateBatch] = {}
    warnings: list[str] = []

    for item in batches:
        chunk = item.chunk
        if chunk.chunk_id in chunk_by_id:
            raise ProjectionError(f"duplicate chunk_id {chunk.chunk_id!r}")
        if chunk.asset_id not in asset_index:
            raise ProjectionError(
                f"chunk {chunk.chunk_id!r} references unknown asset "
                f"{chunk.asset_id!r}"
            )
        chunk_by_id[chunk.chunk_id] = chunk
        batch_by_chunk[chunk.chunk_id] = item.batch

    # Entities ---------------------------------------------------------
    for item in batches:
        chunk = item.chunk
        for payload in item.batch.entities:
            class_code = ontology.canonical_class(payload.class_name)
            if class_code is None:
                warnings.append(
                    f"{chunk.chunk_id}: unknown class {payload.class_name!r}"
                )
                continue
            if class_code in _SYSTEM_CLASSES:
                warnings.append(
                    f"{chunk.chunk_id}: {class_code} is system-generated; "
                    "the extraction candidate was ignored"
                )
                continue
            if class_code == "File":
                warnings.append(
                    f"{chunk.chunk_id}: File records belong to the asset layer; "
                    "the extraction candidate was ignored"
                )
                continue
            record = _build_entity(
                payload,
                class_code=class_code,
                tenant_id=tenant_id,
                chunk=chunk,
                ontology=ontology,
                schema_version=schema_version,
                review_status=review_status,
                created_by=created_by,
                timestamp=timestamp,
                warnings=warnings,
            )
            if record is None:
                continue
            key = (chunk.chunk_id, payload.local_id)
            if key in local_to_uid:
                warnings.append(
                    f"{chunk.chunk_id}: duplicate local_id "
                    f"{payload.local_id!r} was ignored"
                )
                continue
            existing = entities.get(record.uid)
            if existing is not None:
                record = _merge_entity(existing, record)
            entities[record.uid] = record
            local_to_uid[key] = record.uid
            name_to_uid[
                (chunk.chunk_id, _normalize_name(record.canonical_name))
            ] = record.uid

    for asset in asset_records:
        existing = entities.get(asset.uid)
        entities[asset.uid] = (
            _merge_entity(existing, asset) if existing is not None else asset
        )

    # Facts ------------------------------------------------------------
    evidence_drafts: dict[str, _EvidenceDraft] = {}
    fact_specs: dict[str, _FactSpec] = {}
    for item in batches:
        chunk = item.chunk
        for payload in item.batch.relations:
            source_uid = _resolve_relation_endpoint(
                payload.source_local_id,
                payload.source_ref,
                chunk_id=chunk.chunk_id,
                local_to_uid=local_to_uid,
                name_to_uid=name_to_uid,
            )
            target_uid = _resolve_relation_endpoint(
                payload.target_local_id,
                payload.target_ref,
                chunk_id=chunk.chunk_id,
                local_to_uid=local_to_uid,
                name_to_uid=name_to_uid,
            )
            if source_uid is None or target_uid is None:
                warnings.append(
                    f"{chunk.chunk_id}: relation {payload.relation_code!r} "
                    "references an entity that was not projected"
                )
                continue
            source = entities.get(source_uid)
            target = entities.get(target_uid)
            if source is None or target is None:
                warnings.append(
                    f"{chunk.chunk_id}: relation {payload.relation_code!r} "
                    "has a missing endpoint record"
                )
                continue
            try:
                ontology.validate_relation(
                    payload.relation_code,
                    source.class_code,
                    target.class_code,
                )
            except ValueError as exc:
                warnings.append(
                    f"{chunk.chunk_id}: relation "
                    f"{payload.relation_code!r} rejected: {exc}"
                )
                continue

            fact_id = (
                payload.fact_id.strip()
                if payload.fact_id and payload.fact_id.strip()
                else _stable_id(
                    "FACT", tenant_id, source_uid, payload.relation_code, target_uid
                )
            )
            spec = fact_specs.setdefault(
                fact_id,
                _FactSpec(
                    fact_id=fact_id,
                    subject_uid=source_uid,
                    predicate=payload.relation_code,
                    object_uid=target_uid,
                    properties=dict(payload.properties),
                    confidence=payload.model_confidence,
                    review_status=_review_status(
                        payload.review_status, review_status
                    ),
                ),
            )
            if (
                spec.subject_uid != source_uid
                or spec.predicate != payload.relation_code
                or spec.object_uid != target_uid
            ):
                warnings.append(
                    f"{chunk.chunk_id}: fact id {fact_id!r} was reused for "
                    "different endpoints; the later record was ignored"
                )
                continue
            for candidate in payload.evidence:
                evidence_id = _add_evidence(
                    evidence_drafts,
                    candidate,
                    chunk=chunk,
                    asset=asset_index[chunk.asset_id],
                    tenant_id=tenant_id,
                    default_review_status=review_status,
                    default_extractor=extractor,
                    default_run_id=extraction_run_id,
                    target=("fact", fact_id, "supports"),
                    warnings=warnings,
                )
                if evidence_id:
                    spec.evidence_ids.add(evidence_id)
            for endpoint_uid in (source_uid, target_uid):
                for candidate in payload.evidence:
                    _add_evidence(
                        evidence_drafts,
                        candidate,
                        chunk=chunk,
                        asset=asset_index[chunk.asset_id],
                        tenant_id=tenant_id,
                        default_review_status=review_status,
                        default_extractor=extractor,
                        default_run_id=extraction_run_id,
                        target=("entity", endpoint_uid, "mentions"),
                        warnings=warnings,
                    )

    # Rules ------------------------------------------------------------
    rule_specs: dict[str, _RuleSpec] = {}
    for item in batches:
        chunk = item.chunk
        for payload in item.batch.entities:
            if payload.cause_rule is None:
                continue
            effect_uid = local_to_uid.get((chunk.chunk_id, payload.local_id))
            if effect_uid is None:
                continue
            expression = _project_expression(
                payload.cause_rule,
                chunk_id=chunk.chunk_id,
                local_to_uid=local_to_uid,
                warnings=warnings,
            )
            if expression is None:
                continue
            if expression.event_uid == effect_uid:
                warnings.append(
                    f"{chunk.chunk_id}: cause_rule of {effect_uid!r} references "
                    "the effect itself"
                )
                continue
            rule_uid = _rule_uid(payload, effect_uid, expression)
            spec = rule_specs.get(rule_uid)
            if spec is None:
                spec = _RuleSpec(
                    rule_uid=rule_uid,
                    effect_fault_uid=effect_uid,
                    expression=expression,
                    scenario=_extra_value(payload, "rule_scenario")
                    or _extra_value(payload, "scenario"),
                    review_status=_review_status(
                        _extra_value(payload, "review_status"),
                        review_status,
                    ),
                )
                rule_specs[rule_uid] = spec
            elif spec.expression != expression:
                warnings.append(
                    f"{chunk.chunk_id}: rule {rule_uid!r} has a different "
                    "expression in another chunk; the first expression was kept"
                )
            for candidate in payload.evidence:
                evidence_id = _add_evidence(
                    evidence_drafts,
                    candidate,
                    chunk=chunk,
                    asset=asset_index[chunk.asset_id],
                    tenant_id=tenant_id,
                    default_review_status=review_status,
                    default_extractor=extractor,
                    default_run_id=extraction_run_id,
                    target=("rule", rule_uid, "supports"),
                    warnings=warnings,
                )
                if evidence_id:
                    spec.evidence_ids.add(evidence_id)

    # Entity-level evidence -------------------------------------------
    for item in batches:
        chunk = item.chunk
        for payload in item.batch.entities:
            entity_uid = local_to_uid.get((chunk.chunk_id, payload.local_id))
            if entity_uid is None:
                continue
            for candidate in payload.evidence:
                _add_evidence(
                    evidence_drafts,
                    candidate,
                    chunk=chunk,
                    asset=asset_index[chunk.asset_id],
                    tenant_id=tenant_id,
                    default_review_status=review_status,
                    default_extractor=extractor,
                    default_run_id=extraction_run_id,
                    target=("entity", entity_uid, "mentions"),
                    warnings=warnings,
                )

    # Assemble facts and rules, dropping anything without provenance.
    facts: list[Fact] = []
    for spec in fact_specs.values():
        if not spec.evidence_ids:
            warnings.append(
                f"fact {spec.fact_id!r} dropped: no verbatim evidence"
            )
            continue
        facts.append(
            Fact(
                fact_id=spec.fact_id,
                tenant_id=tenant_id,
                subject_uid=spec.subject_uid,
                predicate=spec.predicate,
                object_uid=spec.object_uid,
                properties=spec.properties,
                evidence_ids=sorted(spec.evidence_ids),
                confidence=spec.confidence,
                review_status=spec.review_status,
                ontology_version=ontology.version,
            )
        )

    rules: list[FaultCauseRule] = []
    for spec in rule_specs.values():
        if not spec.evidence_ids:
            warnings.append(
                f"rule {spec.rule_uid!r} dropped: no verbatim evidence"
            )
            continue
        rules.append(
            FaultCauseRule(
                rule_uid=spec.rule_uid,
                tenant_id=tenant_id,
                effect_fault_uid=spec.effect_fault_uid,
                expression=spec.expression,
                scenario=spec.scenario,
                evidence_ids=sorted(spec.evidence_ids),
                review_status=spec.review_status,
                ontology_version=ontology.version,
                created_at=timestamp,
                created_by=created_by,
            )
        )

    # A dropped fact or rule must not survive as an evidence target.
    surviving_targets = {
        ("fact", fact.fact_id) for fact in facts
    } | {
        ("rule", rule.rule_uid) for rule in rules
    }
    for draft in evidence_drafts.values():
        for key in list(draft.targets):
            if key[0] in {"fact", "rule"} and key[:2] not in surviving_targets:
                del draft.targets[key]

    # Build evidence rows from the surviving targets.
    evidence: list[Evidence] = []
    for draft in evidence_drafts.values():
        if not draft.targets:
            continue
        evidence.append(
            Evidence(
                evidence_id=draft.evidence_id,
                tenant_id=tenant_id,
                asset_id=draft.asset_id,
                chunk_id=draft.chunk_id,
                page=draft.page,
                section_path=draft.section_path,
                char_start=draft.char_start,
                char_end=draft.char_end,
                quote=draft.quote,
                quote_hash=quote_hash(draft.quote),
                modality=draft.modality,
                targets=_ordered_targets(draft.targets),
                confidence=draft.confidence,
                extractor=draft.extractor,
                extraction_run_id=draft.extraction_run_id,
                review_status=draft.review_status,
            )
        )
    evidence.sort(key=lambda item: item.evidence_id)

    evidence_by_entity: dict[str, list[str]] = {}
    for item in evidence:
        for target in item.targets:
            if target.target_type == "entity":
                evidence_by_entity.setdefault(target.target_id, []).append(
                    item.evidence_id
                )

    rule_uids = {rule.rule_uid for rule in rules}
    final_entities: list[EntityRecord] = []
    for record in entities.values():
        evidence_ids = sorted(set(evidence_by_entity.get(record.uid, [])))
        updates: dict[str, Any] = {"evidence_ids": evidence_ids}
        active_rule = getattr(record, "active_cause_rule_uid", None)
        if active_rule and active_rule not in rule_uids:
            updates["active_cause_rule_uid"] = None
        final_entities.append(record.model_copy(update=updates))

    return ProjectedDomain(
        entities=final_entities,
        evidence=evidence,
        facts=facts,
        rules=rules,
        warnings=warnings,
    )

# 把一条实体候选转换为具体的 Equipment、FaultEvent 或 Department。
# 它整理名称、UID、租户、来源、审核状态、置信度等通用字段，
# 再根据实体类别填充专有字段。设备缺少必要的位号或类型时，
# 会记 warning 并跳过该实体。它不负责创建文件实体。
def _build_entity(
    payload: FaultEntityPayload,
    *,
    class_code: str,
    tenant_id: str,
    chunk: SourceChunk,
    ontology: OntologyRegistry,
    schema_version: str,
    review_status: ReviewStatus,
    created_by: str,
    timestamp: datetime,
    warnings: list[str],
) -> EntityRecord | None:
    name = (
        _extra_value(payload, "name")
        or _extra_value(payload, "canonical_name")
        or _property_value(payload, "name")
        or payload.local_id
    )
    name = str(name).strip()
    if not name:
        warnings.append(f"{chunk.chunk_id}: entity has an empty name")
        return None
    uid = (
        _extra_value(payload, "uid")
        or _property_value(payload, "uid")
        or _default_uid(class_code, name)
    )
    uid = str(uid).strip()
    if not uid:
        warnings.append(f"{chunk.chunk_id}: entity {name!r} has an empty uid")
        return None

    common: dict[str, Any] = {
        "uid": uid,
        "tenant_id": tenant_id,
        "canonical_name": name,
        "description": _extra_value(payload, "description")
        or _property_value(payload, "description"),
        "aliases": _string_list(
            _extra_value(payload, "aliases")
            or _property_value(payload, "aliases")
        ),
        "ontology_version": ontology.version,
        "schema_version": schema_version,
        "review_status": _review_status(
            _extra_value(payload, "review_status"), review_status
        ),
        "confidence": _float_value(
            _extra_value(payload, "confidence")
            if _extra_value(payload, "confidence") is not None
            else _property_value(payload, "confidence")
        ),
        "source_uids": [chunk.asset_id],
        "evidence_ids": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "created_by": created_by,
    }

    if class_code == "Equipment":
        equipment_tag = _extra_value(payload, "equipment_tag") or _property_value(
            payload, "equipment_tag"
        )
        equipment_name = _extra_value(payload, "equipment_name") or name
        equipment_type = _extra_value(
            payload, "equipment_type"
        ) or _property_value(payload, "equipment_type")
        if not equipment_tag or not equipment_type:
            warnings.append(
                f"{chunk.chunk_id}: Equipment {name!r} is missing "
                "equipment_tag or equipment_type"
            )
            return None
        return Equipment(
            **common,
            equipment_tag=str(equipment_tag).strip(),
            equipment_name=str(equipment_name).strip(),
            equipment_type=str(equipment_type).strip(),
            manufacturer=_extra_value(payload, "manufacturer")
            or _property_value(payload, "manufacturer"),
            model=_extra_value(payload, "model")
            or _property_value(payload, "model"),
            serial_number=_extra_value(payload, "serial_number")
            or _property_value(payload, "serial_number"),
            functional_location=_extra_value(payload, "functional_location")
            or _property_value(payload, "functional_location"),
            lifecycle_status=_extra_value(payload, "lifecycle_status")
            or "in_service",
            commissioning_date=_extra_value(payload, "commissioning_date"),
            retirement_date=_extra_value(payload, "retirement_date"),
            external_refs=_mapping_value(
                _extra_value(payload, "external_refs"), name, warnings
            ),
        )

    if class_code == "FaultEvent":
        return FaultEvent(
            **common,
            fault_name=str(
                _extra_value(payload, "fault_name") or name
            ).strip(),
            severity=_extra_value(payload, "severity")
            or _property_value(payload, "severity")
            or "unknown",
            symptoms=_string_list(
                _extra_value(payload, "symptoms")
                or _property_value(payload, "symptoms")
            ),
            maintenance_measures=_string_list(
                _extra_value(payload, "maintenance_measures")
                or _property_value(payload, "maintenance_measures")
            ),
            operating_conditions=_mapping_value(
                _extra_value(payload, "operating_conditions"), name, warnings
            ),
            active_cause_rule_uid=_extra_value(
                payload, "active_cause_rule_uid"
            ),
        )

    if class_code == "Department":
        return Department(
            **common,
            department_code=_extra_value(payload, "department_code")
            or _property_value(payload, "department_code"),
            department_path=_extra_value(payload, "department_path")
            or _property_value(payload, "department_path"),
            status=_extra_value(payload, "status") or "active",
            external_refs=_mapping_value(
                _extra_value(payload, "external_refs"), name, warnings
            ),
        )

    warnings.append(
        f"{chunk.chunk_id}: class {class_code!r} cannot be projected from "
        "an extraction candidate"
    )
    return None

#合并多个片段中 UID 相同的实体：合并别名和来源、取较高置信度、保留较新的更新时间；
# 对故障事件还合并症状和维修措施，并在已有严重程度未知时补上新的已知值。
def _merge_entity(
    existing: EntityRecord,
    incoming: EntityRecord,
) -> EntityRecord:
    updates: dict[str, Any] = {
        "aliases": _union(existing.aliases, incoming.aliases),
        "source_uids": _union(existing.source_uids, incoming.source_uids),
        "confidence": _max_confidence(existing.confidence, incoming.confidence),
        "updated_at": max(existing.updated_at, incoming.updated_at),
    }
    if not existing.description and incoming.description:
        updates["description"] = incoming.description
    if isinstance(existing, FaultEvent) and isinstance(incoming, FaultEvent):
        updates["symptoms"] = _union(existing.symptoms, incoming.symptoms)
        updates["maintenance_measures"] = _union(
            existing.maintenance_measures, incoming.maintenance_measures
        )
        if existing.severity == "unknown" and incoming.severity != "unknown":
            updates["severity"] = incoming.severity
        if (
            not existing.active_cause_rule_uid
            and incoming.active_cause_rule_uid
        ):
            updates["active_cause_rule_uid"] = incoming.active_cause_rule_uid
    return existing.model_copy(update=updates)

#递归转换因果表达式。输入使用片段内局部 ID，例如 filter；输出使用稳定 UID，例如 FAULT-FILTER-BLOCKED。
# 局部 ID 找不到时产生 warning，并返回 None，表示这条表达式无法投影。
def _project_expression(
    expression: Expression,
    *,
    chunk_id: str,
    local_to_uid: dict[tuple[str, str], str],
    warnings: list[str],
) -> UidExpression | None:
    if expression.event_local_id:
        event_uid = local_to_uid.get((chunk_id, expression.event_local_id))
        if event_uid is None:
            warnings.append(
                f"{chunk_id}: cause_rule references unprojected local_id "
                f"{expression.event_local_id!r}"
            )
            return None
        return UidExpression(event_uid=event_uid)
    children: list[UidExpression] = []
    for child in expression.children:
        projected = _project_expression(
            child,
            chunk_id=chunk_id,
            local_to_uid=local_to_uid,
            warnings=warnings,
        )
        if projected is None:
            return None
        children.append(projected)
    if expression.op is None:
        warnings.append(f"{chunk_id}: cause_rule expression has no operator")
        return None
    return UidExpression(op=expression.op, children=children)

#验证并暂存一条证据：
# 如果没有字符位置，就在片段文本中找 quote 并计算位置；
# 如果候选提供了字符位置，就检查位置对应的文本是否等于 quote；
# 如果 quote 不在片段中或位置不匹配，记录 warning，丢弃该证据关联；
# 如果候选没有证据 ID，就生成稳定 ID；
# 如果同一证据 ID 再次出现，会合并它的目标。
# 这里验证的是候选片段文本；源文件整体层面的验证由其他校验模块负责。
def _add_evidence(
    drafts: dict[str, _EvidenceDraft],
    candidate: EvidenceCandidate,
    *,
    chunk: SourceChunk,
    asset: EntityRecord,
    tenant_id: str,
    default_review_status: ReviewStatus,
    default_extractor: str | None,
    default_run_id: str | None,
    target: tuple[str, str, str],
    warnings: list[str],
) -> str | None:
    quote = candidate.quote
    if not quote:
        return None
    text = chunk.text
    start = candidate.char_start
    end = candidate.char_end
    if start is None or end is None:
        local_start = text.find(quote)
        if local_start < 0:
            warnings.append(
                f"{chunk.chunk_id}: quote is not a verbatim substring; "
                "the evidence target was dropped"
            )
            return None
        start = chunk.char_base + local_start
        end = start + len(quote)
    elif not span_matches(text, quote, start, end, chunk.char_base):
        warnings.append(
            f"{chunk.chunk_id}: explicit char span does not match the quote; "
            "the evidence target was dropped"
        )
        return None

    evidence_id = (
        candidate.evidence_id.strip()
        if candidate.evidence_id and candidate.evidence_id.strip()
        else _stable_id("EV", tenant_id, chunk.asset_id, chunk.chunk_id, quote)
    )
    existing = drafts.get(evidence_id)
    if existing is not None:
        if (
            existing.asset_id != chunk.asset_id
            or existing.chunk_id != (candidate.chunk_id or chunk.chunk_id)
            or existing.quote != quote
        ):
            warnings.append(
                f"{chunk.chunk_id}: evidence id {evidence_id!r} was reused "
                "for a different quote; the later target was ignored"
            )
            return None
        existing.targets.setdefault(target, _target(*target))
        return evidence_id

    asset_modality = getattr(asset, "modality", None)
    modality: Modality = (
        candidate.modality
        or chunk.modality
        or asset_modality
        or "text"
    )
    drafts[evidence_id] = _EvidenceDraft(
        evidence_id=evidence_id,
        asset_id=candidate.asset_id or chunk.asset_id,
        chunk_id=candidate.chunk_id or chunk.chunk_id,
        page=candidate.page if candidate.page is not None else chunk.page,
        section_path=(
            candidate.section_path
            if candidate.section_path is not None
            else chunk.section_path
        ),
        quote=quote,
        char_start=start,
        char_end=end,
        modality=modality,
        confidence=candidate.confidence,
        extractor=candidate.extractor or default_extractor,
        extraction_run_id=(
            candidate.extraction_run_id or default_run_id
        ),
        review_status=_review_status(
            candidate.review_status, default_review_status
        ),
        targets={target: _target(*target)},
    )
    return evidence_id

#把证据关联的目标字典转换为列表，同时保留目标加入时的顺序，避免无意义的重排。
def _ordered_targets(
    targets: dict[tuple[str, str, str], EvidenceTarget],
) -> list[EvidenceTarget]:
    # Preserve insertion order: the extraction candidate that attached a
    # target first is the one a reviewer expects to see first.  Sorting here
    # would scramble target order across chunks for no semantic gain.
    return list(targets.values())

#根据目标类别、目标 ID 和角色构造 EvidenceTarget。例如“这条证据支持某条规则”。
def _target(target_type: str, target_id: str, role: str) -> EvidenceTarget:
    return EvidenceTarget(
        target_type=target_type,
        target_id=target_id,
        role=role,
    )

#判断 quote 是否正好处于指定字符范围。它同时接受片段内偏移和源文件偏移；
# char_base 用来把源文件偏移换算为片段内位置。
def span_matches(
    text: str,
    quote: str,
    start: int,
    end: int,
    char_base: int,
) -> bool:
    """Accept a chunk-local span or a source-document span.

    Extraction locators are source-document offsets, while verification runs
    against the chunk text.  ``char_base`` bridges the two without forcing
    every caller to pre-subtract an offset.
    """

    if end <= start:
        return False
    if 0 <= start and end <= len(text) and text[start:end] == quote:
        return True
    local_start = start - char_base
    local_end = end - char_base
    return (
        0 <= local_start
        and local_end <= len(text)
        and text[local_start:local_end] == quote
    )

#解析关系一端的实体。如果候选给了 local_id，按当前片段查找；
# 否则用名称引用，在当前片段中按规范化名称查找。都找不到就返回 None。
def _resolve_relation_endpoint(
    local_id: str | None,
    name_ref: str | None,
    *,
    chunk_id: str,
    local_to_uid: dict[tuple[str, str], str],
    name_to_uid: dict[tuple[str, str], str],
) -> str | None:
    if local_id:
        return local_to_uid.get((chunk_id, local_id))
    if name_ref:
        return name_to_uid.get((chunk_id, _normalize_name(name_ref)))
    return None

# 生成规则 ID。候选提供了 rule_uid 就使用它；
# 否则根据结果故障 UID 和表达式内容生成稳定 ID。
def _rule_uid(
    payload: FaultEntityPayload,
    effect_uid: str,
    expression: UidExpression,
) -> str:
    explicit = _extra_value(payload, "rule_uid")
    if explicit:
        return str(explicit).strip()
    return _stable_id(
        "RULE",
        effect_uid,
        expression.model_dump_json(exclude_none=True),
    )

# 候选没提供 UID 时，根据实体类别和名称生成默认 UID，
# 例如设备使用 EQ- 前缀、故障事件使用 FAULT- 前缀。
def _default_uid(class_code: str, name: str) -> str:
    prefix = _ENTITY_PREFIX.get(class_code, class_code.upper())
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()
    if not slug:
        slug = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}-{slug}"

#把给定字符串组合后做 SHA-1，截取部分摘要并加上类型前缀，用于稳定生成事实、证据、规则等 ID。
def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"

#从候选模型允许的额外字段中读取值。
# 抽取候选有些领域字段不是基础 schema 固定字段，所以会放在 model_extra 中。
def _extra_value(payload: FaultEntityPayload, key: str) -> Any:
    return payload.model_extra.get(key)

# 从候选的 properties 字典中读取指定属性。
def _property_value(payload: FaultEntityPayload, key: str) -> Any:
    return payload.properties.get(key)

#将列表值清理成字符串列表；不是列表时返回空列表，并去掉空字符串。
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _union(first: list[str], second: list[str]) -> list[str]:
    result = list(first)
    for item in second:
        if item not in result:
            result.append(item)
    return result

# 合并两个置信度：一方缺失时用另一方；两方都有时取较大的值。
def _max_confidence(
    first: float | None, second: float | None
) -> float | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)

#只把整数或浮点数转换为浮点数。
# 布尔值虽然在 Python 中属于整数的子类，但这里明确排除。
def _float_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None

#确保属性值是字典。值为 None 时返回空字典；
# 值类型不正确时记录 warning 并返回空字典。
def _mapping_value(
    value: Any,
    owner: str,
    warnings: list[str],
) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    warnings.append(f"{owner!r}: expected a mapping, got {type(value).__name__}")
    return {}

#规范化名称：整理多余空格并转成大小写无关的形式，便于用名称解析关系端点。
def _normalize_name(value: str) -> str:
    return " ".join(str(value).split()).casefold()

#检查审核状态是否属于允许值。
# 合法时保留候选状态；不合法或缺失时使用调用方提供的默认状态。
def _review_status(value: Any, default: ReviewStatus) -> ReviewStatus:
    allowed = {
        "proposed",
        "approved",
        "rejected",
        "merged",
        "deprecated",
        "needs_more_evidence",
    }
    if isinstance(value, str) and value in allowed:
        return value  # type: ignore[return-value]
    return default


__all__ = [
    "CandidateBatchInput",
    "ProjectedDomain",
    "ProjectionError",
    "SourceChunk",
    "project_domain",
    "span_matches",
]
