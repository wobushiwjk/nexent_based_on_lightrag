# 领域包（domain packs）

领域包把一个行业的语义、数据契约、标准映射和演示语料放在同一个可版本化的目录里。
LightRAG 核心、MCP 服务、Nexent Skill 都只依赖领域包，不把某个行业写死在代码里。
`industrial_fault` 是当前完整实现的参考包；`medical_device` 是同构迁移的占位包。

## 目录约定

```
domain_packs/<domain>/
├── ontology.yml              # 类、关系、约束、版本号（语义源）
├── standards/crosswalk.yml   # 本地概念到国际/行业标准的映射
├── schemas/                  # 由 contracts.py 导出的 JSON Schema + manifest。数据格式说明书
├── seeds/
│   ├── documents/            # 原始语料，证据 quote 必须逐字来自这里
│   └── demo_bundle.json      # 端到端联调用的黄金样例
└── README.md
```

`ontology.yml` 是语义源，`prompts/entity_type/<domain>.yml` 只是送入抽取提示词的投影；
两者的版本号和 `prompt_visible` 标记必须一致，由 `tests/domain` 约束。
Pydantic 模型（`lightrag/fault_tree/contracts.py`）是可执行契约，`schemas/` 是它的
JSON Schema 投影，供非 Python 的 MCP/Nexent/审核界面复用。

## 版本规则

- 语义变化（新增/删除类、关系、约束，或改变证据要求）必须提升
  `ontology_version`，并同步修改 `schema_version`。
- 只改字段说明、不改语义时，提升 `schema_version` 即可。
- 改完契约后必须重新生成 Schema，并提交 `schemas/` 与 `manifest.json`：

```bash
python scripts/export_domain_schemas.py
```

- `manifest.json` 记录每个 Schema 的 sha256；`tests/domain` 会检查已提交的 Schema
  是否与当前 Pydantic 契约一致，以及文件是否被手工改坏。

## 校验

```bash
# 默认校验 industrial_fault 与它的 demo_bundle.json
python scripts/validate_domain.py

# 指定领域包 / 指定 bundle
python scripts/validate_domain.py --pack domain_packs/industrial_fault \
    --bundle domain_packs/industrial_fault/seeds/demo_bundle.json

# 机器可读输出（CI、MCP 工具、审核队列可复用）
python scripts/validate_domain.py --json
```

退出码为 `0` 表示没有 error（warning 仍然打印）；有 error 时退出码为 `1`。
校验覆盖：ONTOLOGY 版本、实体 UID/设备位号唯一性、证据逐字匹配与哈希、字符区间、
资产校验和与大小、证据目标存在性、事实端点与关系定义、规则表达式、故障树单顶事件、
无环性、逻辑门输入/输出数量、顶事件可达性、Schema manifest 哈希。

## 新增一个领域

1. 复制 `industrial_fault` 的目录骨架，先写 `ontology.yml` 和标准 crosswalk。
2. 在 `lightrag/fault_tree/contracts.py` 中判断：能否复用现有类？
   不能复用才新增 Pydantic 模型，并把它加入 `SCHEMA_TARGETS`。
3. 准备至少 3 份不同载体的种子文档（手册、记录、表格），
   每条 evidence 的 quote 必须是原文连续片段。
4. 写一棵人工确认的 gold 树，放进 `demo_bundle.json`。
5. 重新导出 Schema，运行 `python scripts/validate_domain.py --pack <新包>` 和
   `./scripts/test.sh tests/domain`。
