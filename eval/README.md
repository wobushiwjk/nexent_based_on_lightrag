# W9 评测与消融

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
