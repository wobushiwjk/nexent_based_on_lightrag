# 项目工作流与验收清单

这份文档把“计划”变成可检查的清单。**每个工作流只有满足验收项、并由你确认后，才算完成。**
没有演示命令、没有测试、没有写明边界的成果，不标记为完成。

状态图例：✅ 已完成并验收 / 🟡 已完成待验收 / 🚧 进行中 / ⏳ 未开始 / ⚠️ 有风险

## 总原则

1. 每个工作流开始前，先写清“交付物”和“验收方式”。
2. 结束时必须给出：做了什么、你怎么验证、预期输出、已知边界。
3. 测试只跑镜像目录，并报告目录和通过数；跨模块改动才跑全量。
4. 范围变化写进“变更记录”，不能静默消失；砍掉的功能要明确说“没做”。
5. 自动化优先，但本体发布、标准对齐、故障树发布保留人工审核。
6. 每个工作流结束时停下来，等你确认后再进下一个。

## 状态总览

| W | 名称 | 状态 | 依赖 |
| --- | --- | --- | --- |
| W0 | 项目骨架与基线清理 | ⏳ 部分跳过 | 无 |
| W1 | 领域本体与数据契约 | ✅ 已完成并验收 | W0 可并行 |
| W2 | MCP 只读服务 | ✅ 已完成并验收 | W1 |
| W3 | 证据链与图谱投影 | ✅ 已完成并验收 | W1、W2 |
| W4 | Skills 工作流 | ✅ 已完成并验收 | W2、W3 |
| W5 | Nexent 编排与导入 | 🟡 已完成待验收 | W2、W4 |
| W6 | 多模态数据管线 | ⏳ 未开始 | W3 |
| W7 | 教师-学生与主动学习 | ⏳ 未开始 | W3、W6 |
| W8 | 本体演化（上传标准→审核→迁移） | ⏳ 未开始 | W3、W5、W6 |
| W9 | 评测与消融 | ⏳ 未开始 | W2–W8 |
| W10 | 离线交付与演示 | ⏳ 未开始 | 全部 |

## 各工作流验收标准

### W0 项目骨架与基线清理

**目标**：确定 monorepo 目录，保留 LightRAG 核心，清理生成物，给后续工作流留位置。

**交付物**：`mcp_server/`、`skills/`、`nexent/`、`data/`、`eval/`、`deploy/` 的目录与 README；根目录种子文件归位；`.gitignore` 覆盖私有数据。

**验收**：目录存在且有 README；`git status` 中没有误提交的大文件/密钥/标准 PDF；现有测试仍能跑。

**当前**：用户选择先做 W1，W0 暂缓。W2 开始前补目录骨架即可，不阻塞 W1。

### W1 领域本体与数据契约

**目标**：把工业故障树本体落成可校验的领域包和 Pydantic 契约，作为后续自动化的 gold 基准。

**交付物**：
- `domain_packs/industrial_fault/ontology.yml`、标准 crosswalk、7 份 JSON Schema + manifest；
- 3 份种子文档、1 棵 gold 树、1 个反例 bundle；
- `lightrag/fault_tree/contracts.py`、`domain_validation.py`；
- `scripts/validate_domain.py`、`scripts/export_domain_schemas.py`；
- `tests/domain/`；
- `domain_packs/README.md`、工业包 README、医疗包占位 README。

**验收**：
```bash
python scripts/validate_domain.py                 # 0 errors
python scripts/validate_domain.py --bundle .../bad_bundle_sample.json  # 退出码 1
./scripts/test.sh tests/domain                    # 全部通过
uvx ruff@0.15.12 check <changed files>            # 通过
```
**边界**：这是概念级标准映射，不是条款级合规；自动上传/演化在 W6/W8，不在 W1。

**状态**：✅ 已完成并验收（2026-11-05）。

### W2 MCP 只读服务

**目标**：让 Agent 通过 MCP 查询本体、设备、故障、证据和故障树，不直接碰存储。

**交付物**：`mcp_server/`；工具至少包括 `get_ontology`、`get_equipment`、`search_faults`、`get_fault_tree`、`trace_evidence`、`validate_fault_tree`；租户隔离；错误码约定；工具级测试。

