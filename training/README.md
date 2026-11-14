# W7：教师-学生蒸馏与主动学习

本目录是 W7 的正式训练工作区。目标是把 W6 产出的文本块变成**可离线运行的
抽取模型**：教师模型（`deepseek-flash`，外部 API）只读公开文本并生成标签；
学生模型（`Qwen2.5-1.5B-Instruct`）在本地 QLoRA 微调，量化成 GGUF 后部署到
香橙派上的 Ollama。

> 这里的权重、语料、标签、队列都不进 Git。`training/corpus/sources.yml`、
> 配置和文档才进 Git。

## 目录

```
training/
  README.md
  requirements-train.txt      # 训练机依赖（不进 LightRAG 运行时）
  .env.example                # 教师 API 配置模板；真实 .env 被 gitignore
  corpus/
    sources.yml               # 数据来源、许可证、是否允许外发
    manual_sources.yml        # 结构化中文手册的页码/版面解析规格
    raw/                      # 原始文档；gitignore
    parsed/                   # 本地解析后的 chunks.jsonl；gitignore
  data/                       # 教师标签、训练集、复核队列；gitignore
    cfdk_a/                   # 实验 A：只用 CFDK
    w7_b/                     # 实验 B：CFDK + 中文手册（stage 1）
    w7_b_stage2/              # 实验 B 的域适配重配比（stage 2）
  models/                     # ModelScope 下载的基座模型；gitignore
  saves/                      # LoRA / merged 权重；gitignore
  artifacts/                  # GGUF + manifest；gitignore
  configs/
    dataset_info.json
    train_qlora_qwen2.5-1.5b.yaml
    train_qlora_stage2.yaml
    merge_lora_qwen2.5-1.5b.yaml
    merge_lora_stage2.yaml
    Modelfile
  MODEL_CARD.md               # 训练后填数字
  EVAL_REPORT.md              # 训练后填数字
```

## 本地机器与训练机的分工

解析、教师标注、数据集构建都在本地 CPU 上完成；训练机只负责 QLoRA、
合并和 GGUF 转换。教师 key 和原始文档因此不需要上训练机。

| 环节 | 机器 | GPU | 产物位置 |
| --- | --- | --- | --- |
| PDF/ZIP 解析、切块 | 本地 | 不需要 | `training/corpus/parsed/` |
| 教师标注 | 本地 | 不需要 | `training/data/teacher_labels.jsonl` |
| 数据集构建/切分 | 本地 | 不需要 | `training/data/{cfdk_a,w7_b,w7_b_stage2}/` |
| 基座模型下载 | 训练机 | 不需要 | `training/models/` |
| QLoRA 训练 | 训练机 | 需要 | `training/saves/` |
| 合并 / GGUF / 量化 | 训练机 | CPU 可 | `training/artifacts/` |
| Ollama 推理 | 香橙派 | 不需要 | `/opt/models/industrial-extractor/` |

实际用到的原始文件（其余下载是调研时留下的，不必传）：

| 文件 | 大小 | 用途 |
| --- | --- | --- |
| `training/corpus/raw/cfdk/CFDK_dataset_BioNLP11_shared_task_standoff_format.zip` | 1.7M | CFDK 专家标注 |
| `training/corpus/raw/cn-pump-manuals/grundfos_6859942.pdf` | 26M | Grundfos NK 故障表 |
| `training/corpus/raw/cn-pump-manuals-2/grundfos-5564721/manual.pdf` | 5.1M | Grundfos S 故障表 |
| `training/corpus/raw/cn-pump-manuals-2/grundfos-6859100/manual.pdf` | 1.4M | Grundfos 8.x 故障表（test） |
| `training/corpus/raw/cn-pump-manuals/tianjin_standard.pdf` | 772K | 天津标准（val） |
| `training/corpus/raw/cn-pump-manuals/coox.pdf` | 244K | Coox 留出手册 |

