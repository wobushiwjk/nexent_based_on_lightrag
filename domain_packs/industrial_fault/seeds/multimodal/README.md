# 多模态种子样例（W6）

**本目录中的 PDF、CSV、XLSX、PNG 全部由 `scripts/make_multimodal_samples.py`
合成，不是真实客户资料、真实手册或真实铭牌照片。** 设备编号 `P-101A`、压力数值
和维修措施均为演示用途的虚构数据。

这些样例用来证明 W6 的验收链路：PDF / 表格 / 图片三种模态各至少一份导入成功，
证据 quote 逐字校验通过，原始二进制不出本地，失败文件有明确状态和错误原因。

## 文件

| 路径 | 性质 | 作用 |
| --- | --- | --- |
| `manifest.json` | 手写清单 | 4 份文档的 asset_id、路径、模态、抽取 fixture、顶层事件与 VLM 配置 |
| `extraction/pdf_manual.json` | 人工抽取 fixture | PDF 第 1 页的实体、关系与 OR 因果规则 |
| `extraction/table_row3.json` | 人工抽取 fixture | CSV 第 3 行的过滤器与出口压力证据 |
| `extraction/xlsx_row3.json` | 人工抽取 fixture | XLSX `inspection` sheet 第 3 行的同一组证据 |
| `extraction/image_nameplate.json` | 人工抽取 fixture | 铭牌与故障挂牌转写文本中的设备与故障事件 |
| `extraction/image_transcript.json` | VLM 转写 fixture | 离线验收时替代真实 VLM 的逐字转写 |
| `generated/*` | 生成的二进制 | PDF/CSV/XLSX/PNG 与解析文本，已被 `.gitignore` 忽略 |

`generated/` 不在版本库里。首次使用、或修改了 `scripts/make_multimodal_samples.py`
之后，必须重新生成：

```bash
python scripts/make_multimodal_samples.py
```

## 导入与验收

默认流程完全离线：PDF/表格在本地解析，图片使用 `image_transcript.json` 中的
fixture 转写，不发起任何网络请求。

```bash
# 生成样例
python scripts/make_multimodal_samples.py

# 导入 + 证据逐字校验 + 重复导入幂等检查
python scripts/import_domain_multimodal.py --verify
```

预期结果：

- 4 份文档状态均为 `parsed`；
- `verify_evidence` 通过，`rules` 为 1（`RULE-MM-LOW-PRESSURE-OR`）；
- 第二次导入的 `run_id`、实体/证据/事实/规则计数与第一次一致；
- `--verify` 额外确认 `trace_evidence` 使用目标索引，且 MCP 存储仓库不预加载 quote。

## 使用本地 VLM 识别图片

图片文档需要一个本地部署的 OpenAI 兼容 VLM。默认 manifest 指向
`http://localhost:11434/v1/chat/completions`（例如 Ollama 上的 `qwen2.5vl:3b`）。
显式指定端点时：

```bash
python scripts/import_domain_multimodal.py \
    --vlm-endpoint http://127.0.0.1:8000/v1 \
    --vlm-model Qwen/Qwen2.5-VL-3B-Instruct \
    --verify
```

隐私守卫 `ensure_local_endpoint` 只允许 localhost、回环地址、私有网段和链路本地地址；
主机名会先解析，再检查**所有**解析结果，防止用域名指向公网。确需调用外部 VLM 时，
必须同时满足以下之一，并在评审记录中写明原因：

```bash
DOMAIN_ALLOW_EXTERNAL_VLM=1 python scripts/import_domain_multimodal.py --vlm-endpoint ...
# 或
python scripts/import_domain_multimodal.py --allow-external-vlm --vlm-endpoint ...
```

fixture 与真实 VLM 的区别：fixture 证明“解析 → 证据 → 图谱 → MCP 读取”链路和
逐字校验；真实 VLM 只额外证明“请求确实发往本地模型”。真实识别质量属于 W7
本地抽取模型的范围，不是 W6 的验收项。

## 失败状态与重试

每份文档的处理状态记录在 `domain_packs/industrial_fault/storage/multimodal_status.json`
（`--status-file` 可覆盖）。失败不会静默跳过：状态为 `failed`，并带
`error_code` / `error_message` / `attempts`。

常见失败码：

| 错误码 | 含义 | 处理 |
| --- | --- | --- |
| `source-missing` | 原始文件不存在 | 重新生成样例或修正 manifest 路径 |
| `pdf-no-text-layer` | PDF 无文本层（扫描件） | 走服务器侧 MinerU/Docling OCR，或换带文本层的 PDF |
| `pdf-empty` / `table-empty` | 文档没有可用内容 | 检查源文件 |
| `table-encoding` | CSV 既不是 UTF-8 也不是 GB18030 | 转换编码 |
| `parser-dependency-missing` | 缺少 pypdf/openpyxl | `uv sync --extra api` |
| `vlm-not-configured` | 图片没有 VLM 端点或 fixture | 配置本地端点或 `--vlm-fixture` |
| `vlm-endpoint-not-local` | 端点不在本机或私有网段 | 换本地端点；确需外发时显式放行 |
| `vlm-empty-output` | VLM 没有返回可见文字 | 检查图片与提示词，或换模型 |
| `extraction-missing` / `extraction-chunk-mismatch` | fixture 缺失或引用了本文档未产出的 chunk | 修正 fixture 的 `chunk_id` |
| `quote-not-verbatim` | fixture quote 不是解析文本的连续片段 | 按解析文本修正 quote |
| `parsed-artifact-missing` | 状态标记已解析但转写文本不存在 | 删除该条状态后重跑 |

重试只重跑上次 `failed` 的文档；已解析图片直接复用 `text_uri` 指向的转写文本，
PDF/表格在本地重新解析（无网络成本）：

```bash
python scripts/import_domain_multimodal.py --retry-failed
```

有失败且未加 `--allow-failures` 时，CLI 打印每份文档的状态后以退出码 `2` 结束，
不会写入 bundle。需要导入已解析的子集时才使用 `--allow-failures`。

## 修改样例时的连锁影响

- `FileAsset.checksum` / `file_size` 绑定原始二进制；重新生成或编辑任何样例后，
  已导入的存储必须重新导入（`--retry-failed` 不会刷新已解析 PDF/表格的 checksum，
  因为它只重跑失败项）。
- 抽取 fixture 的 `quote` 必须仍是**解析后文本**的逐字连续片段。PDF 的解析文本
  来自 pypdf 逐页重建，改动手写行距或字号可能改变换行，从而让 quote 失效。
- 因果规则的 `cause_rule` 只能引用**同一个 chunk** 内的 `local_id`（投影器按
  chunk 解析局部 ID），跨页 OR 规则必须把两个原因放进同一页或同一张表的同一行。
- 修改 `manifest.json` 的 asset_id 会改变转写文件名、证据 ID 和 chunk_id；请同步
  更新 `extraction/` 下的 fixture 与 `image_transcript.json`。
