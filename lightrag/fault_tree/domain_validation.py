"""
加载本体
→ 为实体、事实、规则、证据建索引
→ 检查版本和租户
→ 检查证据及来源
→ 检查证据引用
→ 检查事实和因果规则
→ 检查诊断轨迹
→ 如果有故障树，再检查树结构
→ 返回问题列表
"""

from __future__ import annotations

import hashlib  #哈希摘要，提供SHA-256，MD5等哈希算法
import json
from collections import defaultdict, deque  #带默认值的字典；双端队列
from collections.abc import Iterable, Mapping   #接受任何类型的迭代器；只读映射（类似字典）
from pathlib import Path    #面向对象的路径操作
from typing import Any      #任意类型

import yaml

from lightrag.fault_tree.contracts import (
    DomainBundle,
    Equipment,
    Evidence,
    Fact,
    FaultCauseRule,
    FaultEvent,
    FaultTreeDraft,
    FileAsset,
    UidExpression,
    ValidationIssue,
)
from lightrag.fault_tree.ontology import OntologyRegistry

#被视作因果方向的关系类型，用于故障树循环和可达性检查
CAUSAL_RELATIONS = {"CAUSES", "INPUT_OF", "OUTPUT_TO"}
#标准对照文件中必须出现的标准名称
REQUIRED_STANDARDS = {"IEC 61025", "ISO 14224", "IEC 60812", "ISO 13374"}

#文件格式本身可被当作 UTF-8 文本读取的扩展名。modality 不等于“可读文本”：
#演示包里的巡检表是 Markdown 文件、modality 为 table，但它仍能直接校验 quote。
#PDF/图片/XLSX 这类二进制格式才需要单独提供 FileAsset.text_uri。
_TEXTUAL_EXTENSIONS = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "rst",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "yaml",
        "yml",
        "xml",
        "html",
        "htm",
        "log",
    }
)


def _source_is_textual(asset: FileAsset) -> bool:
    extension = (asset.file_extension or "").strip().lower().lstrip(".")
    if not extension:
        extension = Path(asset.object_uri).suffix.lower().lstrip(".")
    return extension in _TEXTUAL_EXTENSIONS

#对quote计算SHA-256，检查存储的数据是否与证据原文一致
def quote_hash(quote: str) -> str:
    """Return the stable hash stored on an evidence record."""

    digest = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"

#读取JSON文件，并用Pydantic的DomainBundle模型解析
def load_domain_bundle(path: str | Path) -> DomainBundle:
    """Load and model-validate one domain bundle JSON file."""

    return DomainBundle.model_validate_json(Path(path).read_text(encoding="utf-8"))

#整个领域包的总校验入口，读取ontology.yml，建立实体、事实、规则和证据的索引，然后依次检查
# 1.包、本体、租户版本是否一致；
# 2.证据来源、正文、哈希和目标是否有效；
# 3.实体、事实、规则、诊断轨迹引用的证据是否存在；
# 4.事实关系是否符合本体；
# 5.规则引用的故障事件是否存在；
# 6.可选的故障树是否结构正确。
def validate_domain_bundle(
    bundle: DomainBundle,
    pack_dir: str | Path,
) -> list[ValidationIssue]:
    """Return every structural and evidence problem found in ``bundle``.

    The function is intentionally read-only. Callers decide whether a warning
    blocks publication; every error must block it.
    """

    pack_dir = Path(pack_dir)
    ontology = OntologyRegistry.from_file(pack_dir / "ontology.yml")
    issues: list[ValidationIssue] = []

    #把实体按UID整理成字典，方便后续查询；同时检查 UID 重复、类是否在本体中存在、本体版本是否不同，以及同租户的设备位号是否重复
    entities = _index_entities(bundle, ontology, issues)
    #按指定字段给列表建索引，并报告ID重复
    facts = _index_by(bundle.facts, "fact_id", "fact", issues)
    rules = _index_by(bundle.rules, "rule_uid", "rule", issues)
    evidence = _index_by(bundle.evidence, "evidence_id", "evidence", issues)

    #检查bundle的本体版本和schema版本是否匹配，并检查bundle内的实体、事实、规则、故障树和诊断轨迹是否属于同一个租户
    _validate_bundle_header(bundle, ontology, entities, facts, rules, issues)
    #逐条检查证据的租户、quote 哈希和来源文件，然后确认证据的每个 target 都真实存在
    _validate_evidence(bundle, evidence, entities, facts, rules, pack_dir, issues)
    #检查实体、诊断轨迹和每个诊断步骤引用的Evidence ID是否存在
    _validate_entity_evidence_refs(entities, evidence, issues)
    #逐条检查事实：
    # 主体和客体必须是存在的实体；
    # 不允许主体和客体是同一个实体；
    # predicate 必须符合本体声明的关系类型和端点类别；
    # 事实引用的 Evidence 必须存在；
    # 本体版本不一致会报告 warning。
    _validate_facts(bundle, facts, entities, evidence, ontology, issues)
    #逐条检查因果规则
    # 结果故障必须是已有的 FaultEvent；
    # 原因表达式中的所有输入也必须是已有的 FaultEvent；
    # 不能重复列出同一个原因；
    # 不能把结果故障本身也当作它自己的原因；
    # 规则引用的 Evidence 必须存在；
    # 本体版本不同会报告 warning。
    _validate_rules(bundle, rules, entities, evidence, issues)
    _validate_trace(bundle, evidence, issues)
    if bundle.tree is not None:
        _validate_tree(bundle.tree, rules, issues)
    return issues