训练机其实只需要 `training/data/`（约 47M）和 `training/configs/`；
`training/data/` 里每个目录都自带 `manifest.json`，训练不读原始 PDF。
如果要让训练机也能从原始文件重新生成，再额外传上面六个原始文件（约 35M）、
`training/corpus/parsed/`（3.7M）和 `training/corpus/{sources,manual_sources}.yml`，
然后在训练机重跑两个 ingest 脚本。

```bash
# 最小传输：数据 + 配置（配置目前尚未提交时也要一起传）
rsync -av training/data/ trainer:/workspace/LightRAG/training/data/
rsync -av training/configs/ trainer:/workspace/LightRAG/training/configs/

# 不要传：training/.env（教师 key）、training/models/（在训练机下载）、
#         training/saves/、training/artifacts/
```

`manifest.json` 里记录的 chunk/label 路径是构建机上的路径，只用于审计；
训练只按 `dataset_info.json` 的相对路径读 `training/data/*.jsonl`。

## 任务定义

输入：一个文本片段（手册、维修记录、巡检表行、PDF 页）。

输出：一个 JSON 对象，只包含 `FaultEvent` 和 `CAUSES`：

```json
{
  "entities": [
    {
      "local_id": "filter",
      "class": "FaultEvent",
      "name": "入口过滤器堵塞",
      "description": "过滤器流通面积减小，泵入口流量下降",
      "cause_rule": {"op": "OR", "children": [{"event_local_id": "a"}, {"event_local_id": "b"}]},
      "evidence": [{"quote": "入口过滤器堵塞会降低泵的入口流量"}]
    }
  ],
  "relations": [
    {
      "relation_code": "CAUSES",
      "source_local_id": "filter",
      "target_local_id": "lp",
      "evidence": [{"quote": "入口过滤器堵塞会降低泵的入口流量"}]
    }
  ]
}
```

硬约束：

1. 只输出 JSON，不输出 Markdown、解释或额外文字。
2. 每个实体、每条关系都必须带 `evidence.quote`。
3. `quote` 必须是输入文本里的**逐字连续片段**。
4. 维修措施、检测动作、操作步骤不是故障原因，不抽成 `FaultEvent`。
5. 模型不输出 UID、页码、chunk ID、审核状态；这些由投影器补齐。

## 数据现状

| 来源 | 规模 | 用途 | 备注 |
| --- | --- | --- | --- |
| CFDK 压缩机故障诊断知识 | 6,191 条记录 → 5,967 chunk / 4,410 标签 | 跨域主数据 | CC BY-SA 3.0；专家标注 故障/原因，无关系标注；脚本按单故障记录保守生成 `CAUSES` |
| Grundfos NK/NKE 服务手册 | 7 故障 / 40 原因 | 泵域训练 | 公开中文手册，a)–j) 编号故障表 |
| Grundfos S 泵手册 | 7 故障 / 31 原因 | 泵域训练 | 与 NK 不同泵型，补充原因表述 |
| Grundfos 8.x 安装手册 | 10 故障 / 64 原因 | 泵域测试 | 整份文档强制进 test |
| Tianjin 供水泵站标准 | 7 故障 / 45 原因 | 泵域验证 | 整份文档强制进 val |
| Coox 离心泵手册 | 11 故障 / 53 原因 | 跨域留出 | `holdout: true`，不进入训练，只做评测和错误分析 |
| `训练数据集.json` | 101 条 → 97 条 | 格式/证据 bootstrap | 航空维修域，只做 smoke test，不是泵域评测集 |
| `domain_packs/.../seeds/documents/` | 3 份短文档 | 管线 smoke test | 合成样例，规模太小 |

CFDK 的 4,410 条 bootstrap 标签 = 3,561 条只有实体（多故障或原因无故障
可定向）、449 条带 `CAUSES`、400 条严格负样本；其余 1,557 条记录已由教师
补标：1,553 条成功（其中 1,332 条严格负样本），4 条因教师反复返回空内容
保持未标注。加上 bootstrap 的 400 条，总负样本为 1,732 / 6,143 = 28.2%。

