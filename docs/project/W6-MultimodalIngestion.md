# W6：多模态文档解析与证据定位

状态：✅ 已完成并验收（2026-11-12）

W6 把 PDF、表格、图片变成“带页码/版面/字符区间的证据”，并保证原始数据不出本地、
失败文件有明确状态和错误原因、失败可以重试。本文记录解析路由、定位语义、契约变化、
隐私边界和已知边界；可执行命令见文末。

## 为什么是独立的领域前置层

服务器侧的 `lightrag/pipeline.py::analyze_multimodal` 依赖完整 `LightRAG` 实例、
MinerU/Docling sidecar 和网络服务，无法离线、确定性地验收。W6 因此新增
`lightrag/fault_tree/multimodal.py`：它是一个**领域前置层**，只负责

```
原始文件 → 解析文本 → SourceChunk（带定位） → 抽取 fixture → SeedWorkspace
```

`SeedWorkspace` 之后直接进入 W3 已有的投影、校验、故障树与图谱写入链路，不重写
任何投影逻辑。MinerU/Docling 仍是服务器侧扩展路径：本层遇到无文本层 PDF 时明确
失败并提示走那条路，而不是偷偷降级。

## 解析路由

| 模态 | 解析器 | 依赖 | `parse_engine` | chunk id | 定位字段 |
| --- | --- | --- | --- | --- | --- |
| `pdf` | `parse_pdf_pages` | `pypdf`（惰性导入） | `legacy` | `{asset_id}:p{n}` | `page=n`；`char_base`=页首在转写文本中的偏移 |
| `table` (CSV) | `parse_table_rows` | 标准库 `csv` | `native` | `{asset_id}:row{n}` | `page=None`；`section_path="row{n}"` |
| `table` (XLSX) | `parse_table_rows` | `openpyxl`（惰性导入） | `native` | `{asset_id}:{sheet}:row{n}` | `page=None`；`section_path="{sheet}!row{n}"` |
| `image` | `parse_image` | 本地 OpenAI 兼容 VLM 或转写 fixture | `native` | `{asset_id}:image` | `page=1`；`section_path="image"` |

惰性导入意味着缺少 `pypdf`/`openpyxl` 时核心包仍可导入，解析时才报
`parser-dependency-missing`，并提示安装 `api` extra。

统一约定：

- `page` 从 1 开始；PDF 空白页不产出 chunk，但仍在转写文本中占一个换行，
  因此后续页面的 `char_base` 仍是**整个转写文本**的绝对偏移。
- 表格行号从 1 开始并包含表头；首行默认作为表头，行文本形如
  `设备编号=P-101A | 检查项目=出口压力 | ...`，表头为空时回退 `columnN`。
- CSV 编码优先 `utf-8-sig`，失败回退 `gb18030`，两者都不行报 `table-encoding`。
- 所有 `SourceChunk.text` 都是解析后文本的连续片段；`char_base` 是该片段在
  `text_uri` 文件中的起始偏移。

## 证据定位语义

- 抽取 fixture 里的 `quote` 必须落在**同一个 chunk** 的 `text` 内。摄入时
  `_load_extractions` 先做一次 `quote in chunk.text` 检查（`quote-not-verbatim`），
  校验器 `_validate_evidence_source` 再对整份转写文本做一次（保留原有错误码）。
- 投影器按 `chunk.char_base + local_start` 计算 `char_start`/`char_end`，所以
  证据跨度始终是**源文档转写文本**的绝对区间，而不是 chunk 内相对位置。
  `span_matches()` 同时接受这两种坐标，历史 seed 数据不受影响。
- `cause_rule` 的 `event_local_id` 只在**同一个 chunk** 内解析
  （`local_to_uid[(chunk_id, local_id)]`）。跨页 OR 规则必须把原因事件放到
  同一页或同一行的抽取 fixture 里；这不是 W6 的缺陷，而是投影器的规则作用域。
- PDF 只给页级定位，不推断标题层级或 bbox：`section_path=None`。版面结构留给
  MinerU/Docling。

## `FileAsset.text_uri`：原始二进制与可引用文本分离

W6 之前 `FileAsset.object_uri` 同时承担两个含义：“原始文件”和“可被 quote 校验的
文本”。这对 UTF-8 的 `.md` 成立，对 PDF/PNG/XLSX 不成立——校验器会把二进制当
UTF-8 读，直接抛解码异常。

契约新增可选字段：

| 字段 | 指向 | 用途 |
| --- | --- | --- |
| `object_uri` / `checksum` / `file_size` | 原始二进制 | 出处、完整性；checksum 与大小只对原始文件计算 |
| `text_uri`（可选） | 解析出的 UTF-8 转写文本 | quote 子串与 char span 校验 |

校验规则（`_validate_evidence_source`）：

- 判断依据是**文件格式**，不是 `modality`：Markdown/CSV/TXT 等文本扩展名直接作为
  可校验文本（演示包里的巡检表就是 `modality=table` 的 `.md` 文件）；PDF、图片、
  XLSX 等二进制格式没有 `text_uri` → `evidence-text-artifact-missing`（error，
  fail closed）。
- `text_uri` 是远端 URI → `evidence-source-remote`（warning，无法离线校验）。
- 转写文本不是合法 UTF-8 → `evidence-source-not-text`（error，并提示设置 `text_uri`）。
- `object_uri` 是远端 → `asset-checksum-unverified`（warning）；本地则照常校验
  checksum，大小不一致仍为 warning。