**验收**：不启动 Nexent，用 MCP Inspector 或脚本调用每个工具；只读工具不能写库；跨租户查询被拒绝；`tests/mcp/` 通过；给出工具清单和示例请求/响应。

**验收命令**：
```bash
# 真实 stdio 子进程调用 6 个工具，并校验跨租户拒绝与领域包未被写入
.venv/Scripts/python.exe scripts/mcp_smoke_test.py

# 工具级测试
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/mcp -q

# 领域包回归（W1）
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/domain -q
```

**当前结果**：`tests/mcp` 45 passed；`tests/domain` 20 passed；smoke test 全部通过。

**边界**：
- W2 读取 `demo_bundle.json` 静态快照；W3 已补充 storage-backed repository，
  工具契约保持不变（见 W3）。
- `tenant_id` 由调用方声明，W2 不做认证；身份认证/授权属于 Nexent 或前置网关（W5）。
- 每租户只索引一棵故障树；多树/多版本查询留给 W3 的投影服务。
- `search_faults` 是词法排序，不含向量检索或 reranker。
- 演示 bundle 没有 `HAS_FAULT` 事实，`get_equipment` 的 `faults` 为空是数据现状，
  不是查询缺陷；W3 投影时补关联。
- 只支持 AND/OR；动态门和定量 FTA 不在当前契约内。

**状态**：✅ 已完成并验收（2026-11-07）。

### W3 证据链与图谱投影

**目标**：把抽取结果变成 `Evidence` / `Fact` / `FaultCauseRule` / `FaultTreeDraft`，并写入图存储；证据正文不常驻内存。

**交付物**：
- 投影服务：校验后的 `FaultCandidateBatch` → 规范化实体、`Evidence`、`Fact`、`FaultCauseRule`；
- SQLite 证据库：按 `(tenant_id, target_type, target_id)` 建索引，启动只加载 ID/目标索引，正文按需读取；
- 故障树生成器：递归 AND/OR → `FaultCauseRule` + `LogicGate` + `HAS_CAUSE_RULE` / `INPUT_OF` / `OUTPUT_TO`；
- 校验器接入：投影结果必须通过 `validate_domain_bundle` / `validate_fault_tree_draft`；
- 幂等导入 CLI：3 份种子文档 + 固化抽取输入 → 证据库、图存储、可审计 bundle；
- storage-backed MCP repository：工具契约不变，证据正文懒加载。

**验收**：
1. 3 份种子文档导入后，生成的故障树节点/边与 `demo_bundle.json` 的 gold 树结构一致；
2. 每条 `Fact` / `FaultCauseRule` 的 `evidence_ids` 都能落到逐字证据，且 quote 是来源文档的子串；
3. 重复导入不产生重复证据行、重复事实/规则或重复图边；
4. `python scripts/validate_domain.py` 仍为 0 errors；
5. `tests/domain`、`tests/extraction`、`tests/mcp` 镜像子集通过；
6. 10 万条证据（约 180 字 quote、每条 2 个 target）下，MCP 仓储启动的 Python 堆增量 < 256 MiB，启动不读取 quote；
7. `trace_evidence` 的 SQLite 查询走 `(tenant_id, target_type, target_id)` 索引，查询耗时随命中数增长而不是随证据总量线性增长。

**验收命令**：
```bash
# 投影 3 份种子文档，校验 gold 树一致性、逐字证据和幂等重跑
.venv/Scripts/python.exe scripts/import_seed_domain.py --verify

# 大证据量内存与查询计划基准
.venv/Scripts/python.exe scripts/benchmark_evidence_memory.py --records 100000

# 域契约、投影/抽取、MCP 工具回归
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/domain -q
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/extraction -q
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/mcp -q

# W1 gold 口径不变
.venv/Scripts/python.exe scripts/validate_domain.py

# storage-backed MCP 真实 stdio 调用（导入后）
.venv/Scripts/python.exe scripts/mcp_smoke_test.py \
  --storage-dir domain_packs/industrial_fault/storage
```

