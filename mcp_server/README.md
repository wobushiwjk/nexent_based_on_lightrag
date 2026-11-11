# MCP 只读领域服务

这个目录把工业故障树领域包暴露成 **6 个只读 MCP 工具**，让 Nexent Agent 通过标准
MCP 协议查询本体、设备、故障、证据和故障树，而不接触 LightRAG 的存储实现。

它对应工作流 W2，并在 W3 增加了 storage-backed 模式：可以直接读取导入器写出的
运行时 bundle + SQLite 证据库。两种模式都只读：W2 快照模式读取
`seeds/demo_bundle.json`，W3 存储模式通过
`storage/tenants/<tenant>/active.json` 找到当前 run，读取
`storage/tenants/<tenant>/runs/<run_id>/bundle.json`，并按需加载同目录
`evidence.sqlite3` 里的 quote。

## 分层

```
Nexent Agent / MCP Inspector / scripts/mcp_smoke_test.py
                      │  MCP (stdio / SSE / streamable-http)
                      ▼
              mcp_server/server.py        FastMCP 注册、只读注解、统一信封
                      │
              mcp_server/service.py       六个查询用例、排序、证据汇总
                      │
        ┌─────────────┴─────────────┐
        ▼                           ▼
repository.py                  storage_repository.py
静态 bundle 快照               运行时 bundle + SQLite 证据库
        │                           │
        └─────────────┬─────────────┘
                      ▼
        domain_packs/industrial_fault/   ontology.yml + bundle/storage
```

`repository.py` 在启动时调用 `validate_domain_bundle()`。bundle 有 error 时服务
直接以退出码 `2` 结束，不会带着坏数据提供查询；warning 会记录日志但允许启动。

## 快速开始

### 1. 安装 MCP 依赖

```bash
uv sync --extra mcp
```

### 2. 直接启动服务（stdio）

```bash
# Git Bash
source .venv/Scripts/activate
python -m mcp_server

# 或安装后的命令入口
lightrag-domain-mcp
```

默认加载 `domain_packs/industrial_fault` 和 `seeds/demo_bundle.json`。
用 `--help` 查看全部参数：

```bash
python -m mcp_server --help
```

W3 的 storage-backed 模式需要先导入种子文档：

```bash
.venv/Scripts/python.exe scripts/import_seed_domain.py --verify
python -m mcp_server --storage-dir domain_packs/industrial_fault/storage
```

也可以设置 `MCP_DOMAIN_STORAGE_DIR`，或直接用 `--storage-dir` 覆盖。
`--bundle` 与 `--storage-dir` 互斥；storage 模式会读取每个租户
`active.json` 指向的 run，每个租户对应一个 SQLite 库。仍兼容旧布局
`<storage>/tenants/<tenant>/bundle.json`；若 `active.json` 指向的 run 缺失，
服务会拒绝启动，不会静默回退到旧快照。

### 3. 启动 HTTP 传输（可选）

```bash
python -m mcp_server --transport streamable-http --host 127.0.0.1 --port 8000
# 或
python -m mcp_server --transport sse --host 127.0.0.1 --port 8000
```

stdio 是 Nexent 默认使用的传输方式；HTTP 便于调试和远程部署。

### 4. 不启动 Nexent，跑完整调用链

```bash
.venv/Scripts/python.exe scripts/mcp_smoke_test.py
# storage-backed 模式
.venv/Scripts/python.exe scripts/mcp_smoke_test.py \
  --storage-dir domain_packs/industrial_fault/storage
```

脚本会启动真实子进程、完成 MCP 握手、调用全部 6 个工具、验证跨租户查询被拒绝，
并在前后对领域包做 SHA-256 全目录比对，证明工具没有写文件。预期输出：

```
[ok] get_ontology: 9 classes, 11 relations, 8 constraints
[ok] get_equipment: EQ-P101A (P-101A)
[ok] search_faults: 2 matches, first=FAULT-LOW-PRESSURE
[ok] get_fault_tree: TREE-P101A-LOW-PRESSURE-v1 with 8 nodes, 8 edges, 2 gates
[ok] trace_evidence: 2 evidence fragments
[ok] validate_fault_tree: valid=True, errors=0, warnings=0
[ok] cross-tenant lookup rejected with not_found
[ok] domain pack unchanged (read-only)
All read-only MCP tool checks passed.
```