先转换已有的 101 条公开数据：

```bash
python scripts/import_public_fault_dataset.py
# 产物：
#   training/corpus/parsed/chunks.jsonl
#   training/data/public_labels.jsonl
#   training/data/public_conversion_report.json
```

转换器会做保守修复：quote 不是逐字的节点会被丢弃；中间节点可以借用已保留
子节点的 quote，并在报告里标 `evidence-from-child`。先看报告里的
`samples_converted`、`dropped_nodes` 和 `sample_warnings`，再决定是否直接
拿来训练。**它只能证明管线通，不能证明泵域效果。**

CFDK 和中文手册各有自己的入库脚本；两者都不执行下载目录里的任何代码，
只读取 PDF/ZIP：

```bash
# CFDK：Standoff 标注 -> chunk + 保守标签
python scripts/ingest_cfdk.py
#   training/corpus/parsed/cfdk_chunks.jsonl
#   training/data/cfdk_bootstrap_labels.jsonl
#   training/data/cfdk_report.json

# 中文手册：按 x 坐标读故障表 -> 一原因一 chunk
python scripts/ingest_pump_manuals.py
#   training/corpus/parsed/manual_chunks.jsonl
#   training/data/manual_bootstrap_labels.jsonl
#   training/data/manual_report.json
#   training/data/manual_holdout_{chunks,labels}.jsonl   # Coox 留出集
```

手册解析依赖 `pdfplumber`（`training/requirements-train.txt`），但在仓库的
LightRAG 环境里运行即可，不需要 GPU。页码和版面在
`training/corpus/manual_sources.yml` 里逐份声明；加新手册时优先加一条规格，
而不是改脚本。`manual_report.json` 必须保持 `invalid_labels: 0`。

## 教师标注（deepseek-flash）

1. 把教师端点、模型名、key 写进 `training/.env`（从 `.env.example` 复制）：
   ```bash
   cp training/.env.example training/.env
   # 编辑 W7_TEACHER_ENDPOINT / W7_TEACHER_MODEL / W7_TEACHER_API_KEY
   ```
   `W7_TEACHER_MODEL` 用 `deepseek-flash`。如果你的网关用的不是这个名字，
   改成网关实际暴露的模型名；端点和 key 不写进任何提交文件。
2. 只有 `sources.yml` 里 `public: true` 的来源才允许外发。请求只带
   chunk 文本、任务提示、模态/页码，不带路径、租户、客户信息。
3. 先空跑。`--chunks` 可重复，用来一次合并 CFDK 和手册两份清单；
   `--skip-label-file` 也接受多条，把已有 bootstrap 标签的 chunk 排除，
   避免重复付费。当前应显示 `1557 public / 0 blocked / 4590 skipped`：
   ```bash
   python scripts/label_with_teacher.py \
     --chunks training/corpus/parsed/cfdk_chunks.jsonl \
     --chunks training/corpus/parsed/manual_chunks.jsonl \
     --skip-label-file training/data/cfdk_bootstrap_labels.jsonl \
     --skip-label-file training/data/manual_bootstrap_labels.jsonl \
     --dry-run
   # 计划写到 training/data/teacher_plan.json
   ```
4. 正式标注。默认断点续跑：已在 `teacher_labels.jsonl` 里的 chunk 会跳过；
   `--overwrite` 会重新标注（追加行，构建器取最后一条），并忽略 skip 文件。
   ```bash
   # 用上面同样的 --chunks/--skip-label-file，先小批量确认质量：
   python scripts/label_with_teacher.py ... --max-chunks 20
   # 再放开（分 200 条一批便于观察失败率）：
   python scripts/label_with_teacher.py ... --max-chunks 200
   ```
   网关不支持 `response_format` 时加 `--no-json-mode`，或在 `training/.env`
   设 `W7_TEACHER_JSON_MODE=false`。
