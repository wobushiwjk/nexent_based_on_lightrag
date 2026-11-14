"""Tests for the CFDK standoff importer's pure helpers.

CFDK is downloaded at build time, so the archive is not checked in.  These
tests pin the two properties that make the conversion trustworthy without
the data: the detokenization offset map must keep every standoff span aligned
with the cleaned text, and a label must be emitted only when the fault/cause
orientation is unambiguous.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_cfdk_module():
    spec = importlib.util.spec_from_file_location(
        "ingest_cfdk_under_test", ROOT / "scripts" / "ingest_cfdk.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


cfdk = _load_cfdk_module()


def _span(text: str, quote: str) -> tuple[int, int]:
    start = text.index(quote)
    return start, start + len(quote)


def _annotation(text: str, label: str, quote: str) -> dict:
    start, end = _span(text, quote)
    return {"label": label, "start": start, "end": end}


def test_clean_with_map_detokenizes_and_keeps_offsets_aligned() -> None:
    text = "﻿泵 振动 大， 检查 发现 轴承 磨损 。"

    cleaned, mapping = cfdk.clean_with_map(text)

    assert cleaned == "泵振动大，检查发现轴承磨损。"
    # A standoff span covers the original text, spaces included; the map must
    # project it onto the detokenized quote.
    for original_quote, detokenized in (
        ("泵 振动 大", "泵振动大"),
        ("检查 发现", "检查发现"),
        ("轴承 磨损", "轴承磨损"),
    ):
        start, end = _span(text, original_quote)
        assert cleaned[mapping[start] : mapping[end]] == detokenized


def test_clean_with_map_keeps_latin_spacing() -> None:
    cleaned, _ = cfdk.clean_with_map("型号 NK 65 泵。")

    assert cleaned == "型号 NK 65 泵。"


def test_record_spans_returns_blank_line_separated_records() -> None:
    text = "第一条记录。\n\n 第二条记录。\n"

    spans = list(cfdk.record_spans(text))

    assert [text[start:end] for start, end in spans] == [
        "第一条记录。",
        "第二条记录。",
    ]


def test_parse_standoff_ann_verifies_spans_and_skips_other_labels() -> None:
    text = "泵振动大，轴承磨损。"
    fault_start, fault_end = _span(text, "泵振动大")
    cause_start, cause_end = _span(text, "轴承磨损")
    raw = (
        f"T1\t故障 {fault_start} {fault_end}\t泵振动大\n"
        f"T2\t原因 {cause_start} {cause_end}\t轴承磨损\n"
        f"T3\t方案 {fault_start} {fault_end}\t泵振动大\n"
        f"T4\t原因 0 2\t错误的原文\n"
        "# comment\n"
    ).encode("utf-8")

    annotations, mismatches = cfdk.parse_standoff_ann(raw, text)

    assert annotations == [
        {"label": "故障", "start": fault_start, "end": fault_end},
        {"label": "原因", "start": cause_start, "end": cause_end},
    ]
    assert mismatches == 1


def test_normalize_name_strips_clause_prefixes_and_suffixes() -> None:
    assert cfdk.normalize_name("初步分析为轴承磨损造成的") == "轴承磨损"
    assert cfdk.normalize_name("主要是密封圈老化所致") == "密封圈老化"


def test_normalize_name_replaces_anonymization_placeholders() -> None:
    assert cfdk.normalize_name("两件 company 电机损坏") == "两件公司电机损坏"
    assert cfdk.normalize_name("person 误操作") == "人员误操作"


def test_normalize_name_falls_back_to_the_quote() -> None:
    assert cfdk.normalize_name("由于") == "由于"


def test_build_target_links_each_cause_to_a_single_fault() -> None:
    text = "泵振动大。原因是轴承磨损和润滑不足。"
    cleaned, mapping = cfdk.clean_with_map(text)
    annotations = [
        _annotation(text, "故障", "泵振动大"),
        _annotation(text, "原因", "轴承磨损"),
        _annotation(text, "原因", "润滑不足"),
    ]

    target, warnings, stats = cfdk.build_target(
        text, annotations, mapping, 0, len(text), cleaned
    )

    assert target is not None
    assert [entity["local_id"] for entity in target["entities"]] == [
        "c1",
        "c2",
        "f1",
    ]
    assert [
        (relation["source_local_id"], relation["target_local_id"])
        for relation in target["relations"]
    ] == [("c1", "f1"), ("c2", "f1")]
    assert stats["entities"] == 3
    assert stats["relations"] == 2
    assert warnings == []


def test_build_target_skips_a_cause_contained_in_the_fault() -> None:
    text = "泵振动大。振动是主要原因。"
    cleaned, mapping = cfdk.clean_with_map(text)
    annotations = [
        _annotation(text, "故障", "泵振动大"),
        _annotation(text, "原因", "振动"),
    ]

    target, warnings, _ = cfdk.build_target(
        text, annotations, mapping, 0, len(text), cleaned
    )

    assert target is not None
    assert target["relations"] == []
    assert "relation-skipped:containment" in warnings


def test_build_target_refuses_to_orient_multiple_faults() -> None:
    text = "泵振动大，轴承过热。原因是润滑不足。"
    cleaned, mapping = cfdk.clean_with_map(text)
    annotations = [
        _annotation(text, "故障", "泵振动大"),
        _annotation(text, "故障", "轴承过热"),
        _annotation(text, "原因", "润滑不足"),
    ]

    target, warnings, _ = cfdk.build_target(
        text, annotations, mapping, 0, len(text), cleaned
    )

    assert target is not None
    assert target["relations"] == []
    assert "relations-skipped:multi-fault" in warnings


def test_build_target_skips_a_cause_without_a_fault() -> None:
    text = "原因是轴承磨损。"
    cleaned, mapping = cfdk.clean_with_map(text)
    annotations = [_annotation(text, "原因", "轴承磨损")]

    target, _, _ = cfdk.build_target(
        text, annotations, mapping, 0, len(text), cleaned
    )

    assert target is None


def test_build_target_emits_strict_negative_for_administrative_text() -> None:
    text = "已安排人员前往现场处理。"
    cleaned, mapping = cfdk.clean_with_map(text)

    target, warnings, stats = cfdk.build_target(
        text, [], mapping, 0, len(text), cleaned
    )

    assert target == {"entities": [], "relations": []}
    assert "negative:strict-heuristic" in warnings
    assert stats["entities"] == 0


def test_build_target_never_negatives_text_with_a_fault_cue() -> None:
    text = "已安排处理故障。"

    cleaned, mapping = cfdk.clean_with_map(text)
    target, _, _ = cfdk.build_target(
        text, [], mapping, 0, len(text), cleaned
    )

    assert target is None


def test_build_target_drops_ungrounded_spans() -> None:
    text = "泵振动大。"
    cleaned, mapping = cfdk.clean_with_map(text)
    annotations = [_annotation(text, "故障", "泵振动大")]

    target, warnings, _ = cfdk.build_target(
        "完全不同的文本。", annotations, mapping, 0, len(text), cleaned
    )

    assert target is None
    assert warnings == ["span-not-grounded"]
