# Nexent 上传导入 MCP

这个服务补上“用户上传文件”到工业故障领域数据之间的缺口。Nexent 是唯一交互入口：

1. 用户在 Nexent 中上传 PDF、CSV、XLSX、PNG/JPEG、Markdown 或 TXT；
2. Agent 调用 `list_uploaded_documents` 找到附件；
3. 设备资料调用 `ingest_uploaded_document`，标准资料调用 `read_uploaded_document`；
4. 设备资料经本地 W7 学生模型抽取、逐字证据校验后，追加为新的不可变租户快照；
5. 领域 MCP 检测 `active.json` 变化并热刷新，用户下一条问题即可检索新数据。

现有 `mcp_server/`、`governance_mcp_server/` 和 `skills/` 的协议不需要修改。

## 工具

| 工具 | 写入 | 作用 |
| --- | --- | --- |
| `list_uploaded_documents` | 否 | 列出收件目录中的近期文件，返回稳定 `document_id` |
| `read_uploaded_document` | 否 | 本地解析并返回逐字 transcript、页码、章节和字符位置 |
| `ingest_uploaded_document` | 追加 | W7 抽取、校验、投影、追加快照并更新活动指针 |
| `get_upload_ingestion_status` | 否 | 查询文件成功/失败、新增数量和 `run_id` |

失败不会覆盖旧快照。相同文件重复导入生成相同内容寻址 `run_id`，不会制造重复版本。

## 启动

先启动本地 W7 学生模型，并通过兼容接口验证：

```bash
export W7_STUDENT_ENDPOINT=http://127.0.0.1:8000/v1
export W7_STUDENT_MODEL=industrial-extractor
python scripts/probe_w7_extractor.py
```

然后启动 MCP：

```bash
export INGESTION_DOMAIN_PACK=/opt/lightrag-domain/domain_packs/industrial_fault
export INGESTION_DOMAIN_STORAGE=/opt/lightrag-domain/domain_packs/industrial_fault/storage
export INGESTION_UPLOAD_DIRS=/opt/lightrag-domain/uploads/inbox:/var/lib/docker/volumes/nexent-agent-workspace/_data
python -m ingestion_mcp_server \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8767
```

`INGESTION_UPLOAD_DIRS` 使用操作系统路径分隔符。Linux 是 `:`，Windows 是 `;`。生产环境只需包含 Nexent 附件实际落盘的目录；扫描不会走出这些根目录。

## 用户流程

设备巡检表：

> 我上传了一份 P-101A 巡检表，请识别并导入，然后告诉我新增了哪些故障和证据。

Agent 先调用 `list_uploaded_documents`，再调用 `ingest_uploaded_document`，并把用户给出的设备位号作为 `equipment_ref`。学生模型只抽 `FaultEvent + CAUSES`，服务会基于现有租户设备记录补充有逐字证据的 `HAS_FAULT` 事实；找不到设备时不会伪造设备。成功答复应包含新增实体、事实、规则、证据数量、关联设备和新 `run_id`，随后可以立即询问设备故障。

标准文件：

> 我上传了一份标准 PDF，请读取原文，并根据“传感器安装”条款生成本体变更草案。

Agent 用 `read_uploaded_document` 返回的逐字 `source_text` 调用治理 MCP 的 `create_ontology_change_draft`。读取文件本身不会发布或改变活动本体。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `INGESTION_MCP_HOST` | `127.0.0.1` | MCP 监听地址 |
| `INGESTION_MCP_PORT` | `8767` | MCP 端口 |
| `INGESTION_DOMAIN_PACK` | 仓库工业领域包 | `ontology.yml` 所在目录 |
| `INGESTION_DOMAIN_STORAGE` | 领域包 `storage/` | 租户快照目录 |
| `INGESTION_UPLOAD_DIRS` | 仓库收件箱与 Nexent workspace | 允许扫描的上传根目录 |
| `W7_STUDENT_ENDPOINT` | 空 | 本地 OpenAI 兼容 `/v1` 端点 |
| `W7_STUDENT_MODEL` | 空 | 合并学生模型名称 |
| `W7_STUDENT_MAX_ATTEMPTS` | `2` | 合同失败时的本模型重试次数 |
| `DOMAIN_VLM_ENDPOINT` | 空 | PNG/JPEG 的本地 VLM |

没有配置 W7 时，标准文件仍可被只读解析，但设备资料导入会明确返回 `extractor_not_configured`，不会悄悄改用 DeepSeek。

## 安全边界

- 只扫描显式配置的上传根目录，不支持任意服务器路径。
- 文件引用的 `document_id` 由路径、大小和修改时间派生，助手不能通过路径逃逸收件箱。
- 上传原件与解析文本保存在领域包的 `.ingestion/` 下，已被 Git 忽略。
- W7 只生成候选；现有投影器继续执行本体约束和逐字 quote 校验。
- 导入是新增快照并原子切换活动指针，不删除旧快照。
