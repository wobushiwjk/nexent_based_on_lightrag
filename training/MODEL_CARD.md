# Model Card：工业故障证据抽取学生模型

状态：核心抽取版本及 Nexent 上传链路已验证；GGUF/边缘部署尚未验收。

## 模型

| 项目 | 值 |
| --- | --- |
| 基座 | `Qwen/Qwen2.5-1.5B-Instruct` |
| LoRA | r=16，alpha=32，dropout=0.05，`lora_target=all` |
| 训练方式 | QLoRA 4-bit，3 epochs，1,827 steps |
| 版本 | `industry-fault-extractor-b1-v1` |
| Hugging Face 产物 | `qwen2.5-1.5b-evidence-merged/` |
| GGUF | 尚未交付 |
| 任务 | 从工业维护文本抽取 `FaultEvent`、`CAUSES` 和逐字证据 |

## 输入输出

输入是一段公开或已获授权的工业维护文本。

输出只包含：

```json
{
  "entities": [
    {
      "local_id": "e1",
      "class": "FaultEvent",
      "name": "...",
      "description": "...",
      "cause_rule": {},
      "evidence": [{"quote": "..."}]
    }
  ],
  "relations": [
    {
      "relation_code": "CAUSES",
      "source_local_id": "e1",
      "target_local_id": "e2",
      "evidence": [{"quote": "..."}]
    }
  ]
}
```

所有 quote 必须逐字出现在输入文本中。模型不输出 UID、页码、租户或审核状态。

## 训练数据

| 来源 | 规模或用途 |
| --- | --- |
| CFDK 压缩机维修记录 | 跨域主数据，专家标签和保守 `CAUSES` 规则 |
| Grundfos NK/NKE 手册 | 泵域训练 |
| Grundfos S 手册 | 泵域训练 |
| Grundfos 8.x 手册 | 强制测试文档 |
| 天津供水泵站标准 | 强制验证文档 |
| Coox 离心泵手册 | 跨域留出，未进入训练 |
| `teacher_labels.jsonl` | `deepseek-flash` 补标的公开文本 |

数据集 B：

```text
train 4,868 / val 623 / test 652
labels 6,143
negatives 1,732
leakage problems 0
```

## 评测结果

| 指标 | 基座 | B1 学生 |
| --- | ---: | ---: |
| JSON 合法率 | 0.9678 | 1.0000 |
| 合同合法率 | 0.0015 | 0.9801 |
| 实体 F1 | 0.1702 | 0.6841 |
| 关系 F1 | 0.0000 | 0.5212 |
| 证据逐字率 | 0.8136 | 0.9824 |
| 负样本准确率 | 0.0255 | 0.9745 |
| 平均输出 tokens | 337.90 | 92.13 |
| 规则准确率 | 0.0000 | 0.0000 |

目标手册分组的 B1 指标：

```text
entity F1:   0.9845
relation F1: 0.9767
quote rate:  0.9545
JSON:        1.0000
```

## 预期用途

- 作为 Nexent/MCP 工作流前置的结构化故障候选抽取器；
- 通过 `ingestion_mcp_server` 接收 Nexent 用户上传，候选继续经过确定性证据和本体校验；
- 输出必须继续经过本体校验、逐字证据校验、故障树生成和人工复核；
- 适合本地离线或训练机推理原型。

## 非用途

- 不能替代人工诊断、安全评估或维修决策；
- 不能把抽取候选直接当作已确认故障；
- 当前规则准确率为 0，不能宣称已具备可靠 AND/OR 故障树推理；
- 当前未提供 GGUF，不能宣称已在香橙派上完成部署；
- 不应外推到未评测行业、私有企业数据或非公开文档。

## 已知限制

- 证据逐字率为 0.9824，低于项目要求的 1.0。
- CFDK 关系 F1 只有 0.1910。
- 13 条合同错误和 1 条畸形关系仍存在。
- 4 条教师标注失败样本尚未补标。
- 没有 A_cfdk 和 B2 stage-2 消融结果。
- 没有香橙派延迟、吞吐和峰值内存数据。
- 训练数据含教师模型生成标签，需要继续抽样人工复核。

## 隐私和合规

- 教师只接收 `sources.yml` 中标记为公开的数据。
- 外发日志记录 chunk、来源、许可证、哈希、模型和时间，不记录路径、租户、客户或工单信息。
- 原始语料、教师标签、权重和 merged 模型不进入 Git。
- 模型权重和 tar 包应按文件哈希与 manifest 独立交付。

## 维护

- 每次复训必须固定相同测试集并重新生成 `w7-audit.json`。
- 优先处理 `w7_review_queue.jsonl` 中的合同错误、非逐字证据和空输出。
- 新增人工复核标签后，重建数据集并重新评估 base/B1/A/B2。
- 只有逐字证据率达到 1.0、规则指标完成、GGUF 实测完成，才能将 W7 标记为全部验收。
