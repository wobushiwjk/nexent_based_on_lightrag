# 工业设备故障诊断项目已完成工作总结

更新时间：2026-11-15

本文只汇总已经落地，并有代码、测试、报告或服务器运行结果支撑的工作。
后续计划和尚未形成的交付物不在本文范围内。

## 1. 项目定位

项目面向先进制造企业的工业设备故障诊断与本体治理场景，以 Nexent 作为用户
唯一交互入口，以 LightRAG 工业故障领域包作为数据与服务底座，由多个受限
MCP 服务和 Agent 完成：

- 设备故障检索与原因诊断；
- 逐字证据追溯与证据缺口识别；
- AND/OR 故障树生成与校验；
- 标准/设备资料上传；
- 本体变更草案、审核、迁移、发布和回滚；
- 本地小模型结构化抽取。

系统强调“先证据、后结论”。没有逐字原文支持的内容不得作为正式确认结论，
无法通过本体或证据校验的候选不得进入活动知识快照。

## 2. 已完成总体架构

```text
Nexent Web UI
    │
    ▼
工业设备故障诊断主 Agent
    ├── 证据复核 Agent
    └── 本体专家 Agent
    │
    ├── industrial-fault-domain MCP
    │     └── 本体、设备、故障、证据、故障树查询与校验
    │
    ├── ontology-governance MCP
    │     └── 版本、草案、审核、影响分析、迁移、发布、回滚、快照
    │
    └── industrial-fault-ingestion MCP
          ├── 用户文件发现与解析
          ├── W7 本地学生模型抽取
          ├── 证据与本体校验
          └── 租户快照追加

存储层
    ├── 租户运行时 bundle / evidence.sqlite3
    ├── NetworkX 领域图
    ├── 不可变本体版本库
    ├── 变更单、审核事件与发布记录
    └── 租户快照历史
```

## 3. W1–W4：领域、证据、图谱、MCP 与 Skill

### 3.1 工业故障本体与数据契约

已完成：

- `domain_packs/industrial_fault/ontology.yml`；
- 9 个类、11 条关系、8 条约束；
- IEC 61025、ISO 14224、IEC 60812、ISO 13374 的标准映射；
- Department、File、Equipment、FaultEvent、FaultCauseRule、LogicGate、
  Evidence、Fact、DomainBundle、FaultTreeDraft 等 Pydantic 契约；
- 7 份 JSON Schema 及 manifest 哈希校验；
- 领域 bundle、证据、故障树和查询计划的自动校验。

### 3.2 版本化领域存储

每个租户的数据按内容寻址的不可变 run 保存：

```text
storage/tenants/<tenant>/
  active.json
  runs/<run_id>/
    bundle.json
    bundle.full.json
    evidence.sqlite3
    manifest.json
    graph/
```

活动指针原子切换。相同 bundle 重复导入得到同一 `run_id`，不会产生重复快照。

### 3.3 只读领域 MCP

`mcp_server/` 已完成 6 个工具：

- `get_ontology`
- `get_equipment`
- `search_faults`
- `get_fault_tree`
- `trace_evidence`
- `validate_fault_tree`

工具返回统一信封：

```json
{
  "ok": true,
  "data": {},
  "error": null,
  "meta": {}
}
```

包含租户隔离、稳定错误码、证据按需加载、只读保证和 stdio/SSE/HTTP 传输。

### 3.4 三个确定性 Skill

已完成：

- `fault-diagnosis`：设备确认、候选故障检索、证据追溯、故障树读取与校验；
- `evidence-verification`：quote、哈希、字符区间、来源文件和目标关联核验；
- `ontology-change-draft`：标准候选到本体变更草案的确定性流程。

Skill 不调用大模型，不直接访问存储，只通过 MCP ToolClient 串联查询和校验。

## 4. W5：Nexent 多智能体业务入口

### 4.1 主 Agent 与两个受限子 Agent

当前声明式配置包含：

- 主 Agent：`industrial_fault_agent`
- 证据复核子 Agent：`evidence_review_agent`
- 本体专家子 Agent：`ontology_expert_agent`

主 Agent 通过 Nexent `managed_agents` 调用两个子 Agent。子 Agent 无发布、
回滚和治理写入权限。实际 Nexent 对话中已经出现两个子 Agent 的真实调用及
各自独立输出。

这里的协作机制是 Nexent Managed Agent。项目没有外部 A2A Server 注册，
因此文档和演示应使用“Nexent 多智能体协作”，不能表述为外部 A2A 协议。

### 4.2 三个 MCP 服务

Agent 同时绑定：

- `industrial-fault-domain`：8765；
- `ontology-governance`：8766；
- `industrial-fault-ingestion`：8767。

导入流程支持 MCP 注册/更新、工具扫描、Skill 上传、知识库创建、三 Agent
导入、发布关系校验和配置报告。

### 4.3 脚本化与可回退

已完成：

- `nexent/scripts/import_to_nexent.py`：8 步幂等导入；
- `nexent/scripts/agent_bundle.py`：从实时工具目录构建 Agent bundle；
- `nexent/scripts/build_skills.py`：确定性 Skill ZIP；
- `nexent/scripts/demo.py`：通过北向 API 调用已发布 Agent 并保存 transcript；
- `nexent/ROLLBACK.md`：Agent、Skill、知识库和 MCP 的回退顺序。