5. 产物（都在 `training/data/`，gitignored）：
   - `teacher_labels.jsonl`：通过本地校验的 `FaultEvent + CAUSES` 标签。
   - `teacher_failures.jsonl`：JSON/schema/quote 校验失败，带原始输出；
     后续由主动学习脚本并入复核队列，不直接进训练集。
   - `teacher_blocked.jsonl`：被 `public: true` 门禁拦下的 chunk，从未外发。
   - `teacher_export_log.jsonl`：每次请求的 chunk_id、来源、许可证、文本
     SHA256、模型、时间、prompt/response 哈希和 token 用量。

导出日志只记录 chunk 文本的 SHA256，不记录原文；请求本身只带 chunk 文本、
任务提示、模态/页码，不带路径、租户、客户信息。教师输出在本地经过
`normalize_teacher_target` 收窄和 `validate_student_target` 校验：
未落地的 quote、悬空关系、单子节点 AND/OR 都会被修复或丢弃；如果教师
点名了故障事件但全部被丢弃，该样本记为失败，不会当成负样本。

> 教师 CLI、数据集构建器与转换器共用 `lightrag/fault_tree/distillation/`
> 下的提示词和校验逻辑，避免训练提示与运行时提示漂移。教师脚本依赖
> `httpx` 和 `lightrag` 包，建议在仓库的 LightRAG 环境里运行；训练在
> 独立的训练环境中运行。

## 数据集构建

三个实验目录都由同一个构建器生成，差异只在输入、强制切分和训练配比：

| 实验 | 输入 | 训练 / 验证 / 测试 | 用途 |
| --- | --- | --- | --- |
| A `cfdk_a` | CFDK | 4,797 / 578 / 588 | 单域 baseline，与 B 共用同一批 CFDK 评测文档 |
| B stage 1 `w7_b` | CFDK + 手册 | 4,868 / 623 / 652 | 主实验；手册占 1.46% |
| B stage 2 `w7_b_stage2` | 同 B，重配比 | 1,555（1,271 唯一）/ 623 / 652 | 目标域适配；手册占 22.8% |

标签文件按**从低到高优先级**传入：bootstrap 在前，教师标签在后，人工复核
最后。同一 chunk 上后给的文件覆盖先给的，但只在后者的 target 通过校验时
才覆盖。注意 `--labels` 一旦显式给出就会**替代**默认列表，所以每条命令都要
把现有的标签文件写全；教师标签文件为空或不存在时先不要加这一行。

A（只用 CFDK；先构建 B，再把 B 的 asset 切分钉给 A，使两者的 CFDK
评测文档完全一致）：

```bash
python scripts/build_distillation_dataset.py \
  --chunks training/corpus/parsed/cfdk_chunks.jsonl \
  --labels cfdk=training/data/cfdk_bootstrap_labels.jsonl \
  --labels teacher=training/data/teacher_labels.jsonl \
  --pin-from-manifest training/data/w7_b/manifest.json \
  --out-dir training/data/cfdk_a
```

`--pin-from-manifest` 只复用当前 chunk 文件里存在的 asset；B 里的手册
asset 在 A 中不存在，会被忽略。A 的 3,514/445/451 正好等于 B 的 CFDK 部分。

B stage 1（CFDK + 手册，强制把两份手册留作目标域验证/测试）：

```bash
python scripts/build_distillation_dataset.py \
  --chunks training/corpus/parsed/cfdk_chunks.jsonl \
  --chunks training/corpus/parsed/manual_chunks.jsonl \
  --labels cfdk=training/data/cfdk_bootstrap_labels.jsonl \
  --labels manual=training/data/manual_bootstrap_labels.jsonl \
  --labels teacher=training/data/teacher_labels.jsonl \
  --force-split tianjin-water-standard=val \
  --force-split grundfos-6859100=test \
  --out-dir training/data/w7_b
```

