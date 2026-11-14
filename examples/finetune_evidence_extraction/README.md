# 微调证据抽取模型：一个能跑通的例子

这是 W7「教师-学生与主动学习」的教学样例，不是最终交付物。它用领域包里
现成的 4 个种子片段 + 1 个负样本，把“文本 → 故障事件 + 因果证据”的
微调管线完整走一遍：**造数据 → QLoRA 训练 → 合并 → GGUF → 评测**。

5 条样本只能验证管线通不通，不能证明模型好坏。真正的 W7 数据集要从这里
扩展（见最后两节）。

## 任务定义

输入：一个文本片段（手册、维修记录、巡检表行）。

输出：一个 JSON 对象，只包含 `FaultEvent` 和 `CAUSES`：

```json
{
  "entities": [
    {
      "local_id": "filter",
      "class": "FaultEvent",
      "name": "入口过滤器堵塞",
      "description": "过滤器流通面积减小，泵入口流量下降",
      "cause_rule": {"event_local_id": "..."},
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

三条硬约束，和 `lightrag/fault_tree/schemas.py` 的契约一致：

1. 只输出 JSON，不输出 Markdown、解释或多余文字。
2. 每个实体、每条关系都必须带 `evidence.quote`。
3. `quote` 必须是输入文本里的**逐字连续片段**，不能改写、概括、翻译或拼接。

模型不输出正式 UID、页码、chunk ID、审核状态；这些由投影器从来源位置补齐。

## 目录

```
examples/finetune_evidence_extraction/
├── build_dataset.py     # 从种子包生成 train/val JSONL
├── dataset_info.json    # LLaMA-Factory 数据集注册
├── train_qlora.yaml     # QLoRA 训练配置
├── merge_lora.yaml      # 合并 LoRA 配置
├── evaluate.py          # OpenAI 兼容接口的评测脚本
├── data/                # 生成物，已 gitignore
├── models/              # 基座模型，已 gitignore
└── saves/               # 训练/合并输出，已 gitignore
```

## 第 0 步：本地构建数据（现在就能跑）

在仓库根目录执行：

```bash
.venv/Scripts/python.exe examples/finetune_evidence_extraction/build_dataset.py
```

Windows 之外的 Python 用 `python` 即可。预期输出类似：

```
[build] 4 train / 1 val samples, 10 entities, 5 relations
[build] wrote D:\...\examples\finetune_evidence_extraction\data
[build] system prompt sha256 ...
```

生成三个文件：

| 文件 | 内容 |
|---|---|
| `data/train.jsonl` | 4 条训练样本 |
| `data/val.jsonl` | 1 条留出样本（巡检表行） |
| `data/val_meta.jsonl` | 对应的原文和 gold，供 `evaluate.py` 使用 |
| `data/manifest.json` | 样本清单、系统提示词哈希、统计 |

看一条训练样本：

```bash
head -1 examples/finetune_evidence_extraction/data/train.jsonl
```

`build_dataset.py` 做了五件事，每一步都值得单独讲：

1. **只保留 `FaultEvent` + `CAUSES`。** 设备主数据、部门、文件、逻辑门由
   导入器和投影器生成，不让小模型学。
2. **修证据。** 种子里有些实体没有自己的 `evidence`（例如维修记录里的
   “出口压力不足”）。脚本先用实体名、再用相邻关系的 quote 补；补不上就
   删除该实体及其关系。**宁可少标，也不训练模型编造证据。**
3. **剪掉悬空规则。** 如果 `cause_rule` 引用了被删掉的实体（例如
   `record-2026-10-03` 里的过滤器），递归剪枝；不足两个子节点的 AND/OR
   直接移除。
4. **加负样本。** 手册 4.3「处理措施」整段都是维修动作，没有故障事件，
   gold 是空数组。这是在教模型“维修措施不是原因”。
5. **逐字校验。** 写文件前检查每一条 quote 是否真的在原文里；有一条
   不通过就报错退出。

## 第 1 步：准备训练环境

推荐 Linux + NVIDIA GPU。3B 模型 QLoRA 大约需要 10–16 GB 显存；
24 GB 很宽裕。Windows 建议用 WSL2，原生 Windows 上 `bitsandbytes`
的 4-bit 支持不稳定。

```bash
conda create -n lf python=3.11 -y
conda activate lf

git clone https://github.com/hiyouga/LLaMA-Factory.git
cd LLaMA-Factory
pip install -e ".[torch,metrics]"
```

确认：

```bash
llamafactory-cli version
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