**当前结果（2026-11-07）**：
- `import_seed_domain.py --verify`：9 实体、7 证据、4 事实、2 规则、树 8 节点/8 边；重建树与 gold 完全一致；证据 quote 均为原文子串；重复导入行数不变，且两次导入落在同一个内容寻址 run（`run-<sha256 前 12 位>`）；图 13 节点/12 边；查询计划使用 `idx_evidence_targets_trace`。
- 存储布局：`storage/tenants/<tenant>/runs/<run_id>/{bundle.json,bundle.full.json,evidence.sqlite3,manifest.json,graph/}`，`storage/tenants/<tenant>/active.json` 原子切换当前 run；MCP 仓储只读取 active 指向的 run。
- `benchmark_evidence_memory.py --records 100000`：启动堆增量约 0.12 MiB，启动读取 quote 数 0；单命中/100 命中目标查询中位数约 20µs/65µs。
- `tests/mcp` 54 passed；`tests/domain` 39 passed；`tests/extraction` 327 passed（已安装可选依赖 `ollama==0.6.2`（与 `uv.lock` 一致），驱动测试全部收集并通过）。
- `scripts/mcp_smoke_test.py --storage-dir ...` 全部工具通过，跨租户拒绝，领域包未被写入。

**状态**：✅ 已完成并验收（2026-11-08）。

**边界**：
- W3 的种子导入使用 `seeds/extraction/` 下固化的抽取候选（带逐字 quote），保证离线、可重复；真实本地模型抽取在 W7 接入，投影/校验/落库接口不变。
- 图存储写入验证使用 NetworkX 文件后端；生产后端（Neo4j/PostgreSQL）共用同一套 `upsert_node` / `upsert_edge` 调用，但 W3 不做多后端回归。
- 重复导入按内容寻址：bundle 内容相同则写回同一个 `runs/<run_id>`，`upsert` 不产生重复行；内容变化则写新 run，并由 `active.json` 原子切换，旧 run 原样保留用于审计，不会再出现"旧证据行/旧图边和新 bundle 混在一起"的情况。导入器的 `prune_tenant=True`（种子导入内部使用）会删除其他 run 和旧版布局；"抽取结果变化后的逐文档回收"仍属于 W8。
- `trace_evidence` 只对命中的证据读取正文；不提供全量证据导出。
- **嵌套 AND/OR 已支持**：`OUTPUT_TO` 允许 `LogicGate → LogicGate`；嵌套表达式会展开成多层 `LogicGate`，子门通过 `OUTPUT_TO` 汇入父门，只有规则根门输出到故障事件。嵌套门带 `expression_path`（如 `0`、`1.2`）用于回指规则表达式中的位置；校验器要求门→门两端属于同一 `rule_uid`。
- storage 模式通过 `MCP_DOMAIN_STORAGE_DIR` 或 `--storage-dir` 启用，读取 `storage/tenants/<tenant>/active.json` 指向的 run；仍兼容旧布局 `tenants/<tenant>/bundle.json`。active 指针悬空时拒绝启动，不静默回退。运行时 bundle 不含 quote，正文只存在 SQLite。

### W4 Skills 工作流

**目标**：把“故障诊断、证据核验、本体审核”封装成可复用 Skill，而不是写死在 Agent 提示词里。

**交付物**：`skills/` 下的 `SKILL.md`、输入/输出契约、示例调用、失败处理；至少 3 个 Skill：诊断、证据核验、本体变更草案。

**验收**：每个 Skill 有独立测试或可执行脚本；输入缺失/证据不足时返回结构化错误，不编造；Skill 只通过 MCP 工具访问数据。