**为什么强制切分**：随机 80/10/10 按文档切时，4 份训练手册只占 251 个
asset 中的 4 个，实测全部落进了 train，val/test 变成纯 CFDK。把天津标准
钉在 val、把 Grundfos 8.x 钉在 test，才能保证目标域指标不是“碰运气”。
Coox 手册是独立的跨域留出集，只用于错误分析。

**手册配比问题**：stage 1 只有 71/4,868 = 1.46% 手册行，远低于计划的
20–30%。当前只有 180 条唯一手册 chunk，靠增加重复也变不出新文本；直接在
单次训练里复制 71 条会过拟合。因此 stage 2 从 stage-1 adapter 继续训练，
用“手册 ×5 + CFDK 1200 条 replay”把手册行提到 355/1,555 = 22.8%：

```bash
python scripts/build_distillation_dataset.py \
  --chunks training/corpus/parsed/cfdk_chunks.jsonl \
  --chunks training/corpus/parsed/manual_chunks.jsonl \
  --labels cfdk=training/data/cfdk_bootstrap_labels.jsonl \
  --labels manual=training/data/manual_bootstrap_labels.jsonl \
  --labels teacher=training/data/teacher_labels.jsonl \
  --pin-from-manifest training/data/w7_b/manifest.json \
  --repeat-source grundfos-nk-6859942=5 \
  --repeat-source grundfos-s-5564721=5 \
  --cap-source cfdk-compressor-maintenance=1200 \
  --out-dir training/data/w7_b_stage2
```

`--repeat-source` 复制训练行；`--cap-source` 用 `--seed` 确定性抽样训练行；
val/test 永不被重配比。manifest 记录 `train_mix`、`train_unique` 和逐来源
行数。教师 pass 后全量 B 的负样本为 1,732/6,143 = **28.2%**（bootstrap
400 + 教师 1,332）；stage 2 因 CFDK 被抽样，总负样本降到 668/2,830 =
23.6%。实际训练集里的负样本比例以 manifest 的 `train_mix` 和逐来源计数
为准，模型卡按实测数字写，不要沿用设计阶段的 20%。

构建器会逐条校验 schema、`FaultEvent + CAUSES`、`cause_rule` 引用和逐字
quote；不合规标签跳过并写进 `manifest.json` 的 `errors`，`--strict` 则
直接失败。切分按 `asset_id`（文档）而不是按 chunk，固定 `--seed`，避免
同一文档同时出现在训练和验证集。`--labels origin=path` 可以覆盖默认文件
列表；后给的文件优先，复核标签因此覆盖教师标签。默认顺序是
复核 > 教师 > 公开 bootstrap；如果同一 chunk 上你认为公开标签更可信，
用 `--labels` 调整顺序即可。

## 训练

训练机需要 NVIDIA GPU ≥12GB（或单卡昇腾 910B）；1.5B 在 GPU 上
QLoRA 约 6–8GB，在 NPU 上用 bf16 LoRA。GPU 推荐 Linux/WSL2；
原生 Windows 的 bitsandbytes 4-bit 不稳定。

```bash
conda create -n w7 python=3.11 -y
conda activate w7
pip install -r training/requirements-train.txt

# LLaMA-Factory 训练/合并工具单独安装（训练机专用）
git clone https://github.com/hiyouga/LLaMA-Factory.git
cd LLaMA-Factory
pip install -e ".[torch,metrics]"
```

### Ascend NPU（ModelArts Notebook）

Notebook 选 `modelarts.bm.npu.arm.1snt9b1`（1×910B）即可，1.5B 不需要多卡。
镜像选预装 CANN + torch_npu 的 **Ascend-PyTorch** 镜像；不要从 PyPI 重装
torch，也不要装 bitsandbytes（4-bit 是 CUDA-only）。用带
`--system-site-packages` 的 venv 继承镜像里的 torch_npu：