国内下载模型走 ModelScope，避免 Hugging Face 网络问题：

```bash
pip install modelscope
modelscope download --model Qwen/Qwen2.5-3B-Instruct \
  --local_dir /path/to/LightRAG/examples/finetune_evidence_extraction/models/Qwen2.5-3B-Instruct
```

如果显存只有 8 GB，把 `train_qlora.yaml` 里的
`model_name_or_path` 换成 `Qwen2.5-1.5B-Instruct`，其余不变。

## 第 2 步：训练

进入样例目录，让 `dataset_dir: data` 和相对模型路径生效：

```bash
cd /path/to/LightRAG/examples/finetune_evidence_extraction
llamafactory-cli train train_qlora.yaml
```

因为只有 4 条训练样本，默认 `gradient_accumulation_steps: 8` 意味着
每个 epoch 只走 1 个优化步，loss 几乎不动。做冒烟测试时覆盖两个参数：

```bash
llamafactory-cli train train_qlora.yaml \
  gradient_accumulation_steps=1 \
  num_train_epochs=20 \
  logging_steps=1
```

这样会有 80 个优化步，足够看到 loss 从 2–4 降到 1 以下，并让模型记住
JSON 格式。**这是过拟合，不是效果**；它的作用是证明数据、模板、标签
三者对得上。

`train_qlora.yaml` 的关键项：

| 项 | 值 | 说明 |
|---|---|---|
| `quantization_bit` | 4 | QLoRA；没有 bitsandbytes 时删掉这两行，用 bf16 |
| `finetuning_type` | lora | 只训练低秩适配器 |
| `lora_rank` / `alpha` | 16 / 32 | 小数据够用；数据多了可升到 32/64 |
| `template` | qwen | 必须和基座匹配 |
| `cutoff_len` | 4096 | 系统提示 + 片段足够 |
| `learning_rate` | 1e-4 | LoRA 常用区间 1e-4 ~ 2e-4 |
| `bf16` | true | Ampere 及以上；老卡改成 `fp16: true` |

输出在 `saves/qwen2.5-3b-evidence-lora/`。先看 `trainer_state.json` 里的
loss 曲线，再决定要不要继续。

## 第 3 步：合并与导出

```bash
llamafactory-cli export merge_lora.yaml
```

得到 `saves/qwen2.5-3b-evidence-merged/`，这是一个普通的 HF 模型目录。

转 GGUF（给 Ollama / 香橙派用）：

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build
cmake --build build --config Release -j

python convert_hf_to_gguf.py \
  /path/to/LightRAG/examples/finetune_evidence_extraction/saves/qwen2.5-3b-evidence-merged \
  --outfile evidence-f16.gguf --outtype f16

./build/bin/llama-quantize evidence-f16.gguf evidence-q4_k_m.gguf Q4_K_M
```

注册到 Ollama：

```bash
cat > Modelfile <<'EOF'
FROM ./evidence-q4_k_m.gguf
PARAMETER temperature 0
PARAMETER num_ctx 4096
EOF

ollama create industrial-extractor -f Modelfile
ollama list
```

Ollama 的 OpenAI 兼容接口在 `http://localhost:11434/v1/chat/completions`。

## 第 4 步：评测

先跑基座做对照：

```bash
ollama pull qwen2.5:3b-instruct
python evaluate.py --model qwen2.5:3b-instruct
```

再跑微调后的模型：

```bash
python evaluate.py --model industrial-extractor --out eval-after.json
```

输出分两部分：每个样本一行进度，最后是完整 JSON 报告。

```
[1/1] FILE-P101A-INSPECTION:inspection-2026q3-row1: json=ok ent_f1=1.00 rel_f1=1.00 quote=3/3
{
  "aggregate": {
    "json_valid_rate": 1.0,
    "entity_f1": 1.0,
    "relation_f1": 1.0,
    "quotes": 3,
    "quotes_verbatim": 3,
    "quote_verbatim_rate": 1.0
  }
}
```

看四个指标：

| 指标 | 含义 | 期望 |
|---|---|---|
| `json_valid_rate` | 输出能否直接 `json.loads` | 微调后接近 1.0 |
| `entity_f1` | `(class, name)` 集合的 F1 | 数据多了才有意义 |
| `relation_f1` | `(relation_code, 原因名, 结果名)` 的 F1 | 数据多了才有意义 |
| `quote_verbatim_rate` | 预测 quote 能在原文逐字找到的比例 | **必须 1.0** |