加 `--json` 会打印每个工具的完整响应信封；`--pack`、`--bundle`、`--storage-dir`、
`--tenant` 可以覆盖默认路径和租户。

### 5. 用 MCP Inspector 手工检查（可选）

需要 Node.js 和外网下载 Inspector 时才能用：

```bash
source .venv/Scripts/activate
npx @modelcontextprotocol/inspector python -m mcp_server
```

在 Inspector 中应看到 6 个工具，且每个工具都标记为 `readOnlyHint: true`。

## 工具清单

所有数据工具都要求 `tenant_id`。`get_ontology` 是所有租户共享的语义定义，不需要租户。

| 工具                    | 输入                                                                 | 返回的重点字段                                                                                                   |
| ----------------------- | -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `get_ontology`        | 无                                                                   | `ontology_version`、`schema_version`、`counts`、`classes`、`relations`、`constraints`、`standards` |
| `get_equipment`       | `tenant_id`、`equipment_ref`                                     | `match.matched_by`、`equipment`、`departments`、`faults`、`facts`、`source_files`、`evidence`      |
| `search_faults`       | `tenant_id`、`query`、`limit`（1–50，默认 10）                | `count`、`total_faults`、`results[].uid`、`score`、`matched_terms`                                     |
| `get_fault_tree`      | `tenant_id`、`draft_id` 或 `top_event_uid`（二选一，可都不传） | `draft`、`top_event`、`faults`、`rules`、`rule_expressions`、`counts`                                |
| `trace_evidence`      | `tenant_id`、`target_type`、`target_id`                        | `target`、`count`、`evidence[].quote`、`asset`、`targets`、`via`                                     |
| `validate_fault_tree` | `tenant_id`、`draft_id` 或 `draft`（二选一）                   | `valid`、`error_count`、`warning_count`、`issues`、`checked`                                           |

`get_equipment` 的 `equipment_ref` 接受 UID、设备位号、名称或别名。匹配顺序为
UID → 设备位号 → 名称/别名；多个记录命中时返回 `ambiguous_selector`。

`trace_evidence` 的 `target_type` 只接受：

| 值                | `target_id` 例子       |
| ----------------- | ------------------------ |
| `entity`        | `FAULT-FILTER-BLOCKED` |
| `fact`          | `FACT-1`               |
| `rule`          | `RULE-CAVITATION-AND`  |
| `logic_gate`    | `GATE-AND-CAVITATION`  |
| `decision_step` | `step-3`               |

`logic_gate` 的证据通过节点属性里的 `rule_uid` 解析到规则；`decision_step` 的证据
同时包含步骤自身引用的证据和所有目标指向该步骤的证据。

## 响应信封

每个工具都返回同一种 JSON 对象，MCP 的 `isError` 对业务错误保持 `false`：

```json
{
  "ok": true,
  "data": {},
  "error": null,
  "meta": {
    "ontology_version": "industrial-fault-v0.1",
    "schema_version": "industrial-fault-schema-v0.1",
    "tenant_id": "tenant-demo",
    "read_only": true
  }
}
```