```bash
python -m venv --system-site-packages /home/ma-user/work/w7-venv
source /home/ma-user/work/w7-venv/bin/activate
pip install llamafactory modelscope -i https://mirrors.aliyun.com/pypi/simple
python -c "import torch, torch_npu; print(torch.npu.is_available())"
```

训练改用 `*_npu.yaml`（已去掉 `quantization_bit: 4`，其余超参不变）：

```bash
llamafactory-cli train configs/train_lora_cfdk_baseline_npu.yaml
llamafactory-cli train configs/train_lora_qwen2.5-1.5b_npu.yaml
llamafactory-cli train configs/train_lora_stage2_npu.yaml
```

输出目录与 GPU 版一致，后续 `merge_lora_stage2.yaml` 不用改。

下载基座（ModelScope，国内网络更稳）：

```bash
modelscope download --model Qwen/Qwen2.5-1.5B-Instruct \
  --local_dir training/models/Qwen2.5-1.5B-Instruct
```

从仓库根目录进入 `training/` 再训练，配置里的相对路径才成立。

A baseline（只用 CFDK，超参与 stage 1 相同，只差语料）：

```bash
cd training
llamafactory-cli train configs/train_qlora_cfdk_baseline.yaml
# saves/qwen2.5-1.5b-cfdk-baseline-lora/
```

B stage 1（CFDK + 手册）：

```bash
llamafactory-cli train configs/train_qlora_qwen2.5-1.5b.yaml
# saves/qwen2.5-1.5b-evidence-lora/
```

Stage 2（从 stage-1 adapter 继续，手册重配比）：

```bash
llamafactory-cli train configs/train_qlora_stage2.yaml
# saves/qwen2.5-1.5b-evidence-stage2-lora/
```

如果训练机上的 LLaMA-Factory 版本不支持在训练配置里写
`adapter_name_or_path`，先执行下面的 stage-1 merge，再把
`train_qlora_stage2.yaml` 的 `model_name_or_path` 指向
`saves/qwen2.5-1.5b-evidence-merged` 并删掉 `adapter_name_or_path`。

先看 loss 是否下降、输出是否稳定为 JSON；这不是效果，只是管线检查。
每一步都应以 `w7_b/val.jsonl` 的目标域文档做一次 eval，不能只看训练 loss。

## 合并、量化、香橙派

```bash
cd training
# 只训练了 stage 1 时：
llamafactory-cli export configs/merge_lora_qwen2.5-1.5b.yaml
# saves/qwen2.5-1.5b-evidence-merged/

# 训练了 stage 2 时，合并最终 adapter：
llamafactory-cli export configs/merge_lora_stage2.yaml
# saves/qwen2.5-1.5b-evidence-final/

python /path/to/llama.cpp/convert_hf_to_gguf.py \
  saves/qwen2.5-1.5b-evidence-final \
  --outfile artifacts/qwen2.5-1.5b-evidence-f16.gguf --outtype f16
/path/to/llama.cpp/build/bin/llama-quantize \
  artifacts/qwen2.5-1.5b-evidence-f16.gguf \
  artifacts/qwen2.5-1.5b-evidence-q4_k_m.gguf Q4_K_M
```

把 GGUF 和 `manifest.json` 拷到香橙派（路径按你的 Ollama 安装调整）：

```bash
scp artifacts/qwen2.5-1.5b-evidence-q4_k_m.gguf \
  <user>@<orangepi>:/opt/models/industrial-extractor/
```

香橙派上：

```bash
cd /opt/models/industrial-extractor
# 把 configs/Modelfile 里的 FROM 指向本目录的 GGUF
ollama create industrial-extractor -f Modelfile
ollama run industrial-extractor "只输出 JSON..."
```

Ollama 的 OpenAI 兼容接口是 `http://localhost:11434/v1/chat/completions`，
评测脚本可以直接指向它。

## 评测

`w7_b` 的验证集来自天津标准，测试集来自 Grundfos 8.x 手册；两者都是
**训练中没出现过的目标域文档**。`evaluate.py` 用行对齐的 ShareGPT +
`*_meta.jsonl`，所以直接指向 `w7_b` 即可：

