---
name: ontology_change_draft
description: 把“新增类/关系/约束”或“废弃类”的请求变成可审核的本体变更草案，给出 diff、冲突、影响分析和审核清单，但不发布变更。
version: 0.1.0
---

# 本体变更草案 Skill

用户上传新标准、发现现有本体无法表达某个概念时，先把变更写成**草案**：检查它与当前本体是否冲突，
列出受影响的类、关系、Schema 和实例迁移需求，再交给人工审核。**本 Skill 永不写库、永不改版本号。**

## 什么时候使用

- 新标准/手册引入了现有本体没有的概念（如 `Sensor`）。
- 需要新增一条关系（如 `MONITORS`）或一条约束。
- 需要废弃一个类，并评估替换与迁移影响。
- 用户问“加这个类会不会破坏现有数据”。

不要用于：直接发布 schema（W8 审核管线）、写入实例（导入器）、查询证据（`evidence_verification`）。

## 依赖的 MCP 工具

`get_ontology`、`trace_evidence`。不得直接修改 `ontology.yml`、schema 文件或存储。

## 支持的变更类型

| `change_type` | 必填字段 | 说明 |
| --- | --- | --- |
| `add_class` | `code`、`label` | 可选 `parent`、`required_fields`、`prompt_visible`、`standards` |
| `add_relation` | `code`、`source_types`、`target_types` | 可选 `directed`、`evidence_required` |
| `add_constraint` | `id`、`applies_to`、`rule` | 可选 `severity`（`error`/`warning`） |
| `deprecate_class` | `code` | 可选 `replacement` |

## 执行步骤

1. 调用 `get_ontology`，读取当前 `ontology_version`、类、关系、约束。
2. 按变更类型做冲突检查：
   - `add_class`：类已存在、父类不存在 → 阻断；未声明 `required_fields` → 警告。
   - `add_relation`：关系已存在、端点类不存在 → 阻断；`evidence_required=false` → 警告。
   - `add_constraint`：约束 ID 已存在、`applies_to` 含未知类 → 阻断。
   - `deprecate_class`：类不存在、替换类不存在或等于自身、仍有子类 → 阻断；
     仍被关系引用 → 警告。
3. 生成 diff：`added` / `removed` / `modified`，每项含 `kind`、`code`、`definition`。
4. 生成影响分析：受影响的类、关系、schema 路径、是否需要实例迁移，以及说明。
5. 计算建议版本号：只递增次版本（`industrial-fault-v0.1 → industrial-fault-v0.2`）；
   版本号不符合 `vMAJOR.MINOR` 时追加 `+proposal` 并给出警告。
6. 若提供 `source_targets`，逐个调用 `trace_evidence` 附上动机证据；某个来源查不到只记警告，
   草案仍然生成，但审核清单会提示先补证据。
7. 返回 `review.required=true`、`review.applied=false`、`review.next_workflow="W8"` 和审核清单。

## 输入契约

`schema`：[input.schema.json](input.schema.json)　示例：[examples/request.json](examples/request.json)

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `tenant_id` | 是 | 租户 ID |
| `change` | 是 | 判别联合，按 `change_type` 区分四种变更 |
| `rationale` | 是 | 变更动机，不能为空 |
| `source_targets[]` | 否 | 支持该变更的领域记录，`target_type` + `target_id` |

示例：

```json
{
  "tenant_id": "tenant-demo",
  "rationale": "巡检记录需要独立表达振动传感器，现有类只能复用 Equipment。",
  "change": {
    "change_type": "add_class",
    "code": "Sensor",
    "label": "传感器",
    "parent": "Asset",
    "required_fields": ["uid", "sensor_type"],
    "standards": ["ISO 13374"]
  },
  "source_targets": [
    {"target_type": "fact", "target_id": "FACT-1"}
  ]
}
```

## 输出契约

`schema`：[output.schema.json](output.schema.json)

`data` 包含：`current_version`、`proposed_version`、`change`、`diff`、`conflicts[]`
（`code`、`message`、`blocking`）、`impact`、`source_evidence[]`、`review`。

| `status` | `ok` | 含义 |
| --- | --- | --- |
| `draft` | `true` | 草案已生成，可以进入人工审核 |
| `rejected` | `true` | 草案已评估，但存在阻断冲突，不能提交审核 |
| `invalid_input` | `false` | 输入不满足契约 |
| `failed` | `false` | `get_ontology` 等工具失败，无法评估 |

`status="rejected"` 时 `ok` 仍为 `true`：冲突检测本身成功完成，并给出了可操作的原因。
`review.applied` 永远是 `false`，本 Skill 不提供发布能力。

## 失败处理

- 缺 `rationale`、未知 `change_type`、字段类型错误：`invalid_argument`，不调用工具。
- `get_ontology` 失败：返回 `failed` / `not_found`，不凭记忆生成 diff。
- 父类或关系端点不存在：阻断，`diff` 仍展示用户提议的内容，便于修正。
- 来源证据查不到：警告 + 空 `source_evidence`，草案仍生成。
- 版本号无法解析：`proposed_version` 追加 `+proposal`，并写入 `warnings`。

## 边界

- 只支持新增和废弃，不支持修改已有类的字段级 diff；字段级演进留给 W8。
- 不重新生成 JSON Schema 文件，只给出受影响的 schema 路径。
- 不做实例迁移，只标记 `instance_migration_required`。
- 不校验拟议约束的规则语义（`rule` 是可读文本），只检查目标类是否存在。
- 不自动发布；发布必须经过 W8 的人工审核。

## 离线复现

```bash
.venv/Scripts/python.exe scripts/run_skill.py \
  --skill ontology_change_draft \
  --input skills/ontology_change_draft/examples/request.json
```