#检查bundle的本体版本、Schema版本是否与当前定义匹配，
# 并确认实体、事实、规则、故障树和诊断轨迹属于bundle声明的租户
def _validate_bundle_header(
    bundle: DomainBundle,
    ontology: OntologyRegistry,
    entities: dict[str, Any],
    facts: dict[str, Fact],
    rules: dict[str, FaultCauseRule],
    issues: list[ValidationIssue],
) -> None:
    if bundle.ontology_version != ontology.version:
        issues.append(
            _issue(
                "bundle-version-mismatch",
                "error",
                f"bundle uses ontology version {bundle.ontology_version!r}, "
                f"expected {ontology.version!r}",
            )
        )
    if ontology.schema_version and bundle.schema_version != ontology.schema_version:
        issues.append(
            _issue(
                "bundle-schema-version-mismatch",
                "error",
                f"bundle uses schema version {bundle.schema_version!r}, "
                f"expected {ontology.schema_version!r}",
            )
        )

    for kind, records in (
        ("entity", entities.values()),
        ("fact", facts.values()),
        ("rule", rules.values()),
    ):
        for record in records:
            if record.tenant_id != bundle.tenant_id:
                issues.append(
                    _issue(
                        f"{kind}-tenant-mismatch",
                        "error",
                        f"{kind} record belongs to tenant "
                        f"{record.tenant_id!r}, not {bundle.tenant_id!r}",
                        _record_id(record),
                    )
                )

    if bundle.tree is not None and bundle.tree.tenant_id != bundle.tenant_id:
        issues.append(
            _issue(
                "tree-tenant-mismatch",
                "error",
                f"fault-tree draft belongs to tenant {bundle.tree.tenant_id!r}, "
                f"not {bundle.tenant_id!r}",
                bundle.tree.draft_id,
            )
        )
    if bundle.trace is not None and bundle.trace.tenant_id != bundle.tenant_id:
        issues.append(
            _issue(
                "trace-tenant-mismatch",
                "error",
                f"decision trace belongs to tenant {bundle.trace.tenant_id!r}, "
                f"not {bundle.tenant_id!r}",
                bundle.trace.trace_id,
            )
        )

#检查标准对照crosswalk.yml:文件是否存在、是否包含标准映射、必要标准是否都列出，以及映射的本地概念是否能在本体中识别
def validate_crosswalk(pack_dir: str | Path) -> list[ValidationIssue]:
    """Check that the standards crosswalk exists and names known concepts."""

    pack_dir = Path(pack_dir)
    ontology = OntologyRegistry.from_file(pack_dir / "ontology.yml")
    path = pack_dir / "standards" / "crosswalk.yml"
    issues: list[ValidationIssue] = []
    if not path.is_file():
        return [
            _issue(
                "crosswalk-missing",
                "error",
                f"standards crosswalk not found at {path}",
            )
        ]

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    standards = raw.get("standards") if isinstance(raw, dict) else None
    if not isinstance(standards, dict):
        return [
            _issue(
                "crosswalk-invalid",
                "error",
                "standards crosswalk must contain a standards mapping",
            )
        ]

    missing = REQUIRED_STANDARDS - set(standards)
    for standard_id in sorted(missing):
        issues.append(
            _issue(
                "crosswalk-standard-missing",
                "error",
                f"required standard {standard_id} is not mapped",
            )
        )

    known = set(ontology.classes) | set(ontology.relations)
    known.update({"Fact", "FaultTreeDraft", "DecisionStep"})
    ontology_raw = yaml.safe_load(
        (pack_dir / "ontology.yml").read_text(encoding="utf-8")
    )
    if isinstance(ontology_raw, dict):
        known.update(str(item.get("id")) for item in _constraints(ontology_raw))
    for standard_id, definition in standards.items():
        mappings = (definition or {}).get("mappings")
        if not isinstance(mappings, list) or not mappings:
            issues.append(
                _issue(
                    "crosswalk-mappings-missing",
                    "error",
                    f"standard {standard_id} has no mappings",
                )
            )
            continue
        for mapping in mappings:
            local = str((mapping or {}).get("local") or "").strip()
            head = local.split(".", 1)[0]
            if head and head not in known:
                issues.append(
                    _issue(
                        "crosswalk-local-unknown",
                        "warning",
                        f"standard {standard_id} maps unknown local concept {local!r}",
                    )
                )
    return issues

#读取schemas/manifest.json，检查它列出的Schema文件是否存在，文件哈希是否与manifest中记录的一致
def validate_schema_manifest(pack_dir: str | Path) -> list[ValidationIssue]:
    """Verify that checked-in schemas match their manifest hashes."""

    schema_dir = Path(pack_dir) / "schemas"
    manifest_path = schema_dir / "manifest.json"
    if not manifest_path.is_file():
        return [
            _issue(
                "schema-manifest-missing",
                "error",
                f"schema manifest not found at {manifest_path}",
            )
        ]
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, list) or not files:
        return [
            _issue(
                "schema-manifest-invalid",
                "error",
                "schema manifest must contain a non-empty files list",
            )
        ]
    issues: list[ValidationIssue] = []
    for item in files:
        name = str((item or {}).get("name") or "")
        expected = str((item or {}).get("sha256") or "")
        path = schema_dir / name
        if not name or not path.is_file():
            issues.append(
                _issue(
                    "schema-file-missing",
                    "error",
                    f"schema file {name!r} is listed but missing",
                    name,
                )
            )
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected and expected != actual:
            issues.append(
                _issue(
                    "schema-hash-mismatch",
                    "error",
                    f"schema file {name!r} does not match its manifest hash",
                    name,
                )
            )
    return issues

