
# 故障树本体集成

本包在现有 LightRAG JSON 实体抽取流水线之上增加了一个故障树配置档（profile）。它不替换 `extract_entities`、`merge_nodes_and_edges`、LLM 缓存或图存储后端。

规范的领域数据位于 [`domain_packs/industrial_fault/`](https://../domain_packs/industrial_fault/README.md)：`ontology.yml`、标准对照表、JSON Schema 和种子文档。用 `python scripts/validate_domain.py` 校验它。

## 组件

* `../../prompts/entity_type/industrial_fault.yml`：本体类、提示词可见的属性和关系，以及 JSON 抽取示例。
* `schemas.py`：用于校验抽取载荷的 Pydantic 契约。
* `contracts.py`：LightRAG、MCP 和 Nexent 之间交换的规范记录（实体、证据、事实、规则、树草稿、决策轨迹）。
* `domain_validation.py`：根据本体、源文档和已检入的 JSON Schema 校验一个自包含的领域包。
* `ontology.py`：加载配置档并校验关系的定义域（domain）和值域（range）。
* `validator.py`：消费解析器载荷，验证证据和关系端点，并可选择输出一批规范化候选。
* `graph.py`：将已验证的候选投影为 `Equipment`、`FaultEvent`、`FaultCauseRule` 和 `LogicGate` 图记录。

## 配置

**python**

```
from lightrag import LightRAG
from lightrag.fault_tree import FaultCandidateValidator

validator = FaultCandidateValidator.from_prompt_profile(
    "industrial_fault.yml"
)

rag = LightRAG(
    ...,
    entity_extraction_use_json=True,
    addon_params={"entity_type_prompt_file": "industrial_fault.yml"},
    kg_extraction_validator=validator,
)
```

`PROMPT_DIR` 默认为 `./prompts`，因此配置档从 `prompts/entity_type/industrial_fault.yml` 解析。

## 抽取载荷

现有 JSON 抽取格式保持不变。领域字段保留在每个实体或关系的标量字段 `_fault_payload` 中：

**json**

```
{
  "local_id": "e2",
  "class": "FaultEvent",
  "cause_rule": {"event_local_id": "e1"},
  "evidence": [{"quote": "过滤器堵塞会造成出口压力不足"}]
}
```

验证器会移除无效实体或关系，验证证据引文在文本块中逐字出现，并检查关系端点的类。有效的关系代码会被前置到现有边的 `keywords` 字段中，因此当前 WebUI 图可以显示诸如 `HAS_FAULT, 设备故障` 这样的值。

`CAUSES` 关系可以包含 `gate_type_hint` 和 `gate_key`。验证器会将它们投影为一个确定性的 `LogicGate` 节点，外加 `INPUT_OF` 和 `OUTPUT_TO` 边。门（gate）的名称由目标故障和 gate key 派生，而不是由文本块 ID 派生，因此在不同文本块中发现的同一逻辑分支会在当前图中累积到同一个门上。

## 图投影

**python**

```
from lightrag.fault_tree import project_candidate_batch

graph = project_candidate_batch(candidate_batch)
```

结果包含属性图形状的节点和边，包括针对如下表达式的嵌套门：

**text**

```
(A OR B) AND (C OR D) -> E
```

该投影是纯数据变换。部署适配器仍负责将其写入 PostgreSQL 或图数据库。

## WebUI 投影

已持久化的工作区图以四个有边界的视图暴露：

* `department-files`（`OWNS_FILE`）
* `department-equipment`（`HAS_EQUIPMENT`）
* `equipment-faults`（`HAS_FAULT`，一跳）
* `fault-causes`（`CAUSES`、`HAS_CAUSE_RULE`、`INPUT_OF`、`OUTPUT_TO`，从选定的一个故障出发的惰性因果展开）

完整契约见 [`docs/design/GraphViewProjectionContract.md`](https://../docs/design/GraphViewProjectionContract.md)。领域类和关系保留在本体配置档中；颜色、布局和当前激活视图保留在 WebUI 中。

已验证的实体和关系属性会持久化为 `domain_properties_json` / `relation_properties_json`，外加 `class_code`、`canonical_name`、`relation_code` 和 `ontology_version`。这样图后端只需处理标量属性，同时投影 API 可以为节点和边详情面板暴露解码后的映射。