## 5. W6：多模态与用户上传链路

已完成本地解析：

- PDF 逐页文本；
- CSV 行；
- XLSX 工作表与行；
- PNG/JPEG 本地 VLM 转写；
- Markdown/TXT 文本。

解析结果保存：

- 原文件资产；
- UTF-8 transcript；
- 页码、章节、行级 section path；
- 原文字符区间；
- 解析状态和失败原因。

### Nexent 上传导入 MCP

`ingestion_mcp_server/` 向用户提供：

- `list_uploaded_documents`
- `read_uploaded_document`
- `ingest_uploaded_document`
- `get_upload_ingestion_status`

用户不需要提供服务器路径。设备资料经过本地 W7 抽取后追加租户快照；标准
资料可以先读取逐字原文，再调用治理 MCP 创建草案。

领域 MCP 已支持活动快照热刷新。新文件发布后，无需重启领域服务即可检索。

## 6. W7：教师-学生蒸馏与本地抽取模型

### 6.1 已完成数据工程

- CFDK 压缩机维修记录：6,191 条，CC BY-SA 3.0；
- Grundfos 中文泵手册；
- 天津供水泵站标准；
- Coox 离心泵跨域留出手册；
- 公开航空故障树格式数据；
- 教师补标和严格公开来源门禁；
- README、模型卡、数据 manifest、审核队列。

当前主训练集：

```text
B1: train 4,868 / val 623 / test 652
```

训练和验证按文档资产切分。Grundfos 8.x 固定进 test，天津标准固定进 val，
Coox 整份文档作为跨域留出集。

### 6.2 已完成微调

- 学生：Qwen2.5-1.5B-Instruct；
- 方法：QLoRA；
- 3 epochs，1,827 steps；
- 训练耗时约 3,484.88 秒；
- train loss 0.0465；
- eval loss 0.0418；
- LoRA adapter、checkpoint、merged Hugging Face 模型均已产出；
- 已接入 Nexent 上传导入 MCP 的本地 OpenAI 兼容端点。

### 6.3 已完成的 652 条测试集评测

| 指标 | 基座 | B1 |
|---|---:|---:|
| JSON 合法率 | 0.9678 | 1.0000 |
| Contract 合法率 | 0.0015 | 0.9801 |
| 实体 F1 | 0.1702 | 0.6841 |
| 关系 F1 | 0.0000 | 0.5212 |
| 逐字证据率 | 0.8136 | 0.9824 |
| 负样本准确率 | 0.0255 | 0.9745 |
| 平均输出 tokens | 337.90 | 92.13 |

分组结果：

| 数据组 | B1 实体 F1 | B1 关系 F1 | B1 证据率 | B1 Contract |
|---|---:|---:|---:|---:|
| CFDK 588 条 | 0.6325 | 0.1910 | 0.9875 | 0.9830 |
| Grundfos 8.x 64 条 | 0.9845 | 0.9767 | 0.9545 | 0.9531 |

该结果说明：

- B1 相比基座在格式、实体、因果、负样本和输出长度上均显著改善；
- 结构化中文泵手册域已达到接近饱和的实体/关系效果；
- CFDK 自由维修文本的关系抽取仍是当前跨域短板。

评测同时完成：

- 文档级泄漏检查；
- 27 条低质量样本审核队列；
- 错误类型统计；
- 机器可读 W7 audit 报告；
- 模型卡和评测报告。

## 7. W8：本体演化和治理闭环

已完成：

- 不可变本体版本库；
- 活动版本指针；
- 标准文本到变更草案；
- 每项操作逐字证据；
- 冲突检测；
- `draft → under_review → approved/rejected → published → rolled_back`
  状态机；
- 审核人、原因、时间和内容哈希审计；
- diff 和租户影响分析；
- 迁移 dry-run；
- 版本化 JSON Schema 生成与哈希；
- 多租户快照迁移；
- 原子发布；
- 回滚；
- 新旧快照保留；
- 租户快照历史查询。

治理 MCP 提供 12 个工具，覆盖只读查询、草案创建、审核、影响分析、发布、
回滚和快照历史。发布前必须经过 dry-run，真实发布和回滚需要显式
`confirm=true`。

实机验收已完成：

- 新本体版本发布；
- 8 个 Schema 产物生成；
- 新实体进入 `$defs`、`oneOf` 和 discriminator mapping；
- 回滚后活动版本恢复；
- 发布快照和回滚前快照同时保留。

## 8. 三 Agent 协同验收

已形成主 Agent 负责最终答复、证据 Agent 负责复核、本体 Agent 负责语义和
迁移建议的职责分离。

已实现：

- 主 Agent 的 `managed_agents` 关系持久化；
- Agent 发布版本包含子 Agent 关系；
- 工具最小权限；
- 子 Agent 不能互相递归；
- 子 Agent 不能发布或回滚；
- 子 Agent 失败时必须明确报告未完成；
- Nexent 实机 transcript 中已出现两个真实子 Agent 调用；
- 严格协议修复开关已持久化。

