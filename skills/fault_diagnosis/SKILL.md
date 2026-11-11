---
name: fault_diagnosis
description: 根据设备位号/名称和现场症状，检索候选故障、逐条追溯逐字证据，并给出不超出证据范围的诊断结论。
version: 0.1.0
---

# 故障诊断 Skill

把“这台设备现在这个症状，可能是哪些故障”变成一条可复核的流程：先定位设备，再找候选故障，
然后为每个候选追溯原文证据，最后只根据工具返回的内容写结论。**任何工具之外的推断都不允许。**

## 什么时候使用

- 用户给出设备位号（如 `P-101A`）、设备名称或别名，并描述了一个症状。
- 用户要求“诊断”“可能原因”“故障排查”，且需要给出证据出处。
- 用户要求查看某台设备的历史故障、维修措施或故障树。

不要用于：纯知识问答（改用 `search_faults`）、标准条款查询（改用 `get_ontology`）、
证据真伪核验（改用 `evidence_verification`）、修改本体（改用 `ontology_change_draft`）。

## 依赖的 MCP 工具

只允许调用这 6 个只读工具：`get_ontology`、`get_equipment`、`search_faults`、
`trace_evidence`、`get_fault_tree`、`validate_fault_tree`。不得直接读文件、数据库或 LightRAG 存储。

## 执行步骤

1. 调用 `get_ontology`，记录 `ontology_version`。后续所有结论都属于这个版本。
2. 调用 `get_equipment(tenant_id, equipment_ref)`。
   - `not_found`：返回 `status="not_found"`，请用户确认位号/名称；不要猜设备。
   - `ambiguous_selector`：返回 `status="invalid_input"`，把工具给出的候选列表交给用户选择。
3. 确定检索词：优先用用户给出的 `symptom`；没有症状时用设备的位号或名称。
   设备记录里已有的关联故障（`faults`）排在前面，`search_faults` 的结果按工具顺序追加，
   按 `fault_uid` 去重，最多保留 `max_candidates` 个。
4. 对每个候选故障：
   - `trace_evidence(target_type="entity", target_id=fault_uid)`；
   - 对其 `cause_rule_uids` 中最多前 2 条规则各做一次 `trace_evidence(target_type="rule", ...)`；
   - 按 `evidence_id` 合并证据。
   - 证据必须同时满足：quote 非空、`quote_hash == sha256(quote)`、字符区间长度等于 quote 长度、
     来源文件存在。任一条不满足就标记 `evidence_complete=false`，并在 `warnings` 中写明。
   - 一个候选没有任何证据时，**不能为它编原因**；整体返回证据不足。
5. 故障树（`include_tree=true` 时）：用第一个有证据的候选作为 `top_event_uid` 调 `get_fault_tree`；
   若该顶事件没有树，再尝试不带选择器读取该租户唯一的树；随后用 `validate_fault_tree` 校验。
   树的校验失败只作为 `warnings`，不改变候选排序。
6. 写结论：只复述设备、候选名称、证据条数、树节点/边数和校验状态，并明确说明“只引用 MCP 工具返回的内容”。

## 输入契约

`schema`：[input.schema.json](input.schema.json)　示例：[examples/request.json](examples/request.json)

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `tenant_id` | 是 | 租户 ID，工具据此隔离数据 |
| `equipment_ref` | 是 | 设备 UID、位号或名称/别名 |
| `symptom` | 否 | 现场症状；缺省时用设备位号/名称检索 |
| `max_candidates` | 否 | 1–10，默认 5 |
| `include_tree` | 否 | 是否附带故障树，默认 `true` |

示例：

```json
{
  "tenant_id": "tenant-demo",
  "equipment_ref": "P-101A",
  "symptom": "出口压力不足",
  "max_candidates": 3,
  "include_tree": true
}
```

## 输出契约

`schema`：[output.schema.json](output.schema.json)

统一信封：`{ok, status, data, error, warnings, tool_calls, meta}`。

| `status` | `ok` | 含义 |
| --- | --- | --- |
| `diagnosed` | `true` | 至少一个候选有完整逐字证据 |
| `needs_more_evidence` | `false` | 有候选但都无证据，或没有候选；`data` 仍返回已知部分 |
| `not_found` | `false` | 设备不存在 |
| `invalid_input` | `false` | 输入不合法或设备选择器有歧义 |
| `failed` | `false` | MCP 工具或上游失败 |

`data` 字段：`equipment`、`query`、`candidates[]`（含 `fault_uid`、`canonical_name`、
`evidence_count`、`evidence[]`、`evidence_complete`、`cause_rule_uids`、`linked_by_equipment`）、
`fault_tree`、`conclusion`、`evidence_ids`。`tool_calls` 记录每一次工具调用的参数和结果。

## 失败处理

- 输入缺字段或类型错误：返回 `status="invalid_input"`、`error.code="invalid_argument"`，
  不发起任何工具调用。
- 设备不存在：`error.code="not_found"`，不尝试相似位号。
- 设备名匹配多条：`error.code="ambiguous_selector"`，保留 `details.candidates`。
- 搜索结果为 0：`error.code="insufficient_evidence"`，`data.candidates=[]`，结论写明“未生成诊断结论”。
- 证据查询失败：该候选标为证据不完整；若全部候选都无证据，整体为 `needs_more_evidence`。
- 故障树缺失或校验失败：只记 `warnings`，诊断仍可返回 `diagnosed`。

## 边界

- 不做概率排序、不输出“最可能”的置信度；候选顺序只反映工具返回顺序和设备关联。
- 不生成维修步骤；维修措施只能原样引用故障记录中的 `maintenance_measures`（由调用方另行读取）。
- 不跨租户查询；`tenant_id` 由调用方声明。
- `max_candidates` 上限 10，避免把整棵故障库塞进上下文。

## 离线复现

```bash
.venv/Scripts/python.exe scripts/run_skill.py \
  --skill fault_diagnosis \
  --input skills/fault_diagnosis/examples/request.json
```

退出码：`0` 表示 `ok=true`，`1` 表示结构化业务失败（`envelope` 仍会打印），`2` 表示输入或传输失败。
