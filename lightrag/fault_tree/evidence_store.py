"""
evidence_store.py 实现了一个基于 SQLite 文件数据库的证据存储类 EvidenceStore。
它负责把 Evidence 记录写入 SQLite，并提供按租户、证据 ID 或目标对象查询的接口。

设计上，证据正文（quote）保存在数据库中，调用方需要时才读取。
比如 MCP 启动时可以先查证据 ID 和目标索引；用户调用证据追溯功能时，再读取对应 quote。

它不是独立运行的数据库服务，也不负责决定存储文件的位置。
调用者传入数据库文件路径后，它用 Python 自带的 sqlite3 打开或创建该文件。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path

from lightrag.fault_tree.contracts import Evidence, EvidenceTarget

#_SCHEMA 定义了两张表：

# evidence：存证据主体，包括租户、证据 ID、文件和片段 ID、位置、quote、哈希、模态、置信度和审核状态。
# 主键是 (tenant_id, evidence_id)，因此同一租户内一个证据 ID 对应一行。
# evidence_targets：存证据关联到哪些实体、事实或规则，以及关联角色和顺序。
# 它通过外键关联 evidence；开启外键后，删除证据会级联删除关联目标。

# 三个索引：
# idx_evidence_targets_lookup 和 idx_evidence_targets_trace：帮助按租户、目标类别和目标 ID 找证据；
# idx_evidence_asset：帮助按租户和来源文件查证据。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    tenant_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    page INTEGER,
    section_path TEXT,
    bbox_json TEXT,
    char_start INTEGER,
    char_end INTEGER,
    quote TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    modality TEXT NOT NULL,
    confidence REAL,
    extractor TEXT,
    extraction_run_id TEXT,
    review_status TEXT NOT NULL,
    PRIMARY KEY (tenant_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS evidence_targets (
    tenant_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    role TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, evidence_id, target_type, target_id, role),
    FOREIGN KEY (tenant_id, evidence_id)
        REFERENCES evidence (tenant_id, evidence_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evidence_targets_lookup
    ON evidence_targets (tenant_id, target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_evidence_targets_trace
    ON evidence_targets (tenant_id, target_type, target_id, evidence_id);
CREATE INDEX IF NOT EXISTS idx_evidence_asset
    ON evidence (tenant_id, asset_id);
"""