**已交付**：
- `skills/fault_diagnosis/`：设备定位 → 候选故障 → 逐字证据 → 只引用 MCP 数据的结论；无候选/无证据返回 `needs_more_evidence`。
- `skills/evidence_verification/`：按 claim 重算 `sha256(quote)`、校验字符区间/来源文件/目标关联；单条目标缺失只拒绝该条，不中断批次；可同时校验故障树。
- `skills/ontology_change_draft/`：`add_class` / `add_relation` / `add_constraint` / `deprecate_class` 四类变更，输出 diff、阻断冲突、影响分析和审核清单；`review.applied` 恒为 `false`。
- `skills/runtime.py`：`ToolClient` 协议、`READ_ONLY_TOOLS` 白名单、统一信封、证据校验助手；handler 不导入 `lightrag` 或 `mcp_server`。
- `scripts/export_skill_schemas.py`：从 Pydantic 模型生成 `input/output.schema.json` 与 `skills/manifest.json`。
- `scripts/run_skill.py`：启动真实 MCP stdio 子进程执行 Skill，与 Agent 使用同一条数据通路。
- `tests/skills/`：三个 Skill 的行为测试、共享契约测试、真实 stdio CLI 测试。

**验收命令**：
```bash
# 1. 生成 schema 与 manifest（修改 Pydantic 模型后必须重跑）
.venv/Scripts/python.exe scripts/export_skill_schemas.py

# 2. Skill 测试（含真实 stdio 子进程）
PYTHON=.venv/Scripts/python.exe ./scripts/test.sh tests/skills -q

# 3. 逐个 Skill 的离线调用（退出码 0 表示 ok=true）
.venv/Scripts/python.exe scripts/run_skill.py \
  --skill fault_diagnosis \
  --input skills/fault_diagnosis/examples/request.json
.venv/Scripts/python.exe scripts/run_skill.py \
  --skill evidence_verification \
  --input skills/evidence_verification/examples/request.json
.venv/Scripts/python.exe scripts/run_skill.py \
  --skill ontology_change_draft \
  --input skills/ontology_change_draft/examples/request.json
```

**当前结果（2026-11-08）**：
- schema 生成成功：6 个 `input/output.schema.json` 与 `skills/manifest.json` 已写入并通过漂移检查。
- `tests/skills`：**58 passed, 1 warning in 11.41s**（warning 来自 anyio 的 pytest 弃用提示，与 Skill 无关）。
- 真实 MCP stdio 示例调用均退出码 0：
  - `fault_diagnosis` → `ok=true`, `status=diagnosed`，调用 `get_ontology → get_equipment → search_faults → trace_evidence ×4 → get_fault_tree → validate_fault_tree`。
  - `evidence_verification` → `ok=true`, `status=verified`，调用 `trace_evidence ×2 → validate_fault_tree`。
  - `ontology_change_draft` → `ok=true`, `status=draft`，调用 `get_ontology → trace_evidence`；输出 `review.applied=false`。

**状态**：✅ 已完成并验收（2026-11-10）。

**边界**：
- Skill 是确定性编排，不调用大模型；自然语言推理由 Nexent Agent 完成，`handler.py` 只做查找、取证、校验和结论模板。
- 只读：不写证据、不发布本体、不修改故障树；发布与迁移属于 W8。
- `tenant_id` 由调用方声明，不做认证；认证/授权在 Nexent 或前置网关（W5）。
- 本体草案只支持新增和废弃，不支持字段级修改；`rule` 文本不做语义校验。
- `SKILL.md` 与 `handler.py` 是同一规程的两份产物，必须同步修改；schema 漂移由 `tests/skills/test_skill_contracts.py` 拦截。

### W5 Nexent 编排与导入

**目标**：在 Nexent 中注册 MCP、Skill、Agent 和知识库，形成可演示的问答/诊断闭环。

**交付物**：`nexent/` 下的版本锁定、配置文件、脚本化导入、Agent 提示词、演示脚本、回滚说明。

**验收**：在 Nexent 中从零导入成功；Agent 能调用 W2 工具和 W4 Skill；完成一次“设备故障诊断 + 证据展示”；重启后配置不丢；给出录屏或截图。

