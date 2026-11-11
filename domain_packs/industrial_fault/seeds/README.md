# 种子语料说明

**本目录除本说明外全部是合成数据，不是真实客户资料、真实维修记录或正式出版的手册节选。**
它们是为了验证本体、证据链和 AND/OR 故障树而手写的演示语料，设备编号、日期、
维修结论均为虚构。用于比赛演示时可以直接说明这一点，不能当作行业真实数据引用。

| 文件 | 性质 | 用途 |
| --- | --- | --- |
| `documents/pump_manual_excerpt.md` | 手写合成手册节选 | 覆盖完整因果链与 AND 条件 |
| `documents/maintenance_record.md` | 手写合成维修记录 | 覆盖跨文档佐证 |
| `documents/inspection_table.md` | 手写合成巡检表 | 覆盖表格模态证据 |
| `demo_bundle.json` | 人工标注 gold bundle | 端到端联调与回归基准 |
| `bad_bundle_sample.json` | 故意写坏的反例 | 证明校验器会拒绝坏数据 |

## 修改语料时的连锁影响

`demo_bundle.json` 里的 `file_size`、`checksum`、`char_start`、`char_end` 和
`quote_hash` 都绑定到当前文件的**精确字节**。修改任何种子文档后必须：

1. 重新计算文件 sha256 和字节数，更新对应 `FileAsset`；
2. 重新确认每条 evidence 的 `quote` 仍是原文连续片段；
3. 重新计算 `char_start` / `char_end` 和 `quote_hash`；
4. 重新运行 `python scripts/validate_domain.py` 和 `./scripts/test.sh tests/domain`。
