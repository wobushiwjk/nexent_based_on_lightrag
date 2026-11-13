# Nexent 集成（W5）

本目录把仓库里的领域与治理服务注册进一个已经运行的 Nexent 实例，形成可演示的问答、诊断和本体版本查询闭环：

- **领域 MCP**：`mcp_server/` 暴露的 6 个只读领域工具；
- **治理 MCP**：`governance_mcp_server/` 暴露版本查询、草案、审核、影响分析、迁移发布、回滚和快照历史工具；
- **Skill**：`skills/` 下的 3 个确定性技能，由 `nexent/scripts/build_skills.py` 打包成 ZIP 上传；
- **知识库**：`nexent/kb/` 下的 3 份写作规范/术语/证据政策文档；
- **Agent**：`industrial_fault_agent`，提示词在 `nexent/agent/system_prompt.md`。

当前 `config/agents.json` 定义三个 Agent：

- `industrial_fault_agent`：主 Agent，负责最终答复；
- `evidence_review_agent`：证据复核子 Agent；
- `ontology_expert_agent`：本体专家子 Agent。

主 Agent 通过 Nexent `managed_agents` 管理两个子 Agent；子 Agent 无权发布或回滚。

Nexent 本身**不在本仓库内**，也不需要把源码放进仓库。本仓库是领域服务的唯一事实来源，Nexent 是消费者；删除 Nexent 里的任何配置都不会丢源码，重新导入即可恢复。

## 验收对照

| W5 验收项 | 证据 |
| --- | --- |
| 从零导入 Nexent | `nexent/build/import-report.json` 中 `ok: true`，8 个步骤全部有记录 |
| Agent 能调用 W2 工具 | 演示 transcript 的 `events` 中 `tool` / `tool_result` 类事件计数 > 0；导入报告第 4 步列出 6 个 MCP 工具 |
| Agent 能调用 W4 Skill | 导入报告第 5 步列出 3 个 Skill；Agent 绑定 `skill_names`；对话中 `read_skill_md` / `run_skill_script` 出现在事件里 |
| Agent 能查询本体治理 | 用户询问当前本体版本时，Agent 调用 `get_ontology_governance_status`，答复包含活动版本和安装信息 |
| 三个 Agent 正确绑定 | 导入报告列出 3 个 Agent；`/agent/call_relationship/<main_id>` 的主 Agent 下有两个子 Agent |
| 完成一次「设备故障诊断 + 证据展示」 | `nexent/build/demo-*.json` 中的 `final_answer`，含 `[evidence:...]` 与逐字引文 |
| 配置在重启后仍在 | 重启 Nexent 容器后重跑演示脚本，回答不变；Agent 仍在列表中 |
| 录屏或截图 | Web UI 对话截图 + `demo-*.json` + `import-report.json` 三件套 |

## 组件与端口

以当前部署（Docker Desktop）为例：

| 容器 | 端口 | 用途 |
| --- | --- | --- |
| `nexent-config` | 5010 | 管理/配置 API，导入脚本的主入口 |
| `nexent-runtime` | 5014 | 运行时 API，由配置服务驱动，脚本不直接调用 |
| `nexent-northbound` | 5013 | 北向对话 API，演示脚本使用 |
| `nexent-web` | 3000 | Web UI |
| `nexent-mcp` | 5011 | Nexent 自带的 MCP 管理服务，**不是**本仓库的 MCP Server |
| `nexent-postgres` / `kong` 等 | — | 基础设施 |

本仓库的领域 MCP 默认监听 **8765**，本体治理 MCP 默认监听 **8766**，均独立于上表。

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `versions.lock.json` | 锁定的 Nexent 版本与接口约定 |
| `config/mcp.json` | MCP 服务注册名、容器内可达 URL、传输方式 |
| `config/governance_mcp.json` | 本体治理 MCP 注册名、容器内可达 URL、传输方式 |
| `config/agents.json` | 当前三 Agent 图、工具/Skill 权限、知识库和示例问题 |
| `config/agent.json` | 早期单 Agent 配置，保留兼容 |
| `config/knowledge_base.json` | 知识库显示名、待导入文档、切分策略 |
| `agent/system_prompt.md` | Agent 系统提示词（证据规则、诊断流程、输出模板） |
| `agent/evidence_review_prompt.md` | 证据复核子 Agent 的输出契约 |
| `agent/ontology_expert_prompt.md` | 本体专家子 Agent 的只建议、不发布边界 |
| `kb/*.md` | 知识库文档：报告写法、术语、证据政策 |
| `scripts/nexent_client.py` | 标准库 HTTP 客户端，封装导入用到的全部接口 |
| `scripts/agent_bundle.py` | 从配置 + 实时工具目录生成 `POST /agent/import` 请求体 |
| `scripts/build_skills.py` | 把 `skills/` 打包成确定性 Skill ZIP |
| `scripts/import_to_nexent.py` | 8 步幂等导入 |
| `scripts/demo.py` | 通过北向 API 跑演示问题并保存 transcript |
| `.env.example` | 端点、账号、MCP URL 模板 |
| `ROLLBACK.md` | 逐项回滚说明 |

