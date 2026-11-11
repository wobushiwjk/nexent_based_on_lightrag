"""Read-only repository for validated domain bundles.

The repository is deliberately snapshot-based: it indexes the JSON bundles that
already passed :func:`validate_domain_bundle`, never a live LightRAG store.  W3
can add a storage-backed implementation behind the same lookup surface without
changing the tool contracts.
"""

from __future__ import annotations  #让文件里所有类型注解在定义时不被求值，而是被当作字符串保存，等到真正需要时才解析

import logging  #在bundle校验出现warning时记录信息，而不是让程序崩溃
from collections import defaultdict     #普通dict访问不存在的键会抛Error，而defaultdict会自动创建一个空列表，省去if key not in dict: dict[key] = []的样板代码
from collections.abc import Iterable, Mapping   #Iterable表示可迭代对象，Mapping表示映射类型（如dict），用于类型注解
from dataclasses import dataclass   #tenantIndex用它定义frozen=True，表示实例一旦创建，字段就不能再被修改
from pathlib import Path    #替代os.path，提供面向对象的路径操作
from typing import Any  #任何类型

import yaml #用来加载ontology.yml文件
from pydantic import ValidationError    #结构化数据校验的异常类型

from lightrag.fault_tree.contracts import (
    DecisionTrace,
    DomainBundle,
    EntityRecord,
    Equipment,
    Evidence,
    Fact,
    FaultCauseRule,
    FaultEvent,
    FaultTreeDraft,
    UidExpression,
)
from lightrag.fault_tree.domain_validation import (
    errors_only,
    load_domain_bundle,
    validate_domain_bundle,
)
from lightrag.fault_tree.ontology import OntologyRegistry
from mcp_server.errors import DomainError, DomainRepositoryError, NOT_FOUND

logger = logging.getLogger(__name__)


@dataclass(frozen=True) #表示这是一个主要用来装饰数据的类，python会自动生成__init__、__repr__、__eq__等方法，frozen=True表示实例一旦创建，字段就不能再被修改
class TenantIndex:
    """Immutable-by-convention indexes for one tenant's bundle."""

    tenant_id: str  #类型注解
    ontology_version: str
    bundle: DomainBundle
    entities: Mapping[str, EntityRecord]    #表示用字符串做键，用EntityRecord做值的映射，类似字典
    evidence: Mapping[str, Evidence]
    facts: Mapping[str, Fact]
    rules: Mapping[str, FaultCauseRule]
    tree: FaultTreeDraft | None
    trace: DecisionTrace | None
    equipment_by_tag: Mapping[str, str]
    evidence_by_target: Mapping[tuple[str, str], tuple[str, ...]]
    facts_by_subject: Mapping[str, tuple[Fact, ...]]
    facts_by_object: Mapping[str, tuple[Fact, ...]]
    rules_by_effect: Mapping[str, tuple[FaultCauseRule, ...]]
    rules_by_input: Mapping[str, tuple[FaultCauseRule, ...]]

    #self表示当前这个对象，uid是方法的参数，返回值是Equipment对象或None
    def equipment(self, uid: str) -> Equipment | None:  
        entity = self.entities.get(uid) #按UID查记录
        return entity if isinstance(entity, Equipment) else None    #找到的记录是设备就返回设备，否则返回None

    def faults(self) -> Iterable[FaultEvent]:
        return (
            entity
            for entity in self.entities.values()
            if isinstance(entity, FaultEvent)
        )


