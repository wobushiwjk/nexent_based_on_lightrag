"""Tests for the W7 dataset builder and shared label validator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lightrag.fault_tree.distillation.dataset import (
    ChunkRecord,
    DatasetBuildError,
    LabelRecord,
    Split,
    load_chunks,
    merge_chunk_files,
    merge_labels,
    rebalance_train,
    sharegpt_row,
    split_by_asset,
    write_dataset,
)
from lightrag.fault_tree.distillation.labels import (
    StudentLabelError,
    validate_student_target,
)


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in rows
        ),
        encoding="utf-8",
    )
    return path


def _chunk(chunk_id: str, asset_id: str, text: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "asset_id": asset_id,
        "text": text,
        "page": None,
        "section_path": "",
        "modality": "text",
        "source_id": "test",
    }


def _target(local_id: str, name: str, quote: str) -> dict:
    return {
        "entities": [
            {
                "local_id": local_id,
                "class": "FaultEvent",
                "name": name,
                "evidence": [{"quote": quote}],
            }
        ],
        "relations": [],
    }


def test_later_label_file_wins_and_invalid_review_falls_back(
    tmp_path: Path,
) -> None:
    chunks = load_chunks(
        _write_jsonl(
            tmp_path / "chunks.jsonl",
            [
                _chunk("c1", "A", "过滤器堵塞导致压力低。"),
                _chunk("c2", "B", "叶轮汽蚀导致压力低。"),
            ],
        )
    )
    teacher = _write_jsonl(
        tmp_path / "teacher_labels.jsonl",
        [
            {"chunk_id": "c1", "target": _target("a", "教师事件一", "过滤器堵塞")},
            {"chunk_id": "c2", "target": _target("b", "教师事件二", "叶轮汽蚀")},
        ],
    )
    reviewed = _write_jsonl(
        tmp_path / "reviewed_labels.jsonl",
        [
            {
                "chunk_id": "c1",
                "target": _target("a", "复核事件一", "不在原文里的引文"),
            },
            {"chunk_id": "c2", "target": _target("b", "复核事件二", "叶轮汽蚀")},
        ],
    )

    records, errors = merge_labels(
        [(teacher, "teacher"), (reviewed, "reviewed")], chunks
    )

    by_id = {record.chunk_id: record for record in records}
    assert by_id["c1"].origin == "teacher"
    assert by_id["c1"].target["entities"][0]["name"] == "教师事件一"
    assert by_id["c2"].origin == "reviewed"
    assert by_id["c2"].target["entities"][0]["name"] == "复核事件二"
    assert len(errors) == 1
    assert "not verbatim" in errors[0]["error"]


def test_split_by_asset_never_leaks_between_splits(tmp_path: Path) -> None:
    rows = []
    labels = []
    for index in range(10):
        asset_id = f"ASSET-{index:02d}"
        chunk_id = f"{asset_id}:chunk"
        text = f"故障{index}导致停机。"
        rows.append(_chunk(chunk_id, asset_id, text))
        labels.append(
            {
                "chunk_id": chunk_id,
                "target": _target("e", f"故障{index}", f"故障{index}"),
            }
        )
    chunks = load_chunks(_write_jsonl(tmp_path / "chunks.jsonl", rows))
    label_path = _write_jsonl(tmp_path / "labels.jsonl", labels)
    records, errors = merge_labels([(label_path, "public")], chunks)
    assert not errors

    split = split_by_asset(
        records, chunks, val_ratio=0.2, test_ratio=0.2, seed=7
    )

    assert len(split.train) + len(split.val) + len(split.test) == 10
    assert len(split.assets["train"]) == 6
    assert len(split.assets["val"]) == 2
    assert len(split.assets["test"]) == 2
    all_assets = (
        set(split.assets["train"])
        | set(split.assets["val"])
        | set(split.assets["test"])
    )
    assert len(all_assets) == 10
    for record in [*split.train, *split.val, *split.test]:
        assert chunks[record.chunk_id].asset_id in all_assets

    again = split_by_asset(
        records, chunks, val_ratio=0.2, test_ratio=0.2, seed=7
    )
    assert again.assets == split.assets


def test_merge_chunk_files_combines_sources_and_rejects_collisions(
    tmp_path: Path,
) -> None:
    first = _write_jsonl(
        tmp_path / "cfdk_chunks.jsonl",
        [
            _chunk("c1", "ASSET-A", "过滤器堵塞导致压力低。"),
            _chunk("c2", "ASSET-B", "叶轮汽蚀导致压力低。"),
        ],
    )
    second = _write_jsonl(
        tmp_path / "manual_chunks.jsonl",
        [_chunk("c3", "MANUAL-A", "入口压力过低导致汽蚀。")],
    )

    merged = merge_chunk_files([first, second])

    assert sorted(merged) == ["c1", "c2", "c3"]
    assert merged["c3"].asset_id == "MANUAL-A"

    collision = _write_jsonl(
        tmp_path / "collision.jsonl",
        [_chunk("c1", "ASSET-C", "重复的块编号。")],
    )
    with pytest.raises(DatasetBuildError, match="duplicate chunk_id"):
        merge_chunk_files([first, collision])


def test_split_by_asset_pins_forced_domain_assets(tmp_path: Path) -> None:
    rows = []
    labels = []
    for index in range(10):
        asset_id = f"ASSET-{index:02d}"
        chunk_id = f"{asset_id}:chunk"
        text = f"故障{index}导致停机。"
        rows.append(_chunk(chunk_id, asset_id, text))
        labels.append(
            {
                "chunk_id": chunk_id,
                "target": _target("e", f"故障{index}", f"故障{index}"),
            }
        )
    chunks = load_chunks(_write_jsonl(tmp_path / "chunks.jsonl", rows))
    label_path = _write_jsonl(tmp_path / "labels.jsonl", labels)
    records, errors = merge_labels([(label_path, "public")], chunks)
    assert not errors

    split = split_by_asset(
        records,
        chunks,
        val_ratio=0.2,
        test_ratio=0.2,
        seed=7,
        forced_splits={"ASSET-00": "val", "ASSET-01": "test"},
    )

    assert "ASSET-00" in split.assets["val"]
    assert "ASSET-01" in split.assets["test"]
    all_assets = (
        set(split.assets["train"])
        | set(split.assets["val"])
        | set(split.assets["test"])
    )
    assert len(all_assets) == 10
    assert len(split.assets["val"]) + len(split.assets["test"]) == 4


def test_forced_split_unknown_asset_is_an_error(tmp_path: Path) -> None:
    chunks = load_chunks(
        _write_jsonl(
            tmp_path / "chunks.jsonl",
            [_chunk("c1", "ASSET-A", "过滤器堵塞导致压力低。")],
        )
    )
    labels = _write_jsonl(
        tmp_path / "labels.jsonl",
        [
            {
                "chunk_id": "c1",
                "target": _target("e", "过滤器堵塞", "过滤器堵塞"),
            }
        ],
    )
    records, _ = merge_labels([(labels, "public")], chunks)

    with pytest.raises(DatasetBuildError, match="forced assets not in"):
        split_by_asset(
            records,
            chunks,
            forced_splits={"ASSET-MISSING": "val"},
        )


def test_manifest_records_chunk_files_and_forced_splits(
    tmp_path: Path,
) -> None:
    rows = [
        _chunk("c1", "ASSET-A", "过滤器堵塞导致压力低。"),
        _chunk("c2", "ASSET-B", "叶轮汽蚀导致压力低。"),
        _chunk("c3", "ASSET-C", "入口压力过低导致汽蚀。"),
        _chunk("c4", "ASSET-D", "密封泄漏导致压力低。"),
    ]
    chunks_path = _write_jsonl(tmp_path / "chunks.jsonl", rows)
    chunks = load_chunks(chunks_path)
    labels = _write_jsonl(
        tmp_path / "labels.jsonl",
        [
            {
                "chunk_id": row["chunk_id"],
                "target": _target(
                    "e", row["text"][:4], row["text"][:4]
                ),
            }
            for row in rows
        ],
    )
    records, errors = merge_labels([(labels, "public")], chunks)
    split = split_by_asset(
        records,
        chunks,
        val_ratio=0.25,
        test_ratio=0.25,
        seed=3,
        forced_splits={"ASSET-A": "val"},
    )

    manifest = write_dataset(
        split,
        chunks,
        "SYS",
        tmp_path / "out",
        seed=3,
        val_ratio=0.25,
        test_ratio=0.25,
        label_files=[(labels, "public")],
        errors=errors,
        chunk_files=[chunks_path],
        forced_splits={"ASSET-A": "val"},
    )

    assert manifest["chunk_files"] == [str(chunks_path)]
    assert manifest["forced_splits"] == {"ASSET-A": "val"}
    assert "ASSET-A" in manifest["assets"]["val"]


def _record(chunk_id: str, origin: str = "public") -> LabelRecord:
    return LabelRecord(
        chunk_id=chunk_id,
        target={"entities": [], "relations": []},
        origin=origin,
        source_path="labels.jsonl",
    )


def _mix_fixture() -> tuple[Split, dict[str, ChunkRecord]]:
    chunks: dict[str, ChunkRecord] = {}
    train: list[LabelRecord] = []
    for index in range(3):
        chunk_id = f"m{index}"
        chunks[chunk_id] = ChunkRecord(
            chunk_id=chunk_id,
            asset_id=f"MANUAL-{index}",
            text=f"手册故障{index}。",
            source_id="manual",
        )
        train.append(_record(chunk_id))
    for index in range(4):
        chunk_id = f"c{index}"
        chunks[chunk_id] = ChunkRecord(
            chunk_id=chunk_id,
            asset_id=f"CFDK-{index}",
            text=f"压缩机故障{index}。",
            source_id="cfdk",
        )
        train.append(_record(chunk_id))
    split = Split(
        train=train,
        val=[_record("m0")],
        test=[_record("c0")],
        assets={"train": ["M", "C"], "val": ["M"], "test": ["C"]},
    )
    return split, chunks


def test_rebalance_train_repeats_and_caps_by_source() -> None:
    split, chunks = _mix_fixture()

    balanced = rebalance_train(
        split,
        chunks,
        repeats={"manual": 3},
        caps={"cfdk": 2},
        seed=7,
    )

    train_ids = [record.chunk_id for record in balanced.train]
    assert train_ids.count("m0") == 3
    assert train_ids.count("m1") == 3
    assert sum(train_ids.count(f"c{index}") for index in range(4)) == 2
    assert len(balanced.val) == len(split.val)
    assert len(balanced.test) == len(split.test)
    assert balanced.assets == split.assets


def test_rebalance_train_cap_is_deterministic() -> None:
    split, chunks = _mix_fixture()

    first = rebalance_train(split, chunks, caps={"cfdk": 2}, seed=11)
    second = rebalance_train(split, chunks, caps={"cfdk": 2}, seed=11)

    assert [record.chunk_id for record in first.train] == [
        record.chunk_id for record in second.train
    ]


def test_rebalance_train_rejects_unknown_and_conflicting_sources() -> None:
    split, chunks = _mix_fixture()

    with pytest.raises(DatasetBuildError, match="not in the train split"):
        rebalance_train(split, chunks, repeats={"missing": 2})
    with pytest.raises(DatasetBuildError, match="both repeated and capped"):
        rebalance_train(
            split, chunks, repeats={"manual": 2}, caps={"manual": 2}
        )
    with pytest.raises(DatasetBuildError, match="count must be >= 1"):
        rebalance_train(split, chunks, repeats={"manual": 0})


def test_manifest_records_train_mix_and_unique_rows(tmp_path: Path) -> None:
    split, chunks = _mix_fixture()
    balanced = rebalance_train(
        split, chunks, repeats={"manual": 2}, seed=5
    )

    manifest = write_dataset(
        balanced,
        chunks,
        "SYS",
        tmp_path / "out",
        seed=5,
        val_ratio=0.1,
        test_ratio=0.1,
        label_files=[],
        errors=[],
        train_mix={"repeats": {"manual": 2}, "caps": {}, "seed": 5},
    )

    assert manifest["train_mix"] == {
        "repeats": {"manual": 2},
        "caps": {},
        "seed": 5,
    }
    assert manifest["stats"]["train"] == 10
    assert manifest["stats"]["train_unique"] == 7
    assert len(
        (tmp_path / "out" / "train.jsonl").read_text(encoding="utf-8").splitlines()
    ) == 10


def test_forward_cause_rule_reference_is_valid() -> None:
    text = "过滤器堵塞和密封泄漏导致压力低。"
    target = {
        "entities": [
            {
                "local_id": "parent",
                "class": "FaultEvent",
                "name": "压力低",
                "cause_rule": {
                    "op": "OR",
                    "children": [
                        {"event_local_id": "child"},
                        {"event_local_id": "child2"},
                    ],
                },
                "evidence": [{"quote": "压力低"}],
            },
            {
                "local_id": "child",
                "class": "FaultEvent",
                "name": "过滤器堵塞",
                "evidence": [{"quote": "过滤器堵塞"}],
            },
            {
                "local_id": "child2",
                "class": "FaultEvent",
                "name": "密封泄漏",
                "evidence": [{"quote": "密封泄漏"}],
            },
        ],
        "relations": [
            {
                "relation_code": "CAUSES",
                "source_local_id": "child",
                "target_local_id": "parent",
                "evidence": [{"quote": "过滤器堵塞和密封泄漏导致压力低"}],
            },
            {
                "relation_code": "CAUSES",
                "source_local_id": "child2",
                "target_local_id": "parent",
                "evidence": [{"quote": "过滤器堵塞和密封泄漏导致压力低"}],
            },
        ],
    }

    validate_student_target(target, text, sample_id="forward")
    with pytest.raises(StudentLabelError, match="invalid AND/OR"):
        validate_student_target(
            {
                "entities": [
                    {
                        "local_id": "parent",
                        "class": "FaultEvent",
                        "name": "压力低",
                        "cause_rule": {
                            "op": "OR",
                            "children": [{"event_local_id": "child"}],
                        },
                        "evidence": [{"quote": "压力低"}],
                    },
                    {
                        "local_id": "child",
                        "class": "FaultEvent",
                        "name": "过滤器堵塞",
                        "evidence": [{"quote": "过滤器堵塞"}],
                    },
                ],
                "relations": [],
            },
            text,
            sample_id="single-child",
        )


def test_negative_label_is_trainable(tmp_path: Path) -> None:
    chunks = load_chunks(
        _write_jsonl(
            tmp_path / "chunks.jsonl",
            [_chunk("c1", "A", "清理过滤器并更换滤芯。")],
        )
    )
    labels = _write_jsonl(
        tmp_path / "labels.jsonl",
        [{"chunk_id": "c1", "target": {"entities": [], "relations": []}}],
    )
    records, errors = merge_labels([(labels, "public")], chunks)

    assert not errors
    assert len(records) == 1
    assert records[0].target == {"entities": [], "relations": []}


def test_sharegpt_row_and_manifest(tmp_path: Path) -> None:
    chunks = load_chunks(
        _write_jsonl(
            tmp_path / "chunks.jsonl",
            [
                _chunk("c1", "A", "过滤器堵塞导致压力低。"),
                _chunk("c2", "B", "清理过滤器。"),
            ],
        )
    )
    labels = _write_jsonl(
        tmp_path / "labels.jsonl",
        [
            {"chunk_id": "c1", "target": _target("e", "过滤器堵塞", "过滤器堵塞")},
            {"chunk_id": "c2", "target": {"entities": [], "relations": []}},
        ],
    )
    records, errors = merge_labels([(labels, "public")], chunks)
    split = split_by_asset(
        records, chunks, val_ratio=0.5, test_ratio=0.0, seed=1
    )
    row = sharegpt_row(split.train[0], chunks[split.train[0].chunk_id], "SYS")

    assert row["system"] == "SYS"
    assert row["conversations"][0]["from"] == "human"
    assert "过滤器堵塞" in row["conversations"][0]["value"]
    assert json.loads(row["conversations"][1]["value"]) == split.train[0].target

    manifest = write_dataset(
        split,
        chunks,
        "SYS",
        tmp_path / "out",
        seed=1,
        val_ratio=0.5,
        test_ratio=0.0,
        label_files=[(labels, "public")],
        errors=errors,
    )

    assert (tmp_path / "out" / "train.jsonl").exists()
    assert (tmp_path / "out" / "val.jsonl").exists()
    assert (tmp_path / "out" / "manifest.json").exists()
    assert manifest["stats"]["labels"] == 2
    assert manifest["stats"]["negatives"] == 1
    assert manifest["system_prompt_sha256"] == hashlib.sha256(
        b"SYS"
    ).hexdigest()


def test_unknown_chunk_is_an_error(tmp_path: Path) -> None:
    chunks = load_chunks(
        _write_jsonl(
            tmp_path / "chunks.jsonl",
            [_chunk("c1", "A", "过滤器堵塞导致压力低。")],
        )
    )
    labels = _write_jsonl(
        tmp_path / "labels.jsonl",
        [{"chunk_id": "missing", "target": {"entities": [], "relations": []}}],
    )

    with pytest.raises(DatasetBuildError, match="unknown chunk"):
        merge_labels([(labels, "public")], chunks, strict=True)