失败时：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "not_found",
    "message": "equipment 'P-999' was not found in tenant 'tenant-demo'",
    "details": {"equipment_ref": "P-999"}
  },
  "meta": {
    "ontology_version": "industrial-fault-v0.1",
    "schema_version": "industrial-fault-schema-v0.1",
    "tenant_id": "tenant-demo",
    "read_only": true
  }
}
```

Agent 应读取 `error.code`，不要解析 `message` 文本。错误码约定：

| 错误码                 | 含义                                                                 | 典型修复                                                |
| ---------------------- | -------------------------------------------------------------------- | ------------------------------------------------------- |
| `invalid_argument`   | 参数为空、类型错误、互斥参数同时出现、limit 越界、内联草稿不符合契约 | 修正调用参数                                            |
| `not_found`          | 租户、设备、故障、规则、树或证据目标不存在                           | 先用 `search_faults` 或 `get_ontology` 找到正确 UID |
| `ambiguous_selector` | 设备引用命中多条记录                                                 | 改用 UID 或设备位号                                     |
| `unsupported`        | 当前版本明确不支持的能力                                             | 不要重试，按返回信息降级                                |
| `internal_error`     | 未预期异常；细节只写服务端日志，不返回给调用方                       | 记录请求并检查服务日志                                  |

## 示例请求与响应

以下响应均为**字段节选**，完整响应还包含 W1 契约中的其余字段。
完整样例见 `scripts/mcp_smoke_test.py --json`。

### `get_ontology`

请求：

```json
{"tool": "get_ontology", "arguments": {}}
```

响应 `data` 节选：

```json
{
  "ontology_version": "industrial-fault-v0.1",
  "schema_version": "industrial-fault-schema-v0.1",
  "counts": {"classes": 9, "relations": 11, "constraints": 8, "standards": 4},
  "classes": [
    {
      "code": "FaultEvent",
      "label": "故障事件",
      "required_fields": ["name", "description"],
      "standards": ["IEC 61025", "IEC 60812", "ISO 14224"],
      "schema_ref": "schemas/entity.schema.json#/$defs/FaultEvent"
    }
  ],
  "relations": [
    {
      "code": "CAUSES",
      "source_types": ["FaultEvent"],
      "target_types": ["FaultEvent"],
      "evidence_required": true
    }
  ],
  "constraints": [
    {"id": "evidence-verbatim", "applies_to": ["Evidence"]}
  ]
}
```

### `get_equipment`

请求：

```json
{"tool": "get_equipment", "arguments": {"tenant_id": "tenant-demo", "equipment_ref": "P-101A"}}
```

响应 `data` 节选：

```json
{
  "match": {"input": "P-101A", "matched_by": "equipment_tag"},
  "equipment": {
    "uid": "EQ-P101A",
    "canonical_name": "冷却水泵",
    "equipment_tag": "P-101A",
    "equipment_type": "PUMP.CENTRIFUGAL",
    "lifecycle_status": "in_service",
    "aliases": ["循环泵", "冷却泵"]
  },
  "departments": [],
  "faults": [],
  "facts": [],
  "source_files": [
    {"uid": "FILE-P101A-MANUAL", "file_name": "pump_manual_excerpt.md", "modality": "text"}
  ],
  "evidence": [
    {
      "evidence_id": "EV-MANUAL-1",
      "quote": "入口过滤器堵塞会降低泵的入口流量，并进一步造成出口压力不足。",
      "page": 12,
      "section_path": "4.2 出口压力不足"
    }
  ]
}
```

> 演示 bundle 没有 `HAS_FAULT` 事实，所以 `departments` 和 `faults` 为空。
> 这不影响设备查询本身；W3 投影图谱时会补上设备与故障的关联。

### `search_faults`

请求：

```json
{"tool": "search_faults", "arguments": {"tenant_id": "tenant-demo", "query": "出口压力不足", "limit": 5}}
```

响应 `data` 节选：

```json
{
  "query": "出口压力不足",
  "limit": 5,
  "count": 2,
  "total_faults": 4,
  "results": [
    {
      "uid": "FAULT-LOW-PRESSURE",
      "canonical_name": "出口压力不足",
      "aliases": ["出口压力低", "压力不够"],
      "severity": "high",
      "evidence_count": 4,
      "cause_rule_uids": ["RULE-LOW-PRESSURE-OR"],
      "score": 250,
      "matched_terms": ["出口", "出口压力不足", "压力", "不足"]
    }
  ]
}
```

匹配是**词法排序**：完整名称/别名命中权重最高，其次是 UID、名称片段和正文片段；
存在 `HAS_FAULT` 事实时，设备关联再加分。它不是向量检索，W3 可以在此之上接入
LightRAG 的混合检索。没有命中时返回 `count: 0`、`results: []` 和 `hint`，
这不是错误。

### `get_fault_tree`

请求：

```json
{"tool": "get_fault_tree", "arguments": {"tenant_id": "tenant-demo", "top_event_uid": "FAULT-LOW-PRESSURE"}}
```

响应 `data` 节选：

```json
{
  "draft": {
    "draft_id": "TREE-P101A-LOW-PRESSURE-v1",
    "top_event_uid": "FAULT-LOW-PRESSURE",
    "version": 1,
    "review_status": "approved",
    "validation": {"valid": true, "issues": []}
  },
  "top_event": {"uid": "FAULT-LOW-PRESSURE", "canonical_name": "出口压力不足"},
  "rule_expressions": [
    {
      "rule_uid": "RULE-CAVITATION-AND",
      "effect_fault_uid": "FAULT-CAVITATION",
      "expression": {
        "op": "AND",
        "event_uid": null,
        "children": [
          {"op": null, "event_uid": "FAULT-FILTER-BLOCKED", "children": []},
          {"op": null, "event_uid": "FAULT-INLET-LEAK", "children": []}
        ]
      }
    }
  ],
  "counts": {"nodes": 8, "edges": 8, "gates": 2, "faults": 4, "rules": 2}
}
```

`draft.nodes` / `draft.edges` 包含完整投影：`FaultEvent`、`FaultCauseRule`、
`LogicGate` 节点，以及 `HAS_CAUSE_RULE`、`INPUT_OF`、`OUTPUT_TO` 边。
同时传 `draft_id` 和 `top_event_uid` 会返回 `invalid_argument`；两个都不传时，
如果租户只有一棵树就返回它，没有树则返回 `not_found`。

### `trace_evidence`

请求：

```json
{"tool": "trace_evidence", "arguments": {"tenant_id": "tenant-demo", "target_type": "entity", "target_id": "FAULT-FILTER-BLOCKED"}}
```

响应 `data` 节选：

```json
{
  "target_type": "entity",
  "target_id": "FAULT-FILTER-BLOCKED",
  "count": 4,
  "evidence": [
    {
      "evidence_id": "EV-TABLE-1",
      "asset_id": "FILE-P101A-INSPECTION",
      "asset": {"file_name": "inspection_table.md", "modality": "table"},
      "quote": "入口过滤器堵塞",
      "quote_hash": "sha256:75fffeaaaf8881673568e8ad25f09ffaff7bc245ce0cc4efc77e1e219cde59e6",
      "page": 1,
      "section_path": "P-101A 2026-09-12 行",
      "char_start": 117,
      "char_end": 124,
      "targets": [
        {"target_type": "fact", "target_id": "FACT-1", "role": "supports"},
        {"target_type": "entity", "target_id": "FAULT-FILTER-BLOCKED", "role": "mentions"}
      ],
      "via": ["record", "target"]
    }
  ]
}
```

这条例子返回 4 条证据，来自巡检表、维修记录和手册三份文档；`via` 说明证据是
直接记录在实体上（`record`）还是通过 `targets` 指向该实体（`target`）。

### `validate_fault_tree`

请求：

```json
{"tool": "validate_fault_tree", "arguments": {"tenant_id": "tenant-demo", "draft_id": "TREE-P101A-LOW-PRESSURE-v1"}}
```

响应 `data`：

```json
{
  "draft_id": "TREE-P101A-LOW-PRESSURE-v1",
  "top_event_uid": "FAULT-LOW-PRESSURE",
  "valid": true,
  "error_count": 0,
  "warning_count": 0,
  "issues_truncated": false,
  "issues": [],
  "checked": {"rules": 2, "entities": 9}
}
```

传入内联 `draft` 时不会写库；校验器会检查：
单顶事件、无环、逻辑门至少两个输入且一个输出、节点可达性、规则/实体存在性、
租户与本体版本。缺少一条 AND 输入边的草稿会返回 `valid: false`，并在 `issues`
中给出 `gate-too-few-inputs` 和 `gate-input-count`。

## 租户隔离

每个 bundle 的 `tenant_id` 独立建索引，工具只在该租户的索引里查找：

- `tenant-demo` 查不到 `tenant-other` 的设备或故障；
- 未加载的租户返回 `not_found`，响应中**不列出**已加载的其他租户；
- 单租户部署时，空 `tenant_id` 会解析到唯一的 bundle；传入任意其他租户名
  仍然返回 `not_found`，不会“随便写一个租户也能查”；
- 多租户部署时用 `--bundle` 重复传入，或设置 `MCP_DOMAIN_BUNDLES`
  （用操作系统路径分隔符分隔，Windows 是 `;`，POSIX 是 `:`）。

**重要边界**：`tenant_id` 是调用方声明的，W2 不做身份认证。真正的租户身份必须由
Nexent 或前置网关在调用 MCP 前完成认证，并把可信租户 ID 注入工具参数（W5）。
在网关就绪前，不要把本服务直接暴露到不可信网络。

## 只读保证

- 6 个工具全部注册为 `readOnlyHint: true`、`destructiveHint: false`；
- 服务没有写方法，也没有开放任何写工具；
- storage 模式不会 upsert/delete 证据行；打开已有 SQLite 库时只做幂等的
  `CREATE TABLE IF NOT EXISTS` 检查；
- 测试在调用前后对比整个领域包的 SHA-256 目录摘要；
- 启动时只读加载 `ontology.yml` 和 bundle，不修改它们。

“只读”是当前 W2 的边界，不是整个项目的边界。W3 的图谱投影和 W8 的本体演化会有
独立的写服务/审核流程，不会复用这 6 个工具偷偷写库。

## 配置

| 环境变量                    | 默认值                                                   | 作用                                  |
| --------------------------- | -------------------------------------------------------- | ------------------------------------- |
| `MCP_DOMAIN_PACK`         | `domain_packs/industrial_fault`                        | 领域包目录（含 `ontology.yml`）     |
| `MCP_DOMAIN_BUNDLES`      | `domain_packs/industrial_fault/seeds/demo_bundle.json` | bundle 路径；多个用路径分隔符连接     |
| `MCP_DOMAIN_STORAGE_DIR`  | 空                                                       | 导入器输出目录；设置后优先于 bundle   |
| `MCP_DEFAULT_TENANT`      | 空                                                       | 工具收到空 `tenant_id` 时使用的租户 |
| `MCP_SERVER_NAME`         | `pump-copilot-domain`                                  | MCP 服务名                            |
| `MCP_LOG_LEVEL`           | `INFO`                                                 | 日志级别                              |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000`                                 | HTTP 传输的绑定地址                   |