## 9. DSML 协议兼容

已完成独立兼容层：

- 识别全角和 ASCII DSML 标记；
- 支持标签内空白；
- 支持多个工具调用；
- 支持参数属性乱序和单引号；
- 支持 invoke 内直接 JSON 参数；
- 无安全标识符的调用不转换；
- 在 Nexent output protocol 和 CoreAgent 解析前执行规范化。

该兼容层以独立镜像和 compose override 部署，不直接修改项目内已有 MCP
或 Skill 源码。

## 10. W9：独立中文评测数据与确定性评测基础

为避免复用已参与 W7 模型选择和错误分析的测试集，已下载并冻结 6 份新的
中文公开泵技术资料。

### 10.1 新数据

- 科普中国；
- 北京海圣达泵业；
- 上海连成集团；
- 佛山肯富来水泵厂；
- 长沙水泵厂；
- 上海帕特泵业。

形成：

- 30 条独立中文案例；
- 26 条故障-原因案例；
- 4 条原因-处理措施案例；
- 所有 gold quote 均能在下载文本中逐字定位；
- 与 W7 数据 asset 重叠为 0；
- 来源 URL、文本 SHA-256、案例文件和 manifest 已固定。

下载脚本发现来源正文哈希变化时默认失败，避免评测集静默变化。

当前未调参来源检索基线：

```text
Source Hit@1  = 0.800
Source Hit@3  = 0.967
Source Hit@All = 1.000
MRR            = 0.886
```

### 10.2 确定性领域评测

`scripts/evaluate_domain.py` 已完成：

- 词法检索与图谱证据链对比；
- 设备位号、名称和负向查询；
- 故障检索；
- 实体、事实和规则证据追溯；
- 故障树结构与校验；
- 本体类/关系数量检查；
- 端到端诊断；
- 删除关键证据后必须降级为 `rejected`。

当前 11/11 场景通过，图谱路径相对纯词法路径在证据覆盖和故障树结构上
有可重复增益。

## 11. 自动化验证结果

截至 2026-11-15：

- `tests/domain`、`tests/nexent`、`tests/mcp`、`tests/skills`、
  `tests/governance_mcp`、`tests/ingestion_mcp`：**398 passed**；
- Ruff check 与 format check 通过；
- W9 新数据 manifest 构建通过；
- W9 下载源变更 fail-closed 测试通过；
- 领域、治理、上传导入和多 Agent 导入的离线测试均通过。

## 12. 已完成的关键用户流程

### 12.1 设备资料上传与诊断

1. 用户在 Nexent 上传 PDF/表格/图片/文本；
2. 主 Agent 调用上传 MCP 找到附件；
3. 本地解析并调用 W7 学生抽取；
4. 本体、证据和字段校验；
5. 追加租户快照；
6. 领域 MCP 热刷新；
7. 用户立即查询故障、证据和故障树；
8. 证据复核 Agent 给出独立 verdict。

### 12.2 标准驱动本体演化

1. 用户上传标准或粘贴节选；
2. Agent 获取逐字 source text；
3. 创建带证据的本体草案；
4. 两个辅助 Agent 联合复核；
5. 影响分析和迁移 dry-run；
6. 用户明确批准后发布；
7. 检查新版本 Schema；
8. 可回滚并保留新旧快照。

## 13. 关键代码与报告入口

| 内容 | 路径 |
|---|---|
| 项目工作流与验收 | `docs/project/WORKFLOWS.md` |
| 领域本体 | `domain_packs/industrial_fault/ontology.yml` |
| 只读领域 MCP | `mcp_server/` |
| 本体治理 MCP | `governance_mcp_server/` |
| 上传导入 MCP | `ingestion_mcp_server/` |
| 领域核心 | `lightrag/fault_tree/` |
| 三个 Skill | `skills/` |
| Nexent 配置与导入 | `nexent/` |
| W7 训练工作区 | `training/` |
| W7 评测报告 | `training/EVAL_REPORT.md` |
| W7 模型卡 | `training/MODEL_CARD.md` |
| W7 审计 | `training/artifacts/w7-audit.json` |
| 领域评测 | `eval/reports/industrial-domain-eval.json` |
| W9 新数据来源 | `eval/w9_sources.json` |
| W9 30 条案例 | `eval/datasets/w9_unseen_web_20261115.jsonl` |
| W9 数据 manifest | `eval/reports/w9-unseen-manifest.json` |
| W9 检索基线 | `eval/reports/w9-unseen-baseline.json` |

## 14. 当前成果结论

项目已经形成完整的工业故障领域技术底座、证据约束的诊断链路、Nexent
多智能体入口、用户文件导入、W7 本地蒸馏模型、W8 本体治理闭环，以及独立于
原有 W7 测试集的新中文评测数据和确定性评测框架。

现有成果可以作为项目阶段汇报、演示准备和后续开发基线使用。所有指标均以
对应报告中的测试集、样本数和评测口径为准，不把离线测试结果外推为未经
验证的通用生产能力。