class EvidenceStore:
    """Store evidence rows and their targets in one SQLite database."""
    # init 初始化存储：
    # 保存数据库文件路径，并创建父目录；
    # 创建线程锁；
    # 连接 SQLite 文件；如果文件不存在，SQLite 会创建它；
    # 设置查询结果可以按列名访问；
    # 开启外键约束、设置锁等待时间，并创建缺失的表和索引；
    # 初始化两个计数器：quotes_loaded 记录已读取证据正文的次数，rows_written 记录写入或更新证据的次数。
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 10000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.quotes_loaded = 0
        self.rows_written = 0

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    
    #upsert 写入一条证据。它只是把单条证据包装成列表，然后交给 upsert_many。
    def upsert(self, evidence: Evidence) -> None:
        self.upsert_many([evidence])

    #upsert_many 批量写入证据：

    # 将证据正文及定位信息写入 evidence；
    # 如果主键已存在，就更新该行；
    # 删除该证据之前的 target 关联，再根据当前 targets 重新写入；
    # 整批操作在锁和 SQLite 事务上下文中完成；
    # 返回传入证据的数量；空输入则返回 0。
    # 它会先把输入转成列表，所以如果传入很大的生成器，整批 Evidence 对象会先同时驻留内存。
    def upsert_many(self, evidence: Iterable[Evidence]) -> int:
        rows = list(evidence)
        if not rows:
            return 0
        with self._lock, self._conn:
            for item in rows:
                self._conn.execute(
                    """
                    INSERT INTO evidence (
                        tenant_id, evidence_id, asset_id, chunk_id, page,
                        section_path, bbox_json, char_start, char_end, quote,
                        quote_hash, modality, confidence, extractor,
                        extraction_run_id, review_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, evidence_id) DO UPDATE SET
                        asset_id = excluded.asset_id,
                        chunk_id = excluded.chunk_id,
                        page = excluded.page,
                        section_path = excluded.section_path,
                        bbox_json = excluded.bbox_json,
                        char_start = excluded.char_start,
                        char_end = excluded.char_end,
                        quote = excluded.quote,
                        quote_hash = excluded.quote_hash,
                        modality = excluded.modality,
                        confidence = excluded.confidence,
                        extractor = excluded.extractor,
                        extraction_run_id = excluded.extraction_run_id,
                        review_status = excluded.review_status
                    """,
                    (
                        item.tenant_id,
                        item.evidence_id,
                        item.asset_id,
                        item.chunk_id,
                        item.page,
                        item.section_path,
                        (
                            json.dumps(item.bbox, ensure_ascii=False)
                            if item.bbox is not None
                            else None
                        ),
                        item.char_start,
                        item.char_end,
                        item.quote,
                        item.quote_hash,
                        item.modality,
                        item.confidence,
                        item.extractor,
                        item.extraction_run_id,
                        item.review_status,
                    ),
                )
                self._conn.execute(
                    """
                    DELETE FROM evidence_targets
                    WHERE tenant_id = ? AND evidence_id = ?
                    """,
                    (item.tenant_id, item.evidence_id),
                )
                self._conn.executemany(
                    """
                    INSERT INTO evidence_targets (
                        tenant_id, evidence_id, target_type, target_id,
                        role, position
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.tenant_id,
                            item.evidence_id,
                            target.target_type,
                            target.target_id,
                            target.role,
                            position,
                        )
                        for position, target in enumerate(item.targets)
                    ],
                )
                self.rows_written += 1
        return len(rows)
    # delete_tenant 删除指定租户的所有证据。
    # 由于外键级联，相关的 evidence_targets 也会删除。返回被删除的证据行数。
    def delete_tenant(self, tenant_id: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM evidence WHERE tenant_id = ?", (tenant_id,)
            )
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    #get 按租户和证据 ID 读取一条完整证据：
    # 查 evidence 主体；
    # 查这条证据关联的 targets，并按保存顺序排列；
    # 找到记录时，将数据库行还原为 Evidence 对象；
    # 每成功读取一条正文，quotes_loaded 加 1；
    # 找不到时返回 None。
    def get(self, tenant_id: str, evidence_id: str) -> Evidence | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM evidence
                WHERE tenant_id = ? AND evidence_id = ?
                """,
                (tenant_id, evidence_id),
            ).fetchone()
            if row is None:
                return None
            target_rows = self._conn.execute(
                """
                SELECT target_type, target_id, role
                FROM evidence_targets
                WHERE tenant_id = ? AND evidence_id = ?
                ORDER BY position
                """,
                (tenant_id, evidence_id),
            ).fetchall()
        self.quotes_loaded += 1
        return _row_to_evidence(row, target_rows)

    #get_many 按多个证据 ID 读取证据：
    # 输入中的重复 ID 只查询一次；
    # 找不到的 ID 会跳过；
    # 返回找到的 Evidence 列表。
    # 它内部逐条调用 get，不是一次 SQL 批量读取。
    def get_many(
        self,
        tenant_id: str,
        evidence_ids: Iterable[str],
    ) -> list[Evidence]:
        result: list[Evidence] = []
        seen: set[str] = set()
        for evidence_id in evidence_ids:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            item = self.get(tenant_id, evidence_id)
            if item is not None:
                result.append(item)
        return result

    #exists 检查某个租户下是否存在指定证据 ID。
    # 只查询是否存在，不读取 quote，因此不会增加 quotes_loaded。
    def exists(self, tenant_id: str, evidence_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM evidence
                WHERE tenant_id = ? AND evidence_id = ?
                LIMIT 1
                """,
                (tenant_id, evidence_id),
            ).fetchone()
        return row is not None

    #missing_ids 检查一批证据 ID 中哪些不存在：
    # 去重并排序输入 ID；
    # 每次最多分成 900 个 ID 查询，避免一条 SQL 参数过多；
    # 返回缺失 ID 列表。
    # 它检查存在性，不读取证据正文。
    def missing_ids(
        self,
        tenant_id: str,
        evidence_ids: Iterable[str],
    ) -> list[str]:
        ids = sorted(set(evidence_ids))
        if not ids:
            return []
        missing: list[str] = []
        with self._lock:
            for start in range(0, len(ids), 900):
                chunk = ids[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"""
                    SELECT evidence_id FROM evidence
                    WHERE tenant_id = ? AND evidence_id IN ({placeholders})
                    """,
                    [tenant_id, *chunk],
                ).fetchall()
                found = {row["evidence_id"] for row in rows}
                missing.extend(item for item in chunk if item not in found)
        return missing

    # ids_for_target 按 (tenant_id, target_type, target_id) 查找关联证据 ID，并返回元组。
    # 此查询依赖 evidence_targets 上的目标索引，不会读取 quote。
    def ids_for_target(
        self,
        tenant_id: str,
        target_type: str,
        target_id: str,
    ) -> tuple[str, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT evidence_id FROM evidence_targets
                WHERE tenant_id = ? AND target_type = ? AND target_id = ?
                ORDER BY evidence_id
                """,
                (tenant_id, target_type, target_id),
            ).fetchall()
        return tuple(row["evidence_id"] for row in rows)

    #iter_ids 按证据 ID 顺序返回某租户的所有证据 ID，不读取 quote。
    # 注意：它执行查询时会先 fetchall()，所以 ID 行会先全部载入内存，然后才逐个 yield。
    def iter_ids(self, tenant_id: str) -> Iterator[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT evidence_id FROM evidence
                WHERE tenant_id = ? ORDER BY evidence_id
                """,
                (tenant_id,),
            ).fetchall()
        for row in rows:
            yield row["evidence_id"]

    #count 返回指定租户的证据行数。
    def count(self, tenant_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM evidence WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    #target_count 返回指定租户的证据目标关联行数。
    # 一个证据可能指向多个目标，所以它和证据数量不一定相同。
    def target_count(self, tenant_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_targets WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    #target_keys 返回该租户出现过的不同 (target_type, target_id) 组合，排序后逐个 yield。
    def target_keys(self, tenant_id: str) -> Iterator[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT target_type, target_id
                FROM evidence_targets
                WHERE tenant_id = ?
                ORDER BY target_type, target_id
                """,
                (tenant_id,),
            ).fetchall()
        for row in rows:
            yield (row["target_type"], row["target_id"])

    #iter_evidence 遍历某租户的完整 Evidence 记录，
    # 包括 quote 和 targets，并为每条已读取记录增加 quotes_loaded。
    def iter_evidence(self, tenant_id: str) -> Iterator[Evidence]:
        """Stream full rows.  Only offline validation should call this."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM evidence
                WHERE tenant_id = ? ORDER BY evidence_id
                """,
                (tenant_id,),
            ).fetchall()
            targets = self._conn.execute(
                """
                SELECT evidence_id, target_type, target_id, role
                FROM evidence_targets
                WHERE tenant_id = ?
                ORDER BY evidence_id, position
                """,
                (tenant_id,),
            ).fetchall()
        by_id: dict[str, list[sqlite3.Row]] = {}
        for row in targets:
            by_id.setdefault(row["evidence_id"], []).append(row)
        for row in rows:
            self.quotes_loaded += 1
            yield _row_to_evidence(row, by_id.get(row["evidence_id"], []))

    #explain_target_query 运行 SQLite 的 EXPLAIN QUERY PLAN，
    # 返回“按目标找证据 ID”这类查询的执行计划，用于确认 SQLite 是否使用目标索引。
    # 它用固定的示例参数解释查询计划，不会实际读取任何证据正文。
    def explain_target_query(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT evidence_id FROM evidence_targets
                WHERE tenant_id = ? AND target_type = ? AND target_id = ?
                ORDER BY evidence_id
                """,
                ("tenant", "entity", "target"),
            ).fetchall()
        return [str(tuple(row)) for row in rows]

    #close 关闭 SQLite 连接。调用方不再使用存储时应调用它。
    def close(self) -> None:
        with self._lock:
            self._conn.close()

#_row_to_evidence 把 SQLite 查询结果还原为 Evidence：

# 将 target 行转换为 EvidenceTarget 对象；
# 将 JSON 字符串形式的 bounding box 还原成列表；
# 把其它列填回 Evidence 模型。
# 它只负责转换数据，不执行 SQL 查询。
def _row_to_evidence(
    row: sqlite3.Row,
    target_rows: Iterable[sqlite3.Row],
) -> Evidence:
    targets = [
        EvidenceTarget(
            target_type=target["target_type"],
            target_id=target["target_id"],
            role=target["role"],
        )
        for target in target_rows
    ]
    bbox = json.loads(row["bbox_json"]) if row["bbox_json"] else None
    return Evidence(
        evidence_id=row["evidence_id"],
        tenant_id=row["tenant_id"],
        asset_id=row["asset_id"],
        chunk_id=row["chunk_id"],
        page=row["page"],
        section_path=row["section_path"],
        bbox=bbox,
        char_start=row["char_start"],
        char_end=row["char_end"],
        quote=row["quote"],
        quote_hash=row["quote_hash"],
        modality=row["modality"],
        targets=targets,
        confidence=row["confidence"],
        extractor=row["extractor"],
        extraction_run_id=row["extraction_run_id"],
        review_status=row["review_status"],
    )

#open_store 是一个便捷入口：给它数据库路径，它就返回一个 EvidenceStore。
# 底层仍由 EvidenceStore.__init__ 负责创建目录、连接数据库和初始化表。
def open_store(path: str | Path) -> EvidenceStore:
    """Open (and create if necessary) an evidence database."""

    return EvidenceStore(path)


__all__ = ["EvidenceStore", "open_store"]
