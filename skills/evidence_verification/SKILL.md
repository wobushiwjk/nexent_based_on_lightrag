---
name: evidence_verification
description: 对事实、规则、实体或故障树的证据做逐字核验，重算 quote 哈希、检查字符区间与来源文件，并给出可解释的通过/拒绝结果。
version: 0.1.0
---

# 证据核验 Skill

回答的是“这条知识到底有没有原文支撑”，而不是“这条知识对不对”。对每个声明指针，Skill 通过
`trace_evidence` 取回证据，重算 `sha256(quote)`，检查字符区间、来源文件和目标关联，再给出结论。
**Skill 不阅读原始文件，也不生成新的声明文本。**

## 什么时候使用

- 诊断结论、故障树或本体草案进入人工审核前，需要逐条确认证据。
- 用户质疑“这个结论有原文吗”“quote 是不是被改过”。
- 批量核验一个租户内的事实/规则/实体。
- 发布故障树前，需要同时核验声明证据和树结构。

不要用于：找候选故障（`fault_diagnosis`）、浏览本体（`get_ontology`）、修改证据（本 Skill 只读）。

## 依赖的 MCP 工具

`trace_evidence`、`validate_fault_tree`。两者都是只读工具；不得直接访问 SQLite、文件或 LightRAG。

## 执行步骤

1. 对 `claims[]` 中的每一项调用
   `trace_evidence(tenant_id, target_type, target_id)`。
2. 目标不存在（`not_found`）时，把该条声明记为 `exists=false`、`supported=false`、
   `issues=["target_not_found"]`，继续核验其余声明；不要中断整个批次。
3. 其他工具错误（如 `invalid_argument`、`upstream_error`）说明核验环境有问题，整体返回失败。
4. 对每条证据检查：
   - `quote_present`：quote 非空；
   - `hash_matches`：`quote_hash == "sha256:" + sha256(quote)`；
   - `span_matches`：`char_end - char_start == len(quote)`；
   - `asset_present`：`asset_id` 和来源文件名都存在；
   - `target_linked`：证据的 `targets` 明确指向该声明，或该证据是记录直接持有的（`via` 含 `record`）。
5. `review_status != "approved"` 只记 `warnings`，不单独判失败；审核状态不是逐字证据的一部分。
6. 若提供 `draft_id`，再调用 `validate_fault_tree`，把树校验结果放在 `tree_validation`，
   并与声明结果一起决定最终 `status`。

## 输入契约

`schema`：[input.schema.json](input.schema.json)　示例：[examples/request.json](examples/request.json)

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `tenant_id` | 是 | 租户 ID |
| `claims[]` | 二选一 | 每项含 `claim_id`、`target_type`、`target_id` |
| `draft_id` | 二选一 | 要校验的故障树 ID |

`target_type` 只能是 `entity`、`fact`、`rule`、`logic_gate`、`decision_step`。至少提供 `claims`
或 `draft_id` 之一。

示例：

```json
{
  "tenant_id": "tenant-demo",
  "claims": [
    {"claim_id": "claim-fact-1", "target_type": "fact", "target_id": "FACT-1"},
    {"claim_id": "claim-rule-and", "target_type": "rule", "target_id": "RULE-CAVITATION-AND"}
  ],
  "draft_id": "TREE-P101A-LOW-PRESSURE-v1"
}
```

## 输出契约

`schema`：[output.schema.json](output.schema.json)

`data` 包含：`claims[]`（`claim_id`、`target_type`、`target_id`、`exists`、`evidence_count`、
`supported`、`checks`、`issues`、`warnings`、`evidence[]`）、`verified_count`、`rejected_count`、
`tree_validation`、`verdict`。

| `status` | `ok` | 含义 |
| --- | --- | --- |
| `verified` | `true` | 所有声明通过且（若有）树校验通过 |
| `partially_verified` | `true` | 部分声明通过 |
| `rejected` | `true` | 没有声明通过，或树校验未通过；核验本身完成 |
| `not_found` | `false` | 指定的故障树不存在 |
| `invalid_input` | `false` | 输入不满足契约 |
| `failed` | `false` | 工具错误导致无法完成核验 |

注意：`status="rejected"` 时 `ok` 仍为 `true`，因为“核验完成且结论为不通过”是一个有效结果；
`ok=false` 只表示 Skill 没能完成核验。

## 失败处理

- 没有 `claims` 也没有 `draft_id`：`invalid_argument`，不发起工具调用。
- 单条声明目标不存在：该条 `rejected`，其余继续；不会伪造 `exists=true`。
- 单条证据 quote 被改动：`all_hashes_match=false`，`issues` 精确到 `<evidence_id>:hash_matches`。
- 工具返回 `upstream_error`：整体 `failed`，已完成的声明放在 `data` 中，便于定位。
- 树不存在：整体 `not_found`；不要把“树不存在”当成“树校验通过”。

## 边界

- 不判断语义真假，只判断“原文是否逐字支持该记录”。
- 不访问原始 PDF/文档；字符区间和哈希以证据记录为准，原始文件核对属于 W6 解析管线。
- 不修改证据或审核状态。
- 不把 `review_status` 当作支持性条件，只作为提示。

## 离线复现

```bash
.venv/Scripts/python.exe scripts/run_skill.py \
  --skill evidence_verification \
  --input skills/evidence_verification/examples/request.json
```