向后兼容：`text_uri` 可选，未升 `SCHEMA_VERSION`；既有纯文本 seed 的
`object_uri` 仍被当作可校验文本。JSON Schema 需用
`python scripts/export_domain_schemas.py` 重新生成。

## 隐私边界

- 默认离线：PDF/表格本地解析，图片使用 `seeds/multimodal/extraction/image_transcript.json`
  的转写 fixture，不发起任何网络请求。
- 真实图片识别只允许本地 OpenAI 兼容端点。`ensure_local_endpoint` 接受
  `localhost`/`*.localhost`、回环、私有、链路本地和未指定地址；主机名先
  `getaddrinfo` 再检查**所有**结果，防止域名指向公网。
- 唯一例外是显式放行：`DOMAIN_ALLOW_EXTERNAL_VLM=1` 或 `--allow-external-vlm`，
  并要求在评审记录中写明原因。原始图片和转写文本默认不出本地。
- 转写文本写入 `seeds/multimodal/generated/text/`，与原始二进制分开；两者都在
  `.gitignore` 覆盖范围内，只有合成样例的生成脚本入库。

## 失败状态与重试

每份文档的处理结果写入 `DocumentStatusStore`（默认
`domain_packs/industrial_fault/storage/multimodal_status.json`，版本化 JSON，
`tempfile.mkstemp` + `os.replace` 原子写）。单文档失败**不会中断整批**，而是记录：

```
asset_id / status("pending"|"parsed"|"failed") / attempts /
error_code / error_message / page_count / chunk_count / text_uri / updated_at
```

损坏的状态文件报 `status-store-corrupt`，不会静默清空。CLI 在存在失败且未加
`--allow-failures` 时打印全部状态并以退出码 2 结束，且不写入 bundle——失败必须
可见，不能“看起来导入成功”。

`--retry-failed` 只重跑上次 `failed` 的文档：图片直接复用 `text_uri` 指向的转写
文本（缺失报 `parsed-artifact-missing`），PDF/表格在本地重新解析（无网络成本）。
已解析文档不会因为重试而重复计数。

常见失败码：`source-missing`、`pdf-no-text-layer`、`pdf-empty`、`table-empty`、
`table-encoding`、`parser-dependency-missing`、`vlm-not-configured`、
`vlm-endpoint-not-local`、`vlm-request-failed`、`vlm-empty-output`、
`extraction-missing`、`extraction-chunk-mismatch`、`quote-not-verbatim`、
`parsed-artifact-missing`。完整表格见
`domain_packs/industrial_fault/seeds/multimodal/README.md`。

## 样例与命令

样例全部为合成数据，位于 `domain_packs/industrial_fault/seeds/multimodal/`：

- `FILE-P101A-PDF`：英文两页 PDF。第 1 页含“过滤器堵塞/汽蚀 → 出口压力低”的 OR
  规则；第 2 页是维修措施负样本（不应产生证据）。
- `FILE-P101A-INSPECTION-CSV` / `FILE-P101A-INSPECTION-XLSX`：同一份中文巡检表，
  CSV 与 XLSX（`inspection`、`equipment` 两个 sheet）各导入一次；第 2 行给出
  过滤器压差 `0.08 MPa`，第 3 行给出出口压力 `0.32 MPa`、下限 `0.45 MPa` 与
  “入口过滤器压差偏高”。
- `FILE-P101A-NAMEPLATE`：ASCII 铭牌与故障挂牌 PNG，离线验收使用 fixture 转写。

```bash
# 1. 生成合成样例（PDF/CSV/XLSX/PNG）
python scripts/make_multimodal_samples.py

# 2. 契约新增字段后重新生成 schema
python scripts/export_domain_schemas.py

# 3. 导入 + 逐字证据校验 + 重复导入幂等检查（默认 fixture，离线）
python scripts/import_domain_multimodal.py --verify

# 4. 镜像目录测试
./scripts/test.sh tests/domain
```

预期：4 份文档 `parsed`；`verify_evidence` 通过且 `rules=1`；第二次导入的
`run_id` 与各项计数不变；`--verify` 确认 `trace_evidence` 使用目标索引、MCP 存储
仓库不预加载 quote，按需加载数 `quotes_loaded >= 1`。

**验收结果（2026-11-12）**：4 份文档 `parsed`；8 个实体、15 条证据、33 条证据目标、
3 条事实、1 条规则；故障树 5 节点/4 边；图谱 10 节点/7 边；`run_id`
`run-0fc559390d7e` 在重复导入中保持不变；`tests/domain` 56 passed。用户已确认验收。

## W6 边界（明确没做）

- **不做扫描件 OCR**：无文本层 PDF 直接失败并提示 MinerU/Docling；OCR 属于服务器
  侧扩展，不在离线验收范围。
- **不推断版面/bbox**：只有页码、行号/工作表名和字符区间；标题层级、表格合并
  单元格语义不解析（合并单元格按左上值读取）。
- **不做真实抽取**：抽取 fixture 是人工 curated 的候选，证明链路；用本地模型从
  转写文本抽取实体/关系属于 W7（教师-学生与主动学习）。
- **不追求真实 VLM 识别质量**：验收默认用 fixture；真实 VLM 只验证“请求确实发往
  本地”。模型效果评测属于 W9。
- **不接入服务器上传流水线**：`/file/process`、MinerU/Docling sidecar 的行为不变；
  两条路径后续在 W8/W10 按同一契约合并。
