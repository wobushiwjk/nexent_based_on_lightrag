# 回滚说明

本文件说明如何把本次导入写进 Nexent 的内容逐项删掉，以及每项删除的边界。**仓库里的源码、配置、Skill 和提示词不受影响**——Nexent 是消费者，删掉后重新导入即可恢复。

## 先读导入报告

所有要删的对象都能在 `nexent/build/import-report.json` 里找到：

| 对象 | 报告字段 |
| --- | --- |
| 租户 | `steps.identity.tenant_id` |
| 领域 MCP 服务 | `steps.mcp.name`、`steps.mcp.mcp_id` |
| 治理 MCP 服务 | `steps.governance_mcp.name`、`steps.governance_mcp.mcp_id` |
| Skill | `steps.skills.skills`（三个名字） |
| 知识库 | `steps.knowledge_base.display_name`、`steps.knowledge_base.index_name` |
| Agent | `steps.agent.name`、`steps.agent.agent_id` |

如果报告被删了，用同名查询：`GET /mcp/list`、`GET /skill`、`GET /indices`、`GET /agent/by-name/industrial_fault_agent`。

## 删除顺序

必须按 **Agent → Skill → 知识库 → 两个 MCP → 种子与治理存储** 的顺序。先删被引用的对象会让中间状态更难收拾（例如 Agent 还绑着已删除的 Skill）；反过来删则每一步都自洽。

### 1. 删除 Agent

Web UI：智能体管理 → 删除 `industrial_fault_agent`、`evidence_review_agent` 和 `ontology_expert_agent`。先删除子 Agent 的绑定关系，或直接删除主 Agent 后再删除两个子 Agent。

接口（需要登录后的 JWT）：

```bash
curl -X DELETE http://localhost:5010/agent \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": <report 里的 agent_id>}'
```

边界：删除会连同该 Agent 的全部版本和草稿一起删掉，无法在 Nexent 内恢复；重新导入会从仓库的 `nexent/agent/system_prompt.md` 和 `nexent/config/agent.json` 重建。

### 2. 删除 Skill

Web UI：技能管理 → 删除 `fault-diagnosis`、`evidence-verification`、`ontology-change-draft`。

接口：

```bash
for name in fault-diagnosis evidence-verification ontology-change-draft; do
  curl -X DELETE "http://localhost:5010/skill/$name" \
    -H "Authorization: Bearer <JWT>"
done
```

边界：Nexent 的删除是软删除（`delete_flag`），名称可以立即复用；Skill 内容是导入时从 ZIP 物化的快照，删除后只能从仓库重新打包上传。若只想刷新内容，不要手工删——用 `--refresh-skills` 重跑导入。

### 3. 删除知识库

Web UI：知识库 → `industrial-fault-kb` → 删除。

接口（路径参数用报告里的**内部索引名** `index_name`，不是显示名）：

```bash
curl -X DELETE "http://localhost:5010/indices/<index_name>" \
  -H "Authorization: Bearer <JWT>"
```

边界：会删除索引与文件记录；MinIO 中的原始对象是否一并清理取决于 Nexent 版本，重新导入时会按文件名比对，已存在同名文件则跳过上传。要彻底重来，先在 UI 里确认文件列表为空。

### 4. 删除 MCP 注册

Web UI：MCP 服务 → `industrial-fault-domain` 和 `ontology-governance` → 删除。

接口：

```bash
curl -X DELETE "http://localhost:5010/mcp/<domain_mcp_id>" \
  -H "Authorization: Bearer <JWT>"
curl -X DELETE "http://localhost:5010/mcp/<governance_mcp_id>" \
  -H "Authorization: Bearer <JWT>"
```

边界：只删除 Nexent 里的注册记录和工具目录，**不会**停止本仓库的两个 MCP Server 进程。要停服务，回到运行它们的终端按 `Ctrl+C`。

### 5. 删除种子存储（可选）

只有在要连本地数据一起清掉时才做。先停两个 MCP Server，再删对应租户目录：

```bash
rm -rf "domain_packs/industrial_fault/storage/tenants/<tenant_id>"
```

边界：`storage/` 是生成物，已列入 `.gitignore`，可随时用 `scripts/import_seed_domain.py` 重建；目录下可能还有别的租户，**不要**直接删整个 `storage/`。删除后 MCP Server 会因为找不到 bundle 而拒绝启动，直到重新导入种子。

治理库位于 `storage/governance/`。它保存不可变本体版本、变更单和审核事件，
默认**不要**随演示清理删除；只有确认无需保留治理审计时才单独删除该目录。

### 6. 清理生成物（可选）

```bash
rm -rf nexent/build
```

里面是 Skill ZIP、导入报告和演示 transcript。建议保留报告与 transcript 作为验收证据，只删 ZIP。

## 回滚后恢复

按 [README.md](README.md) 的「从零导入」重跑即可。第二遍导入会重新创建 Skill、知识库、Agent 和两个 MCP 注册；种子步骤在 `active.json` 仍存在时自动跳过，治理库已初始化时也会跳过 bootstrap。

## 不要做的事

- **不要**在 MCP Server 运行时删除或重写 `storage/`，Windows 上会因文件句柄占用失败。
- **不要**为了“干净”直接删 Postgres 数据卷；那会丢掉整个 Nexent 实例（包括其他 Agent 和账号），远超本次导入的范围。
- **不要**删除 `nexent/config/` 或 `skills/` 下的任何文件来“回滚 Nexent”——那些是源码，不是 Nexent 的副本。