`nexent/build/` 是生成物（Skill ZIP、导入报告、演示 transcript），已被 `.gitignore` 的 `build/` 规则忽略，不要提交。

## 前置条件

1. Nexent 已部署并运行，版本 **v2.8.0**；Web UI 能登录。
2. Nexent 里已配置**一个对话模型**和**一个 embedding 模型**。导入第 6 步建知识库要 embedding 模型，第 7 步导入 Agent 要对话模型；没有就先在「模型管理」里添加。
3. 本仓库的 Python 环境可用（`.venv/Scripts/python.exe`），且已安装 `mcp` 依赖（`uv sync --extra api` 或按 `mcp_server/README.md`）。
4. 端口 **8765、8766** 可从 Docker 容器访问到宿主机。Docker Desktop 用 `host.docker.internal`；首次启动两个 MCP Server 时 Windows 防火墙弹窗要允许专用网络。
5. 确认管理员账号。默认是 `suadmin@nexent.com` / `Nexent@123`；如果你在首次部署前设置过 `NEXENT_SUPER_ADMIN_PASSWORD`，用你设置的那个。

## 配置

```bash
cd "/d/大学/大三上/华为ICT/LightRAG/LightRAG"
cp nexent/.env.example nexent/.env
# 编辑 nexent/.env：至少确认 NEXENT_PASSWORD 和 NEXENT_MCP_URL
set -a; source nexent/.env; set +a
```

脚本只读进程环境，不解析 `.env` 文件；上面两行负责把它导入当前 shell。

## 从零导入（两遍流程）

第一次导入必须跑两遍，原因是顺序上的硬约束：**种子数据必须先写入 MCP 的存储目录，而 MCP Server 启动时要求该目录里已有可用的租户 bundle；种子导入又要求 MCP 停止**（Windows 上 MCP 持有 `evidence.sqlite3` 的打开句柄，重写会失败）。所以：

### 第一遍：导入种子（此时 MCP 未启动）

如果 MCP Server 已经在运行，先 `Ctrl+C` 停掉。

```bash
.venv/Scripts/python.exe -m nexent.scripts.import_to_nexent
```

预期：第 1 步拿到 tenant_id；第 2 步导入种子并输出 `imported`；第 3 步报 MCP 不可达并停下。这是**预期行为**，不是失败。报告里会记录 tenant_id，后面回滚要用。

### 启动两个 MCP Server

领域 MCP：

```bash
.venv/Scripts/python.exe -m mcp_server \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8765 \
  --storage-dir domain_packs/industrial_fault/storage
```

治理 MCP（另一个终端）：

```bash
.venv/Scripts/python.exe -m governance_mcp_server \
  --bootstrap-if-empty \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8766
```

保持两个终端开着。第二遍导入的第 3 步会分别做连通性检查，失败时会直接给出原因；若失败，见「故障排查」。

### 第二遍：注册全部组件

```bash
set -a; source nexent/.env; set +a
.venv/Scripts/python.exe -m nexent.scripts.import_to_nexent
```

这一遍第 2 步检测到 `storage/tenants/<tenant_id>/active.json` 存在会自动跳过，然后依次：

1. 初始化空白治理库并注册两个 MCP（`add` 或 `update` + `enable`）；
2. 扫描工具目录，确认 Agent 所需的 18 个 MCP 工具都在；
3. 打包并上传 3 个 Skill（首次创建）；
4. 创建知识库、上传 3 份文档、等待切分完成；
5. 导入 Agent（自动发布 V1）；
6. 校验 Agent 已发布、两个 MCP 的工具仍在目录中。

成功时终端打印 `{"ok": true, "report": ".../nexent/build/import-report.json"}`。

### 重新导入

日常修改提示词、Skill 或配置后：

```bash
# MCP 保持运行即可
.venv/Scripts/python.exe -m nexent.scripts.import_to_nexent --refresh-skills
```

- Agent 默认**删除后重建**（同名旧版本会消失），加 `--no-replace-agent` 可在存在时直接失败；
- `--refresh-skills` 先删后建，否则 Nexent 的 `use_existing` 会复用旧 Skill 内容；
- 加 `--dry-run` 只检查不修改，但需要 Nexent 与 MCP Server 都在线（第 3 步要做连通性检查）。

