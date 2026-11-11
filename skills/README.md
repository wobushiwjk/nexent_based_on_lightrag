# Skills 工作流

`skills/` 把“故障诊断、证据核验、本体变更草案”封装成 Agent 可以复用的能力，而不是写死在
Nexent 的提示词里。每个 Skill 有两份等价产物：

1. **`SKILL.md`**：给 Nexent Agent 看的执行规程。它只允许调用 W2 的 6 个只读 MCP 工具，
   规定步骤、输入/输出契约、失败处理和不许越界的部分。
2. **`handler.py`**：同一规程的确定性参考实现。它通过 `ToolClient` 协议访问 MCP，
   不导入 `lightrag` 或 `mcp_server`，因此“Skill 只能通过 MCP 访问数据”由代码结构保证，而不只是约定。离线测试和 `scripts/run_skill.py` 直接运行它，用来证明规程可执行。

两者必须同步修改；`tests/skills/` 同时覆盖文档所描述的步骤和 handler 的实际行为。

## 目录

```text
skills/
├── README.md                     # 本文件
├── runtime.py                    # ToolClient 协议、统一信封、证据校验助手
├── manifest.json                 # 由脚本生成，勿手改
├── fault_diagnosis/
│   ├── SKILL.md
│   ├── handler.py
│   ├── input.schema.json         # 由脚本生成，勿手改
│   ├── output.schema.json
│   └── examples/request.json
├── evidence_verification/
└── ontology_change_draft/
```

## 统一响应信封

每个 Skill 都返回与 MCP 工具同形的信封：

```json
{
  "ok": true,
  "status": "diagnosed",
  "data": {},
  "error": null,
  "warnings": [],
  "tool_calls": [],
  "meta": {
    "skill": "fault_diagnosis",
    "skill_version": "0.1.0",
    "ontology_version": "industrial-fault-v0.1",
    "tenant_id": "tenant-demo",
    "read_only": true
  }
}
```

- `ok` 表示**主操作是否完成**，不是“结论是否为阳性”。
- `status` 表示语义结论，例如 `rejected`（核验完成但不通过）仍然是 `ok=true`。
- `error` 只在 `ok=false` 时出现，包含 `code`、`message`、`details`。
- `warnings` 收集可继续执行但不完整的部分（树缺失、来源证据缺失、证据校验失败等）。
- `tool_calls` 按顺序记录每一次 MCP 调用的参数与结果，供审计和复现。

### 状态值

| `status`                                           | 出现于   | 含义                                         |
| ---------------------------------------------------- | -------- | -------------------------------------------- |
| `diagnosed`                                        | 诊断     | 至少一个候选有完整逐字证据                   |
| `needs_more_evidence`                              | 诊断     | 有候选但无完整证据，或没有候选               |
| `verified` / `partially_verified` / `rejected` | 证据核验 | 全部通过 / 部分通过 / 全未通过（或树不合格） |
| `draft` / `rejected`                             | 本体草案 | 可提交审核 / 有阻断冲突                      |
| `not_found`                                        | 全部     | 设备、树或目标不存在                         |
| `invalid_input`                                    | 全部     | 输入不满足 JSON Schema 或选择器有歧义        |
| `failed`                                           | 全部     | 上游工具或环境失败                           |

### 错误码

| `code`                  | 含义                                 |
| ------------------------- | ------------------------------------ |
| `invalid_argument`      | 请求字段缺失、类型错误、枚举越界     |
| `not_found`             | 设备/故障树/取证目标不存在           |
| `ambiguous_selector`    | 设备名匹配多条，需要用户消歧         |
| `insufficient_evidence` | 有候选但没有可用逐字证据，不生成结论 |
| `unsupported`           | 尝试调用只读允许列表之外的工具       |
| `upstream_error`        | MCP 工具返回了非信封或不可用结果     |
| `internal_error`        | 未预期的内部失败                     |

## 在 Nexent 中使用

1. 先在 Nexent 注册 `mcp_server`（stdio 或 SSE），确认 6 个工具可见且 `readOnlyHint=true`。
2. 把每个 `skills/<name>/SKILL.md` 注册为一个 Skill，`description` 使用 frontmatter 中的说明，
   让 Agent 在合适场景自动选择。
3. Skill 的正文就是 Agent 的执行规程；它只能调用已注册的 MCP 工具，不得直接读文件或数据库。
4. 需要完全离线、可重复的验收时，运行 `scripts/run_skill.py`：它启动真实的 stdio MCP 会话，
   用同一份请求 JSON 执行参考实现，输出同一个信封。它不会绕过 MCP 直接调用服务。
5. `skills/manifest.json` 给出每个 Skill 的入口、工具列表和 schema 哈希，便于离线打包和漂移检查。

Agent 的自然语言推理发生在 Nexent 侧；`handler.py` 不调用任何大模型，只做确定性编排、校验和取证。
这样离线评测不受模型波动影响，模型输出的解释也不会被误当成证据。

## 生成 schema 与 manifest

Pydantic 模型是输入/输出契约的唯一事实来源。修改模型后必须重新生成：

```bash
.venv/Scripts/python.exe scripts/export_skill_schemas.py
```

该命令写出 `skills/<name>/{input,output}.schema.json` 与 `skills/manifest.json`。
`tests/skills/test_skill_contracts.py` 会逐字节比对，过期即失败。

## 运行测试

`tests/skills/` 与 `skills/` 镜像对应，包含：

- 三个 Skill 的行为测试（正常、缺输入、无证据、冲突、上游失败）；
- 共享契约测试（信封、状态、工具白名单、schema 同步）；
- `test_cli.py` 用真实 stdio 子进程验证 `scripts/run_skill.py`。

```bash
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/skills -q
```

## 新增一个 Skill

1. 建 `skills/<name>/{__init__.py,handler.py,SKILL.md,examples/request.json}`。
2. `handler.py` 暴露 `SKILL_NAME`、`SKILL_VERSION`、`SKILL_TOOLS`、`REQUEST_MODEL`、
   `RESPONSE_MODEL` 和 `async def run(request, tools)`；只通过 `SkillContext.call()` 访问工具。
3. 在 `scripts/export_skill_schemas.py` 的 `SKILL_MODULES` 和 `scripts/run_skill.py` 的
   `SKILL_RUNNERS` 中登记。
4. 在 `tests/skills/` 增加行为测试；示例请求必须能通过 `REQUEST_MODEL` 校验。
5. 重新生成 schema 与 manifest，并运行 `tests/skills`。

## 边界

- 所有 Skill 都是**只读**的；本体发布、证据写入和实例迁移属于 W8。
- `tenant_id` 由调用方声明，Skill 不做认证；认证/授权在 Nexent 或前置网关（W5）。
- 不跨租户查询，不枚举其他租户的数据。
- `fault_diagnosis` 不做概率排序；`evidence_verification` 不判断语义真假；
  `ontology_change_draft` 不发布变更。
- 参考实现是同步编排逻辑，不包含向量检索、reranker 或大模型调用；这些能力由 LightRAG 和
  Nexent 提供，Skill 只消费它们的 MCP 结果。