只有 1 条验证样本，F1 不是模型质量，只是管线检查。把 `--val-assets`
换成别的文档、或把种子扩到 50+ 片段，数字才开始有解释力。

## 第 5 步：接回项目

模型输出是 `FaultCandidateBatch` 的“去定位字段”版本。接回时有两条路：

1. **离线导入（现在就能做）**：把模型输出写回
   `domain_packs/industrial_fault/seeds/extraction/*.json` 的形状，补上
   `chunk_id`，再走现有的 `scripts/import_seed_domain.py` /
   `project_candidate_batch` / 校验器。
2. **在线抽取（W7 的目标形态）**：MCP server 增加一个抽取工具，输入
   `chunk_id + text + locator`，调用本地模型，返回校验后的候选；投影器
   和现有工具链不变。

注意分工：**微调后的小模型是本地抽取器，不是诊断模型。** 它负责把原始
文本变成带证据的结构化候选；诊断仍由 Nexent Agent 调 MCP 工具完成。
这样原始手册和维修记录不出本地，只有结构化结果进入上层。

## 第 6 步：从 5 条到 2000 条

这是 W7 真正的工作量所在。建议按下面的顺序做：

### 6.1 教师标注

1. 收集文档：设备手册、维修记录、巡检表、故障通报。
2. 规则筛句：只把含因果/条件/记录模式的句子送进教师模型，降低 token
   成本。触发词示例：`导致`、`造成`、`引起`、`使得`、`由于`、`当…时`、
   `如果…则`、`检查发现`、`清理后…恢复`。
3. 教师模型（本地大模型或获批的外部模型）按同一系统提示输出 JSON。
4. 校验器过滤：Schema 合法、quote 逐字、类/关系在本体内。
5. 人工抽检 5%–10%；校验失败和低置信样本进入主动学习队列。
6. 按文档去重、按文档切分 train/val，**绝不能把同一文档的 chunk 分到
   两边**。

### 6.2 数据配比

| 类型 | 占比 | 作用 |
|---|---|---|
| 正例（含因果链） | ~60% | 学抽取 |
| 负例（措施、操作步骤、无故障描述） | ~20% | 学“不抽” |
| 难例（AND/OR、嵌套链、别名、跨句因果） | ~20% | 学结构 |

### 6.3 主动学习队列

`evaluate.py` 或在线抽取器为每条样本打分：

```
score = 低置信度 + 校验失败 + 检索/实体歧义
```

队列一行至少包含：

```json
{"id": "...", "chunk_text": "...", "model_output": {...},
 "error": "evidence-quote-not-verbatim", "confidence": 0.41, "status": "pending"}
```

人工只处理队首；修完后追加到训练集，按固定周期重训。这样人工标注量
集中在模型最不确定的地方，而不是随机抽样。

### 6.4 W7 验收要报的指标

- 基座 vs 蒸馏后：实体 F1、关系 F1、quote 逐字率、JSON 合法率；
- 固定随机种子、固定验证集、按文档报告；
- 模型卡：数据来源、基座、训练参数、评测数字、已知失败模式；
- 低置信样本确实能进入审核队列，并有人工处理记录。

## 隐私与边界

- 原始手册、维修记录、巡检表属于企业数据。教师模型必须本地部署，或
  只在获批的白名单文本上调用外部 API。
- 本样例的数据来自已提交的种子包，可随时重建；`data/`、`models/`、
  `saves/` 都不进 Git。
- 量化后的 GGUF 适合香橙派/边缘设备；但 3B q4 在 ARM CPU 上的吞吐要
  实测，必要时降到 1.5B。
- 这是教学样例。W7 的正式交付应放在 `training/`（或团队约定的目录），
  并带模型卡、评测报告和主动学习队列。

## 常见问题

**`KeyError: 'conversations'`**
`dataset_info.json` 的 `formatting` 必须是 `sharegpt`，且 `columns.messages`
指向 `conversations`。

**CUDA out of memory**
先把 `per_device_train_batch_size` 保持 1，确认 `gradient_checkpointing: true`；
再把 `cutoff_len` 降到 2048；还不够就换 1.5B。

**`bitsandbytes` 报错**
删掉 `train_qlora.yaml` 里的 `quantization_bit` 和 `quantization_method`，
改用 bf16 LoRA。

**模型输出带 ```json 代码块**
`evaluate.py` 会自动剥掉；生产环境要加严格解析，后续可上
Outlines/XGrammar 之类的约束解码。

**验证集只有 1 条，指标全是 1.0**
正常。这是冒烟测试；扩到 50+ 片段再看数字。