## 演示

```bash
.venv/Scripts/python.exe -m nexent.scripts.demo \
  --query "P-101A 冷却水泵出口压力不足，可能的原因有哪些？请逐条给出证据出处，并说明故障树中的位置。" \
  --query "当前本体是哪个版本？安装时间和类、关系数量是什么？"
```

脚本会登录、创建/复用 API Key、调用 `POST /nb/v1/chat/run`，把完整事件序列与最终回答写到 `nexent/build/demo-<UTC时间>.json`。

`--query` 可重复传入多个问题；默认问题已按种子数据（P-101A / 出口压力不足 / 过滤器堵塞 / 汽蚀 / 法兰漏气）设计。

验证标准驱动的本体草案：

```bash
.venv/Scripts/python.exe -m nexent.scripts.demo \
  --query "根据下面标准节选生成本体 v0.2 的变更草案：条件监测系统应识别安装在旋转设备上的传感器。请把‘传感器’作为 Asset 的新增子类，并给出来源原文。"
```

预期 Agent 调用 `create_ontology_change_draft`，返回草案 ID、操作数和证据数，
并明确说明草案尚未审核、未迁移、未发布。

完整本体演化流程可在 Nexent 中按以下顺序对话：

1. “把 CHG-... 提交审核。”
2. “分析 CHG-... 的影响。”
3. “我确认批准 CHG-...。”
4. “对 CHG-... 做迁移 dry-run。”
5. “我确认发布 CHG-...。”
6. “列出该租户的故障树快照历史。”
7. “我确认回滚 CHG-...，原因是……。”

普通“继续”不会构成审核或发布授权；Agent 必须分别调用带 `confirm=true`
的审核、发布和回滚工具。

**注意**：北向 API 只能看到**已发布**的 Agent。导入流程会自动发布；如果你在 UI 里手动改过 Agent 但没有发布，演示会报 `Published agent not found`。

## 重启后验证配置仍在

```bash
cd /home/midnightdance/nexent   # 你的 Nexent 部署目录
docker compose restart
```

等容器全部就绪后，直接重跑演示脚本。配置存在 Postgres 卷里，重启不会丢；Skill ZIP 与 Agent 绑定关系也会保留。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 第 3 步领域 MCP `not reachable from Nexent` | 容器里解析不到 `host.docker.internal`，或 8765 没放行。先用容器内测试：`docker exec -it nexent-config curl -s http://host.docker.internal:8765/mcp`；不通就改用宿主机局域网 IP（`ipconfig` 的 IPv4），`--mcp-url http://<IP>:8765/mcp` |
| 第 3 步治理 MCP `not reachable from Nexent` | 8766 没启动或没放行。先确认 `governance_mcp_server` 正在运行，再用容器内 `curl http://host.docker.internal:8766/mcp` 检查；也可传 `--governance-mcp-url` |
| MCP 启动报 `no runtime domain bundles found` | 种子还没导入。先跑第一遍导入 |
| 种子导入报文件被占用 | MCP Server 还开着。停掉它再导入 |
| `tools are missing from the Nexent catalog` | MCP 已注册但未启用或扫描失败。`GET /mcp/healthcheck?mcp_id=...` 看状态，必要时 `POST /mcp/enable` 后重跑 |
| 第 7 步 `no LLM model matches` | Nexent 里没有对话模型，或 `agent.json` 的 `model_name` 与实际不符；先在 UI 配模型，或留空用第一个 |
| 第 6 步 `no embedding model configured` | 同上，先配 embedding 模型 |
| 知识库一直 `PROCESSING` | 增大 `--wait-kb-seconds`；切分在后台继续，稍后重跑会跳过已完成的文件 |
| 演示报 `Published agent not found` | Agent 未发布，或名字不对；`GET /agent/by-name/industrial_fault_agent` 确认 |
| Skill 改了但 Nexent 里没变 | 重新导入时加 `--refresh-skills` |

## 更新与回滚

- 更新：改仓库里的配置/提示词/Skill → 重跑导入（Skill 变更加 `--refresh-skills`）。
- 回滚：见 [ROLLBACK.md](ROLLBACK.md)。删除顺序是 Agent → Skill → 知识库 → 两个 MCP 注册 → 种子与治理存储。

## 版本锁定

见 [versions.lock.json](versions.lock.json)。Nexent 版本变化时，先核对 `nexent_client.py` 中各接口路径与字段，再更新锁定文件。
