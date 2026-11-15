# W9 评测与消融

## 真正未参与 W7 的新中文测试集

`w9_unseen_web_20261115` 使用 6 份独立中文泵技术资料，来源包括科普中国和
5 家泵制造企业。它们不在 CFDK、Grundfos、天津标准、Coox、P-101A 或
W7 `training/data/` 中，且不参与训练、教师标注、适配器选择或提示词调优。

这批资料是公开的厂商技术说明和科普文章，不是虚构工单，也不是某家企业的
真实私有维修记录。它适合验证“未见中文手册/故障表”的抽取、证据定位和
来源检索；真实工单泛化能力仍需另找有授权的维修记录数据，不能由本测试集
单独证明。

来源清单与用途边界：

- `eval/w9_sources.json`

完整重建：

```bash
PYTHON=python bash scripts/run_w9_unseen.sh
```

下载并清洗 HTML，原始页面和全文保存在被 Git 忽略的 `eval/corpus/`：

```bash
python scripts/fetch_w9_sources.py
```

脚本会比对已冻结的正文 SHA-256。来源页面改版时默认直接失败，避免评测集
静默变化；人工确认新内容后才可加 `--allow-content-change` 重新冻结。

30 条冻结案例：

- `eval/datasets/w9_unseen_web_20261115.jsonl`
- 每个 gold quote 必须能在下载文本中逐字找到；
- 每条 case 记录 publisher、来源 ID、问题、标准术语和禁止结论；
- 重新生成 manifest 会校验 quote、来源、SHA-256 和 W7 来源重叠：

```bash
python scripts/build_w9_web_manifest.py
```

检索基线只使用六来源 corpus，不读取 gold terms：

```bash
python scripts/evaluate_w9_unseen_sources.py
```

当前基线为 `source_hit@1=0.8`、`source_hit@3=0.967`。这不是最终系统结论，
而是证明新语料可检索、案例可执行的未调参基线。该数据只用于 W9 验收，
不得回流到 W7 训练或教师标注。

## 确定性领域评测

运行：

```bash
python scripts/evaluate_domain.py
```

报告：

```text
eval/reports/industrial-domain-eval.json
```

在 P-101A 出口压力不足场景上对比：

- `lexical_no_graph`：只做故障名称检索；
- `graph_evidence_full`：设备定位、故障检索、逐字证据、故障树和确定性诊断 Skill。

指标：

- 检索命中率；
- 故障树顶层事件与边结构；
- 故障树原因分支覆盖率；
- 证据覆盖率；
- quote hash 与字符区间一致率；
- 端到端诊断状态；
- 图谱路径相对词法路径的增量能力。
- 11 条正向、别名、负向、证据、树和本体形状用例；
- 删除“吸入管法兰漏气”分支证据后，证据复核必须从通过降级为拒绝。

## 已接入的 W7 消融

`training/artifacts/eval/w7_b_test/report.json` 对比：

- 基座 zero-shot；
- B1 QLoRA 学生。

审计入口：

```bash
python scripts/audit_w7_results.py
```

## Nexent 单 Agent / 三 Agent

单 Agent 与三 Agent 的线上消融必须使用同一 Agent 名称、同一模型、同一知识库和同一问题集。
录制两个版本的 `nexent/build/demo-*.json` 后，使用统一 transcript 指标比较：

- 是否出现真实子 Agent 调用；
- 工具调用次数和失败次数；
- `[evidence:...]` 引用数量；
- 引用去重率；
- 是否出现 DSML 协议失败；
- 最终回答是否满足场景关键词和禁止词。

单 Agent 臂通过同一导入器切换：

```bash
python -m nexent.scripts.import_to_nexent --single-agent --refresh-skills
python -m nexent.scripts.demo --query "<固定问题>" \
  --output nexent/build/eval-single-agent.json

python -m nexent.scripts.import_to_nexent --refresh-skills
python -m nexent.scripts.demo --query "<固定问题>" \
  --output nexent/build/eval-three-agent.json
```

然后运行：

```bash
python scripts/evaluate_nexent_transcripts.py \
  --run single=nexent/build/eval-single-agent.json \
  --run three=nexent/build/eval-three-agent.json \
  --expect-subagent-in three=evidence_review_agent \
  --expect-subagent-in three=ontology_expert_agent \
  --out eval/reports/nexent-agent-ablation.json
```