#单独检查一棵故障树草案。输入树和规则，可以选择再传实体
#不传实体时检查树结构，传入实体时，还能检查树中的故障节点是不是对应已有的故障实体
def validate_fault_tree_draft(
    tree: FaultTreeDraft,
    rules: Mapping[str, FaultCauseRule] | Iterable[FaultCauseRule],
    entities: Mapping[str, Any] | None = None,
) -> list[ValidationIssue]:
    """Validate one draft against causal rules and, optionally, domain entities.

    Passing ``entities`` adds the cross-check that every ``FaultEvent`` node
    still names a reviewed fault.  Without it the check is structural only and
    cannot detect a draft whose node labels drifted from the entity store.
    """

    rule_index = _records_by_uid(rules, "rule_uid")
    issues: list[ValidationIssue] = []
    if entities is not None:
        _validate_tree_entities(tree, rule_index, entities, issues)
    _validate_tree(tree, rule_index, issues)
    return issues

#从校验结果总只筛出severity == "error"的问题，warning先不管
def errors_only(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    """Return the subset that must block publication."""

    return [issue for issue in issues if issue.severity == "error"]

#将bundle的实体整理成uid->实体的字典，供后续按ID查找。
#同时检查重复UID，未知实体类别，本体版本差异，以及同租户内重复的设备位号
def _index_entities(
    bundle: DomainBundle,
    ontology: OntologyRegistry,
    issues: list[ValidationIssue],
) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    tags: set[tuple[str, str]] = set()
    for entity in bundle.entities:
        if entity.uid in entities:
            issues.append(
                _issue(
                    "duplicate-entity-uid",
                    "error",
                    f"duplicate entity uid {entity.uid!r}",
                    entity.uid,
                )
            )
            continue
        entities[entity.uid] = entity
        if entity.class_code not in ontology.classes:
            issues.append(
                _issue(
                    "unknown-class",
                    "error",
                    f"entity {entity.uid!r} uses unknown class {entity.class_code!r}",
                    entity.uid,
                )
            )
        if entity.ontology_version != ontology.version:
            issues.append(
                _issue(
                    "entity-version-mismatch",
                    "warning",
                    f"entity {entity.uid!r} uses ontology version "
                    f"{entity.ontology_version!r}, expected {ontology.version!r}",
                    entity.uid,
                )
            )
        if isinstance(entity, Equipment):
            key = (entity.tenant_id, entity.equipment_tag.casefold())
            if key in tags:
                issues.append(
                    _issue(
                        "duplicate-equipment-tag",
                        "error",
                        f"equipment_tag {entity.equipment_tag!r} is not unique "
                        "within its tenant",
                        entity.uid,
                    )
                )
            tags.add(key)
    return entities

#尝试从一条记录里取出它的标识符。
#按顺序查找uid，fact_id,rule_uid等字段，供错误信息指出问题对象
def _record_id(record: Any) -> str | None:
    for field in ("uid", "fact_id", "rule_uid", "evidence_id", "draft_id", "trace_id"):
        value = getattr(record, field, None)
        if value:
            return str(value)
    return None

#按指定字段为记录列表建立字典索引，例如按evidence_id索引证据
def _index_by(
    records: list[Any],
    field: str,
    kind: str,
    issues: list[ValidationIssue],
) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for record in records:
        key = getattr(record, field)
        if key in indexed:
            issues.append(
                _issue(
                    f"duplicate-{kind}-id",
                    "error",
                    f"duplicate {kind} id {key!r}",
                    key,
                )
            )
            continue
        indexed[key] = record
    return indexed

#逐条检查证据的租户ID、quote哈希和源文件，再检查每个evidence target是否指向已存在的对象
#它会调用下面的_validate_evidence_source和_validate_evidence_target
def _validate_evidence(
    bundle: DomainBundle,
    evidence: dict[str, Evidence],
    entities: dict[str, Any],
    facts: dict[str, Fact],
    rules: dict[str, FaultCauseRule],
    pack_dir: Path,
    issues: list[ValidationIssue],
) -> None:
    tree_nodes: dict[str, str] = {}
    step_ids: set[str] = set()
    if bundle.tree is not None:
        tree_nodes = {node.uid: node.class_code for node in bundle.tree.nodes}
    if bundle.trace is not None:
        step_ids = {step.step_id for step in bundle.trace.steps}

    for item in evidence.values():
        if item.tenant_id != bundle.tenant_id:
            issues.append(
                _issue(
                    "evidence-tenant-mismatch",
                    "error",
                    f"evidence {item.evidence_id!r} belongs to another tenant",
                    item.evidence_id,
                )
            )
        if item.quote_hash != quote_hash(item.quote):
            issues.append(
                _issue(
                    "evidence-quote-hash",
                    "error",
                    f"evidence {item.evidence_id!r} has an incorrect quote_hash",
                    item.evidence_id,
                )
            )
        _validate_evidence_source(item, entities, pack_dir, issues)
        for target in item.targets:
            _validate_evidence_target(
                item,
                target.target_type,
                target.target_id,
                entities,
                facts,
                rules,
                tree_nodes,
                step_ids,
                issues,
            )

#检查每个实体的evidence_ids是否都能在证据集合中找到
def _validate_entity_evidence_refs(
    entities: dict[str, Any],
    evidence: dict[str, Evidence],
    issues: list[ValidationIssue],
) -> None:
    for entity in entities.values():
        _validate_evidence_refs(
            entity.uid,
            entity.evidence_ids,
            evidence,
            "entity",
            issues,
        )

#如果bundle有诊断轨迹，就检查轨迹本身和其中每个步骤引用的Evidence ID是否存在
def _validate_trace(
    bundle: DomainBundle,
    evidence: dict[str, Evidence],
    issues: list[ValidationIssue],
) -> None:
    trace = bundle.trace
    if trace is None:
        return
    _validate_evidence_refs(
        trace.trace_id,
        trace.evidence_ids,
        evidence,
        "decision trace",
        issues,
    )
    for step in trace.steps:
        _validate_evidence_refs(
            step.step_id,
            step.evidence_ids,
            evidence,
            "decision step",
            issues,
        )

# 检查单条证据的来源：
# asset_id 必须对应一个文件实体；
# 本地源文件必须存在；
# quote 必须是源文件正文中的原文子串；
# 如果提供字符起止位置，截取结果必须等于 quote；
# 文件 checksum 必须匹配；
# 文件大小不一致时报告警告。
def _validate_evidence_source(
    item: Evidence,
    entities: dict[str, Any],
    pack_dir: Path,
    issues: list[ValidationIssue],
) -> None:
    asset = entities.get(item.asset_id)
    if not isinstance(asset, FileAsset):
        issues.append(
            _issue(
                "evidence-asset-missing",
                "error",
                f"evidence {item.evidence_id!r} references non-file asset "
                f"{item.asset_id!r}",
                item.evidence_id,
            )
        )
        return
    # A text-format source (Markdown, CSV, plain text) is its own transcript;
    # a binary source (PDF, image, XLSX) carries its quotable text in
    # ``text_uri``.  Without it there is nothing UTF-8 to verify against, so
    # fail closed instead of decoding the binary and crashing.
    text_uri = asset.text_uri
    if text_uri is None:
        if not _source_is_textual(asset):
            issues.append(
                _issue(
                    "evidence-text-artifact-missing",
                    "error",
                    f"asset {asset.uid!r} has no text_uri; quotes from a "
                    f"{asset.modality} source cannot be verified",
                    item.evidence_id,
                )
            )
            return
        text_uri = asset.object_uri
    if "://" in text_uri:
        issues.append(
            _issue(
                "evidence-source-remote",
                "warning",
                f"evidence {item.evidence_id!r} points at remote object "
                f"{text_uri!r}; quote presence was not checked",
                item.evidence_id,
            )
        )
        return

    text_path = (pack_dir / text_uri).resolve()
    if not text_path.is_file():
        issues.append(
            _issue(
                "evidence-source-missing",
                "error",
                f"source text {text_path} does not exist",
                item.evidence_id,
            )
        )
        return
    try:
        text = text_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        issues.append(
            _issue(
                "evidence-source-not-text",
                "error",
                f"source {text_path.name} is not valid UTF-8; set text_uri "
                f"to the parsed transcript for asset {asset.uid!r}",
                item.evidence_id,
            )
        )
        return
    if item.quote not in text:
        issues.append(
            _issue(
                "evidence-quote-not-verbatim",
                "error",
                f"evidence {item.evidence_id!r} quote is not a verbatim "
                f"substring of {text_path.name}",
                item.evidence_id,
            )
        )
    if (
        item.char_start is not None
        and item.char_end is not None
        and text[item.char_start : item.char_end] != item.quote
    ):
        issues.append(
            _issue(
                "evidence-span-mismatch",
                "error",
                f"evidence {item.evidence_id!r} char span does not match quote",
                item.evidence_id,
            )
        )

    # checksum and size describe the original binary, not the text artifact.
    if "://" in asset.object_uri:
        issues.append(
            _issue(
                "asset-checksum-unverified",
                "warning",
                f"original object {asset.object_uri!r} is remote; checksum "
                f"was not verified for asset {asset.uid!r}",
                asset.uid,
            )
        )
        return
    source = (pack_dir / asset.object_uri).resolve()
    if not source.is_file():
        issues.append(
            _issue(
                "asset-source-missing",
                "error",
                f"original source file {source} does not exist",
                asset.uid,
            )
        )
        return
    checksum = asset.checksum.removeprefix("sha256:")
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if checksum and checksum != actual:
        issues.append(
            _issue(
                "asset-checksum-mismatch",
                "error",
                f"file {source.name} checksum does not match asset {asset.uid!r}",
                asset.uid,
            )
        )
    if asset.file_size != source.stat().st_size:
        issues.append(
            _issue(
                "asset-size-mismatch",
                "warning",
                f"file {source.name} size differs from asset {asset.uid!r}",
                asset.uid,
            )
        )

#检查证据指向的对象是否存在
def _validate_evidence_target(
    item: Evidence,
    target_type: str,
    target_id: str,
    entities: dict[str, Any],
    facts: dict[str, Fact],
    rules: dict[str, FaultCauseRule],
    tree_nodes: dict[str, str],
    step_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    pools = {
        "entity": entities,
        "fact": facts,
        "rule": rules,
        "logic_gate": tree_nodes,
        "decision_step": step_ids,
    }
    if target_type not in pools:
        issues.append(
            _issue(
                "evidence-target-type",
                "error",
                f"evidence {item.evidence_id!r} has unknown target type "
                f"{target_type!r}",
                item.evidence_id,
            )
        )
        return
    if target_id not in pools[target_type]:
        issues.append(
            _issue(
                "evidence-target-missing",
                "error",
                f"evidence {item.evidence_id!r} targets missing "
                f"{target_type} {target_id!r}",
                item.evidence_id,
            )
        )
        return
    if target_type == "logic_gate" and tree_nodes[target_id] != "LogicGate":
        issues.append(
            _issue(
                "evidence-target-class",
                "error",
                f"evidence {item.evidence_id!r} targets {target_id!r} as a "
                "logic gate, but it is a "
                f"{tree_nodes[target_id]}",
                item.evidence_id,
            )
        )

# 检查每条事实：
# 主体、客体是否存在；
# 是否错误地把实体连到自身；
# 关系名称和两端实体类别是否符合本体；
# 事实引用的证据是否存在；
# 事实的本体版本是否与 bundle 一致。
def _validate_facts(
    bundle: DomainBundle,
    facts: dict[str, Fact],
    entities: dict[str, Any],
    evidence: dict[str, Evidence],
    ontology: OntologyRegistry,
    issues: list[ValidationIssue],
) -> None:
    for fact in facts.values():
        subject = entities.get(fact.subject_uid)
        target = entities.get(fact.object_uid)
        if subject is None or target is None:
            issues.append(
                _issue(
                    "fact-endpoint-missing",
                    "error",
                    f"fact {fact.fact_id!r} references an unknown entity",
                    fact.fact_id,
                )
            )
            continue
        if fact.subject_uid == fact.object_uid:
            issues.append(
                _issue(
                    "fact-self-loop",
                    "error",
                    f"fact {fact.fact_id!r} is a self-loop",
                    fact.fact_id,
                )
            )
        try:
            ontology.validate_relation(
                fact.predicate,
                subject.class_code,
                target.class_code,
            )
        except ValueError as exc:
            issues.append(
                _issue(
                    "fact-relation-invalid",
                    "error",
                    f"fact {fact.fact_id!r}: {exc}",
                    fact.fact_id,
                )
            )
        _validate_evidence_refs(
            fact.fact_id,
            fact.evidence_ids,
            evidence,
            "fact",
            issues,
        )
        if fact.ontology_version != bundle.ontology_version:
            issues.append(
                _issue(
                    "fact-version-mismatch",
                    "warning",
                    f"fact {fact.fact_id!r} uses ontology version "
                    f"{fact.ontology_version!r}",
                    fact.fact_id,
                )
            )

# 检查每条因果规则：
# 结果必须是已有的 FaultEvent；
# 原因表达式中的所有输入都必须是已有的 FaultEvent；
# 原因事件不能重复；
# 结果故障不能同时作为自己的原因；
# 规则引用的证据必须存在；
# 规则的本体版本应与 bundle 对应。
# 它用 _expression_uids 把递归表达式里的叶子故障 ID 全部取出来。
def _validate_rules(
    bundle: DomainBundle,
    rules: dict[str, FaultCauseRule],
    entities: dict[str, Any],
    evidence: dict[str, Evidence],
    issues: list[ValidationIssue],
) -> None:
    for rule in rules.values():
        effect = entities.get(rule.effect_fault_uid)
        if not isinstance(effect, FaultEvent):
            issues.append(
                _issue(
                    "rule-effect-missing",
                    "error",
                    f"rule {rule.rule_uid!r} effect is not a FaultEvent",
                    rule.rule_uid,
                )
            )
        event_uids = _expression_uids(rule.expression)
        if len(event_uids) != len(set(event_uids)):
            issues.append(
                _issue(
                    "rule-duplicate-input",
                    "error",
                    f"rule {rule.rule_uid!r} contains duplicate event inputs",
                    rule.rule_uid,
                )
            )
        if rule.effect_fault_uid in event_uids:
            issues.append(
                _issue(
                    "rule-input-is-effect",
                    "error",
                    f"rule {rule.rule_uid!r} lists its own effect "
                    f"{rule.effect_fault_uid!r} as an input",
                    rule.rule_uid,
                )
            )
        for event_uid in event_uids:
            if not isinstance(entities.get(event_uid), FaultEvent):
                issues.append(
                    _issue(
                        "rule-input-missing",
                        "error",
                        f"rule {rule.rule_uid!r} references missing FaultEvent "
                        f"{event_uid!r}",
                        rule.rule_uid,
                    )
                )
        _validate_evidence_refs(
            rule.rule_uid,
            rule.evidence_ids,
            evidence,
            "rule",
            issues,
        )
        if rule.ontology_version != bundle.ontology_version:
            issues.append(
                _issue(
                    "rule-version-mismatch",
                    "warning",
                    f"rule {rule.rule_uid!r} uses ontology version "
                    f"{rule.ontology_version!r}",
                    rule.rule_uid,
                )
            )

#把规则集合统一转换成按 UID 索引的字典
def _records_by_uid(
    records: Mapping[str, FaultCauseRule] | Iterable[FaultCauseRule],
    field: str,
) -> dict[str, FaultCauseRule]:
    if isinstance(records, Mapping):
        return dict(records)
    indexed: dict[str, FaultCauseRule] = {}
    for record in records:
        indexed[str(getattr(record, field))] = record
    return indexed

#在调用者提供实体集合时，检查故障树节点和真实实体是否对应：
# 故障节点是否确实是已有的 FaultEvent、租户是否相同、版本和标签是否合理。
def _validate_tree_entities(
    tree: FaultTreeDraft,
    rules: dict[str, FaultCauseRule],
    entities: Mapping[str, Any],
    issues: list[ValidationIssue],
) -> None:
    for node in tree.nodes:
        if node.class_code == "FaultEvent":
            entity = entities.get(node.uid)
            if not isinstance(entity, FaultEvent):
                issues.append(
                    _issue(
                        "tree-node-entity-missing",
                        "error",
                        f"tree node {node.uid!r} does not name a reviewed "
                        "FaultEvent entity",
                        node.uid,
                    )
                )
                continue
            if entity.tenant_id != tree.tenant_id:
                issues.append(
                    _issue(
                        "tree-node-tenant-mismatch",
                        "error",
                        f"tree node {node.uid!r} belongs to tenant "
                        f"{entity.tenant_id!r}, not {tree.tenant_id!r}",
                        node.uid,
                    )
                )
            if entity.ontology_version != tree.ontology_version:
                issues.append(
                    _issue(
                        "tree-node-version-mismatch",
                        "warning",
                        f"tree node {node.uid!r} uses ontology version "
                        f"{entity.ontology_version!r}, not "
                        f"{tree.ontology_version!r}",
                        node.uid,
                    )
                )
            if node.label not in {entity.fault_name, entity.canonical_name}:
                issues.append(
                    _issue(
                        "tree-node-label-mismatch",
                        "warning",
                        f"tree node {node.uid!r} label {node.label!r} differs "
                        f"from entity name {entity.canonical_name!r}",
                        node.uid,
                    )
                )
            continue
        if node.class_code == "FaultCauseRule" and node.uid not in rules:
            issues.append(
                _issue(
                    "tree-rule-node-missing",
                    "error",
                    f"tree node {node.uid!r} does not name a known FaultCauseRule",
                    node.uid,
                )
            )

#故障树结构校验的总调度器，会检查：
#1.节点ID是否重复
#2.顶事件是否是FaultEvent
#3.每条边的端点是否存在
#4.边类型和端点类别是否相符
#5.逻辑门输入、输出是否合规
#6.因果关系图是否有环
#7.树中相关节点是否能从顶事件追溯到
#8.顶事件是否错误地指向它的上游原因
def _validate_tree(
    tree: FaultTreeDraft,
    rules: dict[str, FaultCauseRule],
    issues: list[ValidationIssue],
) -> None:
    issue_start = len(issues)
    nodes = {node.uid: node for node in tree.nodes}
    if len(nodes) != len(tree.nodes):
        issues.append(
            _issue(
                "tree-duplicate-node",
                "error",
                "fault-tree draft contains duplicate node uids",
                tree.draft_id,
            )
        )
    top = nodes.get(tree.top_event_uid)
    if top is None or top.class_code != "FaultEvent":
        issues.append(
            _issue(
                "tree-top-event-missing",
                "error",
                f"top event {tree.top_event_uid!r} is not a FaultEvent node",
                tree.draft_id,
            )
        )

    incoming_gate_inputs: dict[str, int] = defaultdict(int)
    outgoing_gate_outputs: dict[str, int] = defaultdict(int)
    causal_edges: list[tuple[str, str]] = []
    for edge in tree.edges:
        source = nodes.get(edge.source_uid)
        target = nodes.get(edge.target_uid)
        if source is None or target is None:
            issues.append(
                _issue(
                    "tree-edge-endpoint",
                    "error",
                    f"edge {edge.source_uid!r} -> {edge.target_uid!r} has a "
                    "missing endpoint",
                    tree.draft_id,
                )
            )
            continue
        _validate_tree_edge(edge, source, target, rules, issues)
        if edge.relation_code == "INPUT_OF":
            incoming_gate_inputs[edge.target_uid] += 1
        elif edge.relation_code == "OUTPUT_TO":
            outgoing_gate_outputs[edge.source_uid] += 1
        if edge.relation_code in CAUSAL_RELATIONS:
            causal_edges.append((edge.source_uid, edge.target_uid))

    for node in tree.nodes:
        if node.class_code == "LogicGate":
            _validate_gate(
                node,
                incoming_gate_inputs[node.uid],
                outgoing_gate_outputs[node.uid],
                rules,
                issues,
            )

    if _has_cycle(set(nodes), causal_edges):
        issues.append(
            _issue(
                "tree-cycle",
                "error",
                "causal graph contains a directed cycle",
                tree.draft_id,
            )
        )
    if top is not None:
        _validate_reachability(tree, nodes, causal_edges, issues)
        if any(source == tree.top_event_uid for source, _ in causal_edges):
            issues.append(
                _issue(
                    "tree-top-has-parent",
                    "error",
                    f"top event {tree.top_event_uid!r} has an outgoing causal edge",
                    tree.draft_id,
                )
            )

    computed_errors = errors_only(issues[issue_start:])
    if tree.validation.valid and computed_errors:
        issues.append(
            _issue(
                "tree-validation-flag",
                "warning",
                "draft declares valid=true but validation found errors",
                tree.draft_id,
            )
        )

#检查一条故障树边的类别和端点：
#HAS_CAUSE_RULE: 故障事件指向规则，且规则的结果故障必须是这个事件
#INPUT_OF: 输入可以是故障事件或逻辑门，接收端必须是逻辑门
#OUTPUT_TO: 逻辑门可以输出到故障时间或父逻辑门，嵌套们要属于同一规则
#CAUSES: 两端都必须是故障事件
def _validate_tree_edge(
    edge: Any,
    source: Any,
    target: Any,
    rules: dict[str, FaultCauseRule],
    issues: list[ValidationIssue],
) -> None:
    if edge.relation_code == "HAS_CAUSE_RULE":
        if source.class_code != "FaultEvent":
            issues.append(
                _issue(
                    "tree-rule-source",
                    "error",
                    "HAS_CAUSE_RULE source must be a FaultEvent",
                    edge.source_uid,
                )
            )
        if target.class_code != "FaultCauseRule":
            issues.append(
                _issue(
                    "tree-rule-target",
                    "error",
                    "HAS_CAUSE_RULE target must be a FaultCauseRule",
                    edge.target_uid,
                )
            )
        rule = rules.get(edge.target_uid)
        if rule is None:
            issues.append(
                _issue(
                    "tree-rule-missing",
                    "error",
                    f"HAS_CAUSE_RULE references unknown rule {edge.target_uid!r}",
                    edge.target_uid,
                )
            )
        elif rule.effect_fault_uid != edge.source_uid:
            issues.append(
                _issue(
                    "tree-rule-effect",
                    "error",
                    f"rule {rule.rule_uid!r} produces "
                    f"{rule.effect_fault_uid!r}, not {edge.source_uid!r}",
                    edge.source_uid,
                )
            )
        return
    if edge.relation_code == "INPUT_OF":
        if target.class_code != "LogicGate":
            issues.append(
                _issue(
                    "tree-input-target",
                    "error",
                    "INPUT_OF target must be a LogicGate",
                    edge.target_uid,
                )
            )
        if source.class_code not in {"FaultEvent", "LogicGate"}:
            issues.append(
                _issue(
                    "tree-input-source",
                    "error",
                    "INPUT_OF source must be a FaultEvent or LogicGate",
                    edge.source_uid,
                )
            )
    if edge.relation_code == "OUTPUT_TO":
        if source.class_code != "LogicGate":
            issues.append(
                _issue(
                    "tree-output-source",
                    "error",
                    "OUTPUT_TO source must be a LogicGate",
                    edge.source_uid,
                )
            )
        if target.class_code not in {"FaultEvent", "LogicGate"}:
            issues.append(
                _issue(
                    "tree-output-target",
                    "error",
                    "OUTPUT_TO target must be a FaultEvent or LogicGate",
                    edge.target_uid,
                )
            )
        source_rule_uid = source.properties.get("rule_uid")
        if target.class_code == "LogicGate":
            # A nested gate feeds its parent gate; both belong to the same
            # rule, and only the root gate feeds the rule's effect event.
            target_rule_uid = target.properties.get("rule_uid")
            if not source_rule_uid or not target_rule_uid:
                issues.append(
                    _issue(
                        "tree-output-gate-rule",
                        "error",
                        f"gate {source.uid!r} outputs to gate "
                        f"{target.uid!r} without a rule_uid on both gates",
                        edge.target_uid,
                    )
                )
            elif source_rule_uid != target_rule_uid:
                issues.append(
                    _issue(
                        "tree-output-gate-rule",
                        "error",
                        f"gate {source.uid!r} (rule {source_rule_uid!r}) "
                        f"outputs to gate {target.uid!r} from rule "
                        f"{target_rule_uid!r}",
                        edge.target_uid,
                    )
                )
        else:
            if source.properties.get("expression_path"):
                issues.append(
                    _issue(
                        "tree-nested-gate-output",
                        "error",
                        f"nested gate {source.uid!r} at expression path "
                        f"{source.properties['expression_path']!r} must "
                        "output to its parent LogicGate, not to a FaultEvent",
                        edge.target_uid,
                    )
                )
            rule = rules.get(source_rule_uid)
            if rule is not None and rule.effect_fault_uid != edge.target_uid:
                issues.append(
                    _issue(
                        "tree-output-effect",
                        "error",
                        f"gate {source.uid!r} belongs to rule "
                        f"{rule.rule_uid!r}, whose effect is "
                        f"{rule.effect_fault_uid!r}, not {edge.target_uid!r}",
                        edge.target_uid,
                    )
                )
    if edge.relation_code == "CAUSES":
        if source.class_code != "FaultEvent" or target.class_code != "FaultEvent":
            issues.append(
                _issue(
                    "tree-causes-endpoint",
                    "error",
                    "CAUSES must connect two FaultEvent nodes",
                    edge.source_uid,
                )
            )

#检查逻辑门本身：类型必须为 AND 或 OR、至少有两个输入、
# 实际输入数要与 input_count 一致、必须恰有一个输出，并且引用的规则必须存在。
def _validate_gate(
    node: Any,
    actual_inputs: int,
    actual_outputs: int,
    rules: dict[str, FaultCauseRule],
    issues: list[ValidationIssue],
) -> None:
    gate_type = node.properties.get("gate_type")
    if gate_type not in {"AND", "OR"}:
        issues.append(
            _issue(
                "gate-type-invalid",
                "error",
                f"logic gate {node.uid!r} has invalid gate_type {gate_type!r}",
                node.uid,
            )
        )
    if actual_inputs < 2:
        issues.append(
            _issue(
                "gate-too-few-inputs",
                "error",
                f"logic gate {node.uid!r} has {actual_inputs} inputs",
                node.uid,
            )
        )
    declared_inputs = node.properties.get("input_count")
    if declared_inputs != actual_inputs:
        issues.append(
            _issue(
                "gate-input-count",
                "error",
                f"logic gate {node.uid!r} declares input_count="
                f"{declared_inputs!r} but has {actual_inputs} INPUT_OF edges",
                node.uid,
            )
        )
    if actual_outputs != 1:
        issues.append(
            _issue(
                "gate-output-count",
                "error",
                f"logic gate {node.uid!r} has {actual_outputs} OUTPUT_TO edges",
                node.uid,
            )
        )
    rule_uid = node.properties.get("rule_uid")
    rule = rules.get(rule_uid)
    if rule is None:
        issues.append(
            _issue(
                "gate-rule-missing",
                "error",
                f"logic gate {node.uid!r} references unknown rule {rule_uid!r}",
                node.uid,
            )
        )
    else:
        declared_output = node.properties.get("output_fault_uid")
        if declared_output and declared_output != rule.effect_fault_uid:
            issues.append(
                _issue(
                    "gate-output-fault",
                    "error",
                    f"logic gate {node.uid!r} declares output "
                    f"{declared_output!r}, but its rule produces "
                    f"{rule.effect_fault_uid!r}",
                    node.uid,
                )
            )

#从顶事件沿因果边反向遍历，检查每个故障事件和逻辑门是否都能追溯到顶事件。
# 无法到达的相关节点会被报告为孤立节点。
def _validate_reachability(
    tree: FaultTreeDraft,
    nodes: dict[str, Any],
    causal_edges: list[tuple[str, str]],
    issues: list[ValidationIssue],
) -> None:
    reverse: dict[str, list[str]] = defaultdict(list)
    for source, target in causal_edges:
        reverse[target].append(source)
    seen = {tree.top_event_uid}
    queue = deque([tree.top_event_uid])
    while queue:
        current = queue.popleft()
        for parent in reverse.get(current, []):
            if parent not in seen:
                seen.add(parent)
                queue.append(parent)
    for uid, node in nodes.items():
        if node.class_code in {"FaultEvent", "LogicGate"} and uid not in seen:
            issues.append(
                _issue(
                    "tree-unreachable-node",
                    "error",
                    f"node {uid!r} is not reachable from the top event",
                    uid,
                )
            )

#通用的证据引用检查器。传入记录 ID 和证据 ID 列表，逐个确认引用的证据存在。
# 实体、事实、规则和诊断轨迹等检查都会复用它。
def _validate_evidence_refs(
    owner_id: str,
    evidence_ids: list[str],
    evidence: dict[str, Evidence],
    owner_kind: str,
    issues: list[ValidationIssue],
) -> None:
    for evidence_id in evidence_ids:
        if evidence_id not in evidence:
            issues.append(
                _issue(
                    "evidence-reference-missing",
                    "error",
                    f"{owner_kind} {owner_id!r} references missing evidence "
                    f"{evidence_id!r}",
                    owner_id,
                )
            )

#递归遍历 UidExpression，收集所有叶子上的故障事件 UID。
# 它不判断这些 UID 是否有效，只负责取出来供其他校验函数检查。
def _expression_uids(expression: UidExpression) -> list[str]:
    if expression.event_uid:
        return [expression.event_uid]
    result: list[str] = []
    for child in expression.children:
        result.extend(_expression_uids(child))
    return result

#是否有环
def _has_cycle(nodes: set[str], edges: list[tuple[str, str]]) -> bool:
    outgoing: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        outgoing[source].append(target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for target in outgoing.get(node, []):
            if visit(target):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in nodes)

#从解析后的 ontology YAML 字典中取出 constraints 列表，只保留字典格式的条目。
#供 validate_crosswalk 识别标准映射所引用的约束概念。
def _constraints(raw: dict[str, Any]) -> list[dict[str, Any]]:
    constraints = raw.get("constraints")
    if not isinstance(constraints, list):
        return []
    return [item for item in constraints if isinstance(item, dict)]

#创建一个统一格式的 ValidationIssue，包含错误代码、严重程度、说明文字和可选的目标 ID。
#它是各校验函数共同使用的“报告问题”小工具。
def _issue(
    code: str,
    severity: str,
    message: str,
    target_id: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        target_id=target_id,
    )