class DomainRepository:
    """Serve one or more validated tenant snapshots from a domain pack."""
    #这是类的初始化方法，创建对象时会自动执行
    def __init__(
        self,
        pack_dir: str | Path,
        bundles: Iterable[DomainBundle],    #可以传一组DomainBundle对象，也可以传一个生成器，返回值是None
        *,  #后面的参数必须用参数名制定，不能只按位置传？
        default_tenant: str | None = None,  
    ) -> None:
        #加载本体文件
        self.pack_dir = Path(pack_dir).resolve()
        ontology_path = self.pack_dir / "ontology.yml"  
        #检查文件并读取
        if not ontology_path.is_file():
            raise DomainRepositoryError(
                f"ontology profile not found at {ontology_path}"
            )
        try:
            self.ontology = OntologyRegistry.from_file(ontology_path)
            raw_ontology = yaml.safe_load(ontology_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise DomainRepositoryError(
                f"cannot load ontology profile {ontology_path}: {exc}"
            ) from exc
        if not isinstance(raw_ontology, dict):
            raise DomainRepositoryError(
                f"ontology profile {ontology_path} must contain a mapping"
            )
        self.ontology_document: dict[str, Any] = raw_ontology

        bundle_list = list(bundles)
        if not bundle_list:
            raise DomainRepositoryError("at least one domain bundle is required")

        indexes: dict[str, TenantIndex] = {}    #indexes是一个字典，类型是租户ID->租户索引
        for bundle in bundle_list:
            issues = validate_domain_bundle(bundle, self.pack_dir)  #检查bundle是否符合领域数据规则
            errors = errors_only(issues)    #从所有检查结果中筛出错误
            if errors:
                #把多个错误信息拼接成一个字符串，最多显示前5个错误
                summary = "; ".join(    
                    f"{issue.code} [{issue.target_id or '-'}]: {issue.message}"
                    for issue in errors[:5]
                )
                raise DomainRepositoryError(
                    f"bundle {bundle.bundle_version!r} for tenant "
                    f"{bundle.tenant_id!r} failed validation: {summary}",
                    issues=issues,
                )
            for issue in issues:
                #如果只有warning，就记录日志而不是抛异常
                logger.warning(
                    "bundle %s validation warning %s [%s]: %s",
                    bundle.bundle_version,
                    issue.code,
                    issue.target_id or "-",
                    issue.message,
                )
            #最后检查是否重复使用了同一个租户ID，如果重复就抛异常
            if bundle.tenant_id in indexes:
                raise DomainRepositoryError(
                    f"duplicate bundle for tenant {bundle.tenant_id!r}"
                )
            #创建索引，确保每个租户只能加载一份bundle，避免数据冲突
            indexes[bundle.tenant_id] = _build_index(bundle)

        self._tenants = indexes
        if default_tenant is not None:
            default_tenant = default_tenant.strip() or None
        if default_tenant is not None and default_tenant not in indexes:
            raise DomainRepositoryError(
                f"default tenant {default_tenant!r} has no loaded bundle"
            )
        if default_tenant is None and len(indexes) == 1:
            default_tenant = next(iter(indexes))
        self.default_tenant = default_tenant

    @classmethod    #表示这是类方法，cls代表DomainRepository这个类
    #从文件路径加载
    def from_paths(
        cls,
        pack_dir: str | Path,
        bundle_paths: Iterable[str | Path],
        *,
        default_tenant: str | None = None,
    ) -> "DomainRepository":
        """Load bundles from JSON files and validate them before serving."""

        bundles: list[DomainBundle] = []
        paths = [Path(path) for path in bundle_paths]
        if not paths:
            raise DomainRepositoryError("at least one domain bundle path is required")
        for path in paths:
            if not path.is_file():
                raise DomainRepositoryError(f"domain bundle not found at {path}")
            try:
                bundles.append(load_domain_bundle(path))
            except (OSError, ValueError, ValidationError) as exc:
                raise DomainRepositoryError(
                    f"cannot load domain bundle {path}: {exc}"
                ) from exc
        return cls(pack_dir, bundles, default_tenant=default_tenant)    #创建并返回了一个DomainRepository对象

    @property
    def tenants(self) -> tuple[str, ...]:
        return tuple(sorted(self._tenants))

    #取指定租户的数据
    def tenant_index(self, tenant_id: str | None) -> TenantIndex:   
        """Return the requested tenant or a non-enumerating not-found error."""
        #如果tenant_id是None，先用空字符串代替
        effective = (tenant_id or "").strip() or self.default_tenant
        if not effective:
            raise DomainError(
                "invalid_argument",
                "tenant_id is required when more than one tenant is loaded",
                {"parameter": "tenant_id"},
            )
        index = self._tenants.get(effective)
        if index is None:
            raise DomainError(
                NOT_FOUND,
                f"tenant {effective!r} is not available",
                {"tenant_id": effective},
            )
        return index    #找得到就返回该租户的索引

#把数据整理成查询速查表
def _build_index(
    bundle: DomainBundle,
    *,
    evidence: Mapping[str, Evidence] | None = None,
    evidence_by_target: Mapping[tuple[str, str], tuple[str, ...]] | None = None,
) -> TenantIndex:
    entities: dict[str, EntityRecord] = {   #字典推导式
        entity.uid: entity for entity in bundle.entities
    }
    facts = {fact.fact_id: fact for fact in bundle.facts}
    rules = {rule.rule_uid: rule for rule in bundle.rules}

    #defaultdict(list)是一种特殊字典，第一次访问某个不存在的键时，会自动创建一个空列表
    equipment_by_tag: dict[str, str] = {}
    #target_index是一个defaultdict,key是tuple[str,str], vaule是list[str]
    #之所以defaultdict的参数为list，是因为value的类型是list，这有当value为空时，才会有target_index[("entity", "EQ-1")] = []
    target_index: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    facts_by_subject: defaultdict[str, list[Fact]] = defaultdict(list)
    facts_by_object: defaultdict[str, list[Fact]] = defaultdict(list)
    rules_by_effect: defaultdict[str, list[FaultCauseRule]] = defaultdict(list)
    rules_by_input: defaultdict[str, list[FaultCauseRule]] = defaultdict(list)

    #建立设备位号索引
    for entity in bundle.entities:
        if isinstance(entity, Equipment):   #判断当前实体是不是设备
            #setdefault(键，值)表示只有这个键还不存在时才放入新值
            equipment_by_tag.setdefault(entity.equipment_tag.casefold(), entity.uid)    #casefold把设备位号转成适合忽略大小写比较的形式
    #建立证据目标-->证据ID 索引
    for item in bundle.evidence:    
        #逐条处理这条证据关联的目标
        for target in item.targets:
            target_index[(target.target_type, target.target_id)].append(
                item.evidence_id
            )
    #按事实的主体和客体建立索引
    for fact in bundle.facts:
        facts_by_subject[fact.subject_uid].append(fact)
        facts_by_object[fact.object_uid].append(fact)
    #按规则的结果和输入建立索引
    for rule in bundle.rules:
        rules_by_effect[rule.effect_fault_uid].append(rule)
        for event_uid in _expression_event_uids(rule.expression):
            rules_by_input[event_uid].append(rule)
    #选择证据数据来源
    #Mapping[str, Evidence]表示key的类型为字符串，value的类型为Evidence
    #如果直接传入了evidence，它应当是这种结构
    # {
    # "EVIDENCE-1": Evidence(...),
    # "EVIDENCE-2": Evidence(...),
    # }   
    #所以直接传给resolved_evidence就行，只有没有传入时才会需要按字典格式创建
    resolved_evidence: Mapping[str, Evidence] = (
        evidence    
        if evidence is not None 
        else {item.evidence_id: item for item in bundle.evidence}
    )
    resolved_targets: Mapping[tuple[str, str], tuple[str, ...]] = (
        evidence_by_target
        if evidence_by_target is not None
        else {
            key: tuple(sorted(set(values)))
            for key, values in target_index.items()
        }
    )

    # bundle 原始数据
    #   ├─ 按 UID 整理实体、事实、规则
    #   ├─ 按设备位号索引设备
    #   ├─ 按目标索引证据
    #   ├─ 按主体/客体索引事实
    #   └─ 按结果/输入索引规则
    return TenantIndex(
        tenant_id=bundle.tenant_id,
        ontology_version=bundle.ontology_version,
        bundle=bundle,
        entities=entities,
        evidence=resolved_evidence,
        facts=facts,
        rules=rules,
        tree=bundle.tree,
        trace=bundle.trace,
        equipment_by_tag=equipment_by_tag,
        evidence_by_target=resolved_targets,
        facts_by_subject={
            key: tuple(values) for key, values in facts_by_subject.items()
        },
        facts_by_object={key: tuple(values) for key, values in facts_by_object.items()},
        rules_by_effect={key: tuple(values) for key, values in rules_by_effect.items()},
        rules_by_input={key: tuple(values) for key, values in rules_by_input.items()},
    )

#递归找出表达式里的事件UID
def _expression_event_uids(expression: UidExpression) -> list[str]:
    if expression.event_uid:
        return [expression.event_uid]
    result: list[str] = []
    for child in expression.children:
        result.extend(_expression_event_uids(child))    #extend为python列表里的方法，把另一个可迭代对象里的元素逐个追加到当前列表的末尾
    return result


__all__ = ["DomainRepository", "DomainRepositoryError", "TenantIndex"]