**已交付**（2026-11-10）：
- `nexent/versions.lock.json`：锁定 Nexent v2.8.3（commit `5876d6ec`）、端口、MCP 传输方式、Agent/知识库名称、领域包与 Skill 版本。
- `nexent/config/{mcp,agent,knowledge_base,skills}.json`：MCP 注册、Agent 工具/Skill/示例问题、知识库文档、Skill 改名映射。
- `nexent/agent/system_prompt.md`：诊断流程、证据硬规则（逐字引用 + `[evidence:id]`）、输出模板、边界。
- `nexent/kb/01..03`：报告写法、泵故障术语、证据政策三份知识库文档（明确声明自身不是证据）。
- `nexent/scripts/nexent_client.py`：标准库 HTTP 客户端，覆盖身份、模型、MCP、工具、Skill、知识库、Agent 接口。
- `nexent/scripts/agent_bundle.py`：按实时工具目录生成 `POST /agent/import` 请求体；MCP 工具强制 `params={}`。
- `nexent/scripts/build_skills.py`：把 `skills/` 打包成确定性 ZIP（同一输入字节一致）。
- `nexent/scripts/import_to_nexent.py`：8 步幂等导入；`--dry-run`、`--refresh-skills`、`--no-replace-agent`、`--skip-kb`、`--seed auto|always|never`；报告脱敏写入 `nexent/build/import-report.json`。
- `nexent/scripts/demo.py`：经北向 API `POST /nb/v1/chat/run` 跑演示问题，transcript 写入 `nexent/build/demo-*.json`。
- `nexent/README.md`、`nexent/ROLLBACK.md`、`nexent/.env.example`。
- `tests/nexent/`：离线测试，覆盖客户端请求构造/错误、bundle 校验、Skill 打包确定性、8 步流程、演示 transcript。

**导入顺序**（不可重排）：
1. 登录并读取 tenant_id（Nexent 会把会话租户注入 MCP 工具，种子数据必须写在同一租户下）；
2. 导入种子领域包（`active.json` 存在则跳过）；
3. 注册 MCP 并做连通性检查；
4. 扫描工具目录，确认 6 个工具齐全；
5. 打包并上传 Skill；
6. 创建/复用知识库并等待切分完成；
7. 导入 Agent（默认删除同名旧 Agent 后重建）；
8. 校验 Agent 已发布、MCP 工具仍在目录中。

**关键约束**：
- 首次导入必须跑两遍：种子要求 MCP 停止（Windows 文件句柄），而 MCP 启动要求种子已存在；第二遍自动跳过种子。
- MCP 工具的 ToolConfig `params` 必须为空——Nexent 对 MCP 工具不记录初始化参数，任何自填参数都会在导入校验时被拒绝。
- Skill 的 `use_existing` 复用已存内容，改 `SKILL.md` 后必须加 `--refresh-skills` 先删后建。
- `tenant_id` 对模型不可见，由 Nexent 在调用时注入；提示词禁止模型填写或询问它。

**验收命令**：
```bash
# 离线测试（已执行：58 passed）
./scripts/test.sh tests/nexent

# 两遍导入（第二遍见 README 的 MCP 启动步骤）
.venv/Scripts/python.exe -m nexent.scripts.import_to_nexent
.venv/Scripts/python.exe -m nexent.scripts.demo
```

**验证进展**（2026-11-10）：
- `tests/nexent`：58 passed（Python 3.13.5）。
- `ruff` 未安装在该 venv，`uvx ruff` 拉取超时，lint 尚未执行。
- 实机验收被部署环境阻断：`nexent-config`（5010）、`nexent-runtime`（5014）、
  `supabase-db` 等容器为 Exited(127)，Docker bind mount 报 “not a directory”；
  5010 未监听，导入脚本无法登录。需先在 WSL 部署目录重建容器。
- 待 Nexent 恢复后执行两遍导入、demo transcript、重启验证；
  `host.docker.internal:8765` 连通性随第 3 步一并验证。

### W6 多模态数据管线

**目标**：解析 PDF、表格、图片，生成带页码/版面/字符区间的证据。

**交付物**：解析路由、OCR/VLM 适配、表格结构化、证据定位、失败重试；本地模型优先。

**验收**：3 种模态各至少 1 份样例导入；quote 逐字校验通过；原始数据不出本地；失败文件有明确状态和错误原因。

### W7 教师-学生与主动学习

