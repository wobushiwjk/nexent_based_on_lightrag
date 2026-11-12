# 工业设备故障诊断领域包

版本：`industrial-fault-v0.1` / `industrial-fault-schema-v0.1`

> 所有种子文档和 demo 数据均为**合成演示语料**，不是真实客户资料；标准映射来自
> 公开目录元数据和结构化设计，不是对标准正文的复述或合规声明。详见
> [`seeds/README.md`](seeds/README.md) 和 [`standards/crosswalk.yml`](standards/crosswalk.yml)。

本包把“部门、文件、设备、故障事件、因果规则、逻辑门、证据”这一套对象固化成
可校验的数据契约，用于 LightRAG 图谱、MCP 工具、Nexent Skill 和审核界面之间的
交换。它是工业场景的参考实现，不是只服务于单台泵的演示数据。

## 文件

| 路径 | 作用 |
| --- | --- |
| `ontology.yml` | 类、关系、约束、标准映射的语义源 |
| `standards/crosswalk.yml` | IEC 61025 / ISO 14224 / IEC 60812 / ISO 13374 对照 |
| `schemas/*.schema.json` | 由 Pydantic 契约导出的 JSON Schema |
| `schemas/manifest.json` | Schema 版本与 sha256 |
| `seeds/documents/pump_manual_excerpt.md` | 手册语料，覆盖因果链与 AND 条件 |
| `seeds/documents/maintenance_record.md` | 维修记录语料，覆盖跨文档佐证 |
| `seeds/documents/inspection_table.md` | 表格语料，覆盖表格模态证据 |
| `seeds/multimodal/` | W6 多模态样例：合成 PDF/CSV/XLSX/PNG、manifest 与抽取 fixture（见该目录 README） |
| `seeds/demo_bundle.json` | 9 个实体、6 条证据、2 条事实、2 条规则、1 棵 gold 树、1 条决策轨迹 |
| `seeds/bad_bundle_sample.json` | 故意写坏的反例：非逐字 quote + 引用缺失证据，用于验证 CLI 会失败 |

`seeds/demo_bundle.json` 的故障树：

```
出口压力不足 (FAULT-LOW-PRESSURE)
└── OR 门
    ├── 入口过滤器堵塞 (FAULT-FILTER-BLOCKED)
    └── 叶轮汽蚀 (FAULT-CAVITATION)
        └── AND 门
            ├── 入口过滤器堵塞
            └── 吸入管法兰漏气 (FAULT-INLET-LEAK)
```

树里的 `LogicGate` 是由 `FaultCauseRule.expression` 投影出来的结构节点，
`HAS_CAUSE_RULE`、`INPUT_OF`、`OUTPUT_TO` 三类边共同表达规则归属和 AND/OR 语义。
`FAULT-FILTER-BLOCKED` 同时出现在 OR 门和 AND 门的输入侧，用来验证同一故障在
不同规则中的复用。

## 证据不变量

- `quote` 必须是 `asset_id` 指向文件的逐字连续片段；校验器直接读源文件比对。
- `quote_hash = sha256(quote)`，写成 `sha256:<64 hex>`。
- 提供 `char_start` / `char_end` 时，`text[start:end]` 必须等于 `quote`。
- 每条 evidence 至少有一个 `targets`，且目标必须存在（实体、事实、规则、逻辑门、
  决策步骤之一）。
- 事实和规则至少引用一条 evidence；没有原文证据的因果关系必须省略。
- 文件资产的 `checksum` 和 `file_size` 必须与磁盘文件一致。

这些不变量让“可追溯”成为可执行的检查，而不是文档里的口号。

## 标准对照

`standards/crosswalk.yml` 把本地概念映射到：

| 标准 | 作用 |
| --- | --- |
| IEC 61025 | 故障树结构、逻辑门、顶事件/中间事件 |
| ISO 14224 | 设备分类、可靠性维修数据 |
| IEC 60812 | 故障模式与影响 |
| ISO 13374 | 状态监测与诊断数据 |

这是工程映射，不是认证声明。`crosswalk.yml` 的 `gaps` 明确写出尚未实现的部分：
ISO 14224 完整代码表、IEC 61025 定量概率计算。

## 校验命令

```bash
python scripts/validate_domain.py
python scripts/validate_domain.py --json
./scripts/test.sh tests/domain

# W6 多模态样例：先生成合成二进制，再离线导入并校验
python scripts/make_multimodal_samples.py
python scripts/import_domain_multimodal.py --verify
```

预期输出开头：

```
Domain pack: .../domain_packs/industrial_fault
Ontology: industrial-fault-v0.1 (9 classes, 11 relations)
Bundle: demo-v0.1 (9 entities, 6 evidence, 2 facts, 2 rules)
Issues: 0 errors, 0 warnings
```

反例必须让命令以退出码 `1` 结束：

```bash
python scripts/validate_domain.py \
    --bundle domain_packs/industrial_fault/seeds/bad_bundle_sample.json
# Issues: 2 errors, 0 warnings
#   ERROR evidence-quote-not-verbatim [EV-BAD-1]: ...
#   ERROR evidence-reference-missing [FACT-BAD-1]: ...
```

## 当前边界

- 只覆盖“故障因果 + 处置建议”知识，不覆盖备件库存、工单成本等业务对象。
- 逻辑门表达动态/时序门（PAND、SEQ）尚不支持；当前只支持 AND/OR。
- 定量 FTA（概率、割集、重要度）不在本包范围内，见 crosswalk 的 `gaps`。
- 医疗设备领域只预留目录，尚未宣称同构迁移完成。