```bash
# 基座对照（验证集）
ollama pull qwen2.5:1.5b-instruct
python examples/finetune_evidence_extraction/evaluate.py \
  --model qwen2.5:1.5b-instruct \
  --data training/data/w7_b/val.jsonl \
  --meta training/data/w7_b/val_meta.jsonl \
  --out training/data/eval-baseline-val.json

# 微调后（同一个验证集 + 测试集）
python examples/finetune_evidence_extraction/evaluate.py \
  --model industrial-extractor \
  --data training/data/w7_b/val.jsonl \
  --meta training/data/w7_b/val_meta.jsonl \
  --out training/data/eval-student-val.json
python examples/finetune_evidence_extraction/evaluate.py \
  --model industrial-extractor \
  --data training/data/w7_b/test.jsonl \
  --meta training/data/w7_b/test_meta.jsonl \
  --out training/data/eval-student-test.json
```

Coox 跨域留出集单独构建成评测目录（53 条，全部进 val，不参与训练）：

```bash
python scripts/build_distillation_dataset.py \
  --chunks training/data/manual_holdout_chunks.jsonl \
  --labels manual=training/data/manual_holdout_labels.jsonl \
  --force-split coox-centrifugal-pump=val \
  --val-ratio 0 --test-ratio 0 \
  --out-dir training/data/coox_holdout_eval
```

指标：`json_valid_rate`、实体 P/R/F1、关系 P/R/F1、`quote_verbatim_rate`
（必须 1.0）、空输出率、平均输出 token 数、香橙派端到端延迟。按文档切分，
固定 temperature 0。报告必须分开写 CFDK 测试集、天津/Grundfos 目标域、
Coox 跨域三组数字；把它们混在一起会掩盖跨域退化。

## 主动学习队列

低置信/校验失败样本进 `training/data/review_queue.jsonl`：

```json
{"id": "...", "chunk_id": "...", "chunk_text": "...",
 "model_output": {...}, "error": "quote-not-verbatim",
 "confidence": 0.41, "status": "pending"}
```

人工接受/编辑/拒绝后写回 `training/data/reviewed_labels.jsonl`，下轮训练
合并。队列含公开原文，也按数据目录处理，不进 Git。

## 边界

- 模型是**本地抽取器**，不是诊断模型；诊断仍由 Nexent Agent 调 MCP 工具。
- 101 条航空数据不能代表泵域效果；泵域指标必须用天津/Grundfos/Coox
  留出文档单独报告。
- 手册数据只有 180 条唯一训练 chunk，stage 1 的 1.98% 配比**不足以**
  支撑“目标域适配完成”的说法；必须看 stage 2 的 22.8% 配比和留出集
  指标，且在教师 pass 补足负样本前不宣称达到计划的 20% 负样本。
- CFDK 的主标签是“单故障记录”规则生成的保守 `CAUSES`；多故障记录只保留
  实体、不造关系，因此关系召回率的上限受数据形态限制。
- GGUF Q4_K_M 在香橙派 ARM CPU 上的吞吐要实测；1.5B 仍太慢就缩短
  `num_ctx` 或换更小模型。
- 训练依赖只进 `requirements-train.txt`，不修改 LightRAG 核心
  `pyproject.toml`。

## 结果审计与审核队列

评测归档后运行：

```bash
python scripts/audit_w7_results.py
```

产物：

- `training/artifacts/w7-audit.json`：机器可读的验收状态、指标和待补项；
- `training/artifacts/w7_review_queue.jsonl`：合同错误、非逐字证据、空输出、
  负样本误报和畸形关系的优先复核队列。

当前交付包已通过核心抽取评测，但仍缺 GGUF、香橙派性能和 A/B2 消融；
具体状态以 `EVAL_REPORT.md` 和 `w7-audit.json` 为准。