**目标**：用强模型生成/标注数据，蒸馏到本地小模型，并让人工审核集中在低置信样本。

**交付物**：训练集生成脚本、蒸馏/微调脚本、主动学习队列、模型卡、评测报告。

**验收**：给出基线 vs 蒸馏后的抽取/诊断指标；模型卡写明数据来源、限制和隐私处理；低置信样本可进入审核队列。

### W8 本体演化

**目标**：用户上传标准/文档，系统生成本体变更草案，人工审核后版本升级并迁移实例。

**交付物**：候选概念/关系/约束抽取、与现有本体 diff、冲突检测、影响分析、审核接口/页面、版本升级、Schema 重生成、实例迁移、回滚。

**验收**：上传一份标准节选，生成可读 diff；不自动发布；能指出受影响的实例/树/测试；审核通过后版本号递增、Schema 更新、旧数据仍可查询；`gaps` 和变更日志完整。

**边界**：标准通常改本体/约束，设备手册才改实例；两者共用“候选→审核→发布”管线，但输出不同。

### W9 评测与消融

**目标**：证明检索、抽取、故障树和证据链各自有效，而不是只演示一次成功。

**交付物**：评测集、基线、消融配置、指标脚本、报告；至少覆盖检索命中、证据准确率、树结构正确率、端到端诊断。

**验收**：一键复现指标；报告写明数据、参数、随机种子和失败案例；与基线对比有结论。

### W10 离线交付与演示

**目标**：在无外网环境一键安装并演示。

**交付物**：镜像、模型权重、配置、Skills、种子数据、`install.sh`、`manifest.json`、验收脚本、演示脚本；标准 PDF 等版权材料不进包。

**验收**：干净机器断网安装成功；`manifest.json` 校验通过；演示脚本完成“上传/查询→诊断→证据→结论”；卸载/回滚可用。

## W1 计划对照

| 计划项 | 状态 | 证据 |
| --- | --- | --- |
| 本体 YAML | ✅ | `domain_packs/industrial_fault/ontology.yml` |
| 数据契约 | ✅ | `lightrag/fault_tree/contracts.py` |
| JSON Schema + manifest | ✅ | `domain_packs/industrial_fault/schemas/` |
| 标准 crosswalk + gaps | ✅ | `standards/crosswalk.yml` |
| 3 份种子文档 | ✅ | `seeds/documents/` |
| gold 故障树 | ✅ | `seeds/demo_bundle.json` |
| 反例 bundle | ✅ | `seeds/bad_bundle_sample.json` |
| 校验 CLI | ✅ | `scripts/validate_domain.py` |
| 领域测试 | ✅ | `tests/domain/`，20 passed |
| 医疗迁移占位 | ✅ | `domain_packs/medical_device/README.md` |
| 条款级合规矩阵 | ⏳ 待标准正文 | 不能在没正文时编造条款号 |
| 自动上传/本体演化 | ⏳ W6/W8 | 不是 W1 范围 |

## W2 计划对照

| 计划项 | 状态 | 证据 |
| --- | --- | --- |
| `mcp_server/` 分层骨架 | ✅ | `repository.py` / `service.py` / `server.py` / `__main__.py` |
| 6 个只读工具 | ✅ | `get_ontology`、`get_equipment`、`search_faults`、`get_fault_tree`、`trace_evidence`、`validate_fault_tree` |
| 统一响应信封 | ✅ | `models.ToolResponse`，成功/失败示例见 `mcp_server/README.md` |
| 错误码约定 | ✅ | `invalid_argument` / `not_found` / `ambiguous_selector` / `unsupported` / `internal_error` |
| 租户隔离 | ✅ | 每租户独立索引；未知租户 `not_found` 且不枚举其他租户 |
| 只读保证 | ✅ | 全部 `readOnlyHint=true`；测试比对领域包 SHA-256 摘要 |
| stdio / SSE / HTTP 传输 | ✅ | `python -m mcp_server --transport ...` |
| 工具级测试 | ✅ | `tests/mcp/`，45 passed |
| 无需 Nexent 的调用脚本 | ✅ | `scripts/mcp_smoke_test.py` |
| 示例请求/响应 | ✅ | `mcp_server/README.md` |
| 认证/授权 | ⏳ W5 | W2 的 `tenant_id` 是调用方声明，不能直接暴露到不可信网络 |
| LightRAG 实时图谱 | ⏳ W3 | W2 读取静态 bundle 快照 |
| 多树/多版本查询 | ⏳ W3 | 当前每租户只索引一棵树 |

