"""Tests for the W7 public-corpus bootstrap converter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.import_public_fault_dataset import (
    ConversionError,
    RawSample,
    _validate_target,
    convert_sample,
    main,
)


def _sample(
    output: dict,
    *,
    text: str,
    index: int = 0,
    context: str = "",
) -> RawSample:
    return RawSample(
        index=index,
        instruction="",
        input_text=text,
        context=context,
        output=output,
    )


def _names(target: dict) -> set[str]:
    return {entity["name"] for entity in target["entities"]}


def test_nested_tree_becomes_fault_events_and_causes() -> None:
    text = (
        "泵出口压力低。入口过滤器堵塞会导致出口压力低。"
        "叶轮汽蚀会导致出口压力低。"
    )
    sample = _sample(
        {
            "top_event": "出口压力低",
            "logic_gate": "OR",
            "children": [
                {
                    "node_name": "入口过滤器堵塞",
                    "type": "Basic Event",
                    "treatment": "清理入口过滤器",
                    "evidence": "入口过滤器堵塞会导致出口压力低",
                },
                {
                    "node_name": "叶轮汽蚀",
                    "type": "Basic Event",
                    "treatment": "更换叶轮",
                    "evidence": "叶轮汽蚀会导致出口压力低",
                },
            ],
        },
        text=text,
    )

    result = convert_sample(sample)

    assert _names(result.target) == {"入口过滤器堵塞", "叶轮汽蚀", "出口压力低"}
    root = next(
        entity
        for entity in result.target["entities"]
        if entity["name"] == "出口压力低"
    )
    assert root["cause_rule"]["op"] == "OR"
    assert len(root["cause_rule"]["children"]) == 2

    by_id = {entity["local_id"]: entity for entity in result.target["entities"]}
    causes = {
        (by_id[relation["source_local_id"]]["name"],
         by_id[relation["target_local_id"]]["name"])
        for relation in result.target["relations"]
    }
    assert causes == {
        ("入口过滤器堵塞", "出口压力低"),
        ("叶轮汽蚀", "出口压力低"),
    }
    for relation in result.target["relations"]:
        assert relation["relation_code"] == "CAUSES"
        assert relation["evidence"]


def test_intermediate_node_borrows_child_evidence() -> None:
    text = "过滤器堵塞会导致出口压力低。"
    sample = _sample(
        {
            "top_event": "出口压力低",
            "logic_gate": "OR",
            "children": [
                {
                    "node_name": "堵塞类故障",
                    "logic_gate": "OR",
                    "children": [
                        {
                            "node_name": "过滤器堵塞",
                            "type": "Basic Event",
                            "evidence": "过滤器堵塞会导致出口压力低",
                        }
                    ],
                }
            ],
        },
        text=text,
    )

    result = convert_sample(sample)

    intermediate = next(
        entity
        for entity in result.target["entities"]
        if entity["name"] == "堵塞类故障"
    )
    assert intermediate["evidence"] == [
        {"quote": "过滤器堵塞会导致出口压力低"}
    ]
    assert any(
        warning.startswith(f"{intermediate['local_id']}:")
        and warning.endswith("evidence-from-child")
        for warning in result.warnings
    )
    assert "cause_rule" not in intermediate


def test_semicolon_joined_quotes_keep_only_grounded_parts() -> None:
    text = "TRIED TO ADJUST IDLE. THEN REMOVED & REPLACED FUEL SERVO."
    sample = _sample(
        {
            "top_event": "怠速无法调节",
            "logic_gate": "OR",
            "children": [
                {
                    "node_name": "燃油伺服器失效",
                    "type": "Basic Event",
                    "evidence": (
                        "TRIED TO ADJUST IDLE; REMOVED & REPLACED FUEL SERVO"
                    ),
                }
            ],
        },
        text=text,
    )

    result = convert_sample(sample)
    entity = next(
        entity
        for entity in result.target["entities"]
        if entity["name"] == "燃油伺服器失效"
    )
    assert entity["evidence"] == [
        {"quote": "TRIED TO ADJUST IDLE"},
        {"quote": "REMOVED & REPLACED FUEL SERVO"},
    ]


def test_corrected_tree_is_unwrapped() -> None:
    text = "过滤器堵塞会导致出口压力低。"
    sample = _sample(
        {
            "is_correct": False,
            "error_analysis": "原树缺少证据",
            "corrected_tree": {
                "top_event": "出口压力低",
                "logic_gate": "OR",
                "children": [
                    {
                        "node_name": "过滤器堵塞",
                        "type": "Basic Event",
                        "evidence": "过滤器堵塞会导致出口压力低",
                    }
                ],
            },
        },
        text=text,
    )

    result = convert_sample(sample)

    assert _names(result.target) == {"出口压力低", "过滤器堵塞"}


def test_ellipsis_joined_quotes_keep_grounded_parts() -> None:
    text = "HAS A CRACK. STOP DRILLED CRACK & FABRICATED PATCH."
    sample = _sample(
        {
            "top_event": "结构裂纹",
            "logic_gate": "OR",
            "children": [
                {
                    "node_name": "裂纹",
                    "type": "Basic Event",
                    "evidence": "HAS A CRACK... STOP DRILLED CRACK & FABRICATED PATCH.",
                }
            ],
        },
        text=text,
    )

    result = convert_sample(sample)

    entity = next(
        entity
        for entity in result.target["entities"]
        if entity["name"] == "裂纹"
    )
    assert entity["evidence"] == [
        {"quote": "HAS A CRACK"},
        {"quote": "STOP DRILLED CRACK & FABRICATED PATCH."},
    ]


def test_ungrounded_child_is_dropped_without_its_edge() -> None:
    text = "入口过滤器堵塞会导致出口压力低。"
    sample = _sample(
        {
            "top_event": "出口压力低",
            "logic_gate": "OR",
            "children": [
                {
                    "node_name": "入口过滤器堵塞",
                    "type": "Basic Event",
                    "evidence": "入口过滤器堵塞会导致出口压力低",
                },
                {
                    "node_name": "叶轮汽蚀",
                    "type": "Basic Event",
                    "evidence": "叶轮汽蚀会导致出口压力低",
                },
            ],
        },
        text=text,
    )

    result = convert_sample(sample)

    assert _names(result.target) == {"入口过滤器堵塞", "出口压力低"}
    assert result.stats["dropped_nodes"] == 1
    assert any(
        "dropped-missing-evidence" in warning for warning in result.warnings
    )
    by_id = {entity["local_id"]: entity for entity in result.target["entities"]}
    assert len(result.target["relations"]) == 1
    relation = result.target["relations"][0]
    assert by_id[relation["source_local_id"]]["name"] == "入口过滤器堵塞"
    assert by_id[relation["target_local_id"]]["name"] == "出口压力低"


def test_treatment_is_not_a_fault_entity_or_target_text() -> None:
    text = "过滤器堵塞会导致出口压力低。"
    sample = _sample(
        {
            "top_event": "出口压力低",
            "logic_gate": "OR",
            "children": [
                {
                    "node_name": "过滤器堵塞",
                    "type": "Basic Event",
                    "treatment": "清理过滤器并更换滤芯",
                    "evidence": "过滤器堵塞会导致出口压力低",
                }
            ],
        },
        text=text,
    )

    result = convert_sample(sample)

    assert "清理过滤器并更换滤芯" not in json.dumps(
        result.target, ensure_ascii=False
    )
    assert all(
        entity["name"] != "清理过滤器并更换滤芯"
        for entity in result.target["entities"]
    )


def test_all_dropped_nodes_raise() -> None:
    sample = _sample(
        {
            "top_event": "不存在的顶层事件",
            "logic_gate": "OR",
            "children": [
                {
                    "node_name": "不存在的子事件",
                    "type": "Basic Event",
                    "evidence": "这段证据不在原文里",
                }
            ],
        },
        text="原文只有这一句话。",
    )

    with pytest.raises(ConversionError, match="every node was dropped"):
        convert_sample(sample)


def test_validate_target_rejects_non_verbatim_quote() -> None:
    target = {
        "entities": [
            {
                "local_id": "a",
                "class": "FaultEvent",
                "name": "事件",
                "evidence": [{"quote": "不是原文"}],
            }
        ],
        "relations": [],
    }
    with pytest.raises(ConversionError, match="not verbatim"):
        _validate_target(target, "原文", sample_id="sample")


def test_cli_writes_chunks_labels_and_report(tmp_path: Path) -> None:
    sources = {
        "version": 1,
        "sources": [
            {
                "id": "test-source",
                "title": "test",
                "public": True,
                "license": "public-dataset",
                "url": None,
            }
        ],
    }
    sources_path = tmp_path / "sources.yml"
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True), encoding="utf-8"
    )
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "instruction": "",
                    "input": "过滤器堵塞会导致出口压力低。",
                    "context": "",
                    "output": {
                        "top_event": "出口压力低",
                        "logic_gate": "OR",
                        "children": [
                            {
                                "node_name": "过滤器堵塞",
                                "type": "Basic Event",
                                "evidence": "过滤器堵塞会导致出口压力低",
                            }
                        ],
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--input",
            str(input_path),
            "--sources",
            str(sources_path),
            "--source-id",
            "test-source",
            "--chunk-dir",
            str(tmp_path / "chunks"),
            "--label-dir",
            str(tmp_path / "labels"),
        ]
    )

    assert exit_code == 0
    chunks = (tmp_path / "chunks" / "chunks.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    labels = (tmp_path / "labels" / "public_labels.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    report = json.loads(
        (tmp_path / "labels" / "public_conversion_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(chunks) == 1
    assert len(labels) == 1
    chunk = json.loads(chunks[0])
    assert chunk["source_id"] == "test-source"
    assert chunk["license"] == "public-dataset"
    assert chunk["text"].startswith("过滤器堵塞")
    assert report["stats"]["samples_converted"] == 1
    assert report["stats"]["entities"] == 2
    assert report["stats"]["relations"] == 1