相对路径按**仓库根目录**解析，不是按进程当前目录；从 Nexent 启动子进程时这一点
很重要。

## 测试

```bash
# Git Bash；Windows 上显式指定解释器，scripts/test.sh 才能找到 .venv
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/mcp -q

# 领域包回归（W1）
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/domain -q
```

当前结果：`tests/mcp` **54 passed**，`tests/domain` **39 passed**（W3 验收前）。

`tests/mcp` 覆盖：

- `test_repository.py`：bundle 校验失败、重复租户、未知租户、多租户索引；
- `test_storage_repository.py`：启动不读 quote、按需加载、缺库/缺证据行拒绝、
  `active.json` 指针优先、只读当前 run、旧版布局兼容；
- `test_service.py`：六个工具的查询/排序/证据汇总/校验/租户隔离/只读；
- `test_server.py`：FastMCP 注册、只读注解、输入 Schema、成功/业务错误/内部错误信封；
- `test_transport.py`：真实 stdio 子进程握手、列工具、调用全部工具、跨租户拒绝。

## 当前边界

1. **两种加载模式**：W2 的 `--bundle` 快照模式会把整个 bundle（包括每条 evidence
   的 quote）加载进进程，只适合小规模演示；W3 的 `--storage-dir` 模式启动时不读
   任何 quote，`trace_evidence` 命中后才从 SQLite 读取，10 万条证据启动堆增量约
   0.12 MiB。两条路径的工具响应契约相同。
2. **无认证**：`tenant_id` 由调用方声明；身份认证和授权在 Nexent/网关层完成（W5）。
3. **每租户一棵树**：当前每个返回的 bundle 只保存一个 `FaultTreeDraft`；多树/多版本
   查询不在 W3 范围内。
4. **词法搜索**：`search_faults` 是名称/别名/UID/正文的词法打分，不含向量或
   reranker；W3/W9 会补充混合检索和评测。
5. **演示数据没有 `HAS_FAULT`**：`get_equipment` 的 `faults` 为空是数据现状，
   不是查询缺陷；W3 投影时会补关联。
6. **只有 AND/OR**：动态门（PAND、SEQ）和定量 FTA 不在当前契约内。