## 防止“做得不如计划”的机制

1. **先写验收，再写代码**：每个 W 的验收项在本文件里，完成不了就不打勾。
2. **每个 W 结束必须给可复现命令**：不是“我做好了”，而是“你运行这条命令能看到什么”。
3. **已知边界必须写**：没做的、只做一半的、依赖外部条件的，都写进对应 W 的“边界”。
4. **变更要留痕**：新增/删除/推迟的功能写进变更记录，不静默消失。
5. **你逐 W 验收**：你不确认，不进入下一个 W。
6. **质量优先于速度**：宁可少做一个演示功能，也不把未验证的东西标成完成。

## 变更记录

| 日期 | 变更 | 原因 |
| --- | --- | --- |
| 2026-11-05 | 建立本清单；W1 标记为待验收 | 让计划可检查，避免范围漂移 |
| 2026-11-05 | W1 标记为已完成并验收 | 用户确认 W1；W2 以 W1 的领域包和契约为输入 |
| 2026-11-05 | 完成 W2 只读 MCP 服务，标记为待验收 | 6 个工具、租户隔离、错误码、stdio 传输和 45 个测试已落地 |
| 2026-11-05 | 新增 `mcp_server/README.md` 与 `scripts/mcp_smoke_test.py` | 让验收不依赖 Nexent，并固定工具清单、示例和只读边界 |
| 2026-11-07 | W2 标记为已完成并验收；W3 标记为进行中 | 用户确认 W2；W3 以 W1 契约和 W2 工具契约为输入 |
| 2026-11-07 | 为 W3 增加 3 条证据内存/查询验收标准 | 10 万条证据不能全量驻留；启动不读 quote；`trace_evidence` 必须走索引 |
| 2026-11-07 | 完成 W3 投影、SQLite 证据库、故障树生成、幂等导入和 storage-backed MCP | 让抽取结果可追溯到逐字证据，同时控制大证据量下的内存 |
| 2026-11-07 | 记录嵌套 AND/OR 树形限制 | 校验器不支持门→门，暂以 `UnsupportedTreeShapeError` 显式拒绝 |
| 2026-11-07 | 解除嵌套 AND/OR 树形限制 | 按用户要求扩展 `OUTPUT_TO` 为门→门，树生成器、校验器、图预览、本体同步；种子树与 gold 保持一致 |
| 2026-11-07 | 导入改为版本化快照 + `active.json` 指针 | 内容变化时旧证据/旧图不再与新 bundle 混用；相同内容仍写同一 run，幂等验收不变 |
| 2026-11-08 | W3 标记为已完成并验收；W4 标记为进行中 | 用户确认 W3；W4 以 W2 工具和 W3 证据库为输入 |
| 2026-11-08 | 完成 W4 三个 Skill、统一信封、schema 导出和 stdio 执行脚本 | 让诊断、证据核验、本体草案可被 Nexent 复用，同时保持离线可测试、只读、不编造 |
| 2026-11-08 | W4 记录“确定性编排、不调用大模型”的边界 | 模型解释与证据必须分离，离线评测不受模型波动影响 |
| 2026-11-08 | W4 生成 schema、通过 58 个测试并跑通三个 stdio 示例，标记为待验收 | 验收命令与结果可复现，等待用户确认后再进入 W5 |
| 2026-11-10 | W4 标记为已完成并验收；W5 标记为进行中 | 用户确认 W4；W5 以 W2 工具、W4 Skill 和 W3 storage 为输入 |
| 2026-11-10 | 新增 `nexent/` 版本锁定、Skill 打包、脚本化导入、Agent 提示词、演示脚本与回滚说明 | 让 Nexent 侧注册可从零复现，而不是手工点页面 |
