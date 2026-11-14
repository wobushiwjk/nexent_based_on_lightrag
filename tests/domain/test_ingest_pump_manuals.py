"""Tests for the pump-manual layout helpers behind W7 manual ingestion.

The public PDFs are not checked in, so these tests exercise the pure parsing
helpers with synthetic word/line fixtures.  Each test pins one layout quirk
that the real manuals exposed: an unlettered single cause, a continuation
line that happens to sit on a heading baseline, a remedy that wraps over
several lines, and prose lists with category headings.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_ingest_module():
    spec = importlib.util.spec_from_file_location(
        "ingest_pump_manuals_under_test",
        ROOT / "scripts" / "ingest_pump_manuals.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``from __future__ import annotations`` makes dataclass resolve string
    # annotations through sys.modules[cls.__module__]; register the module
    # before executing it or that lookup returns None.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


manuals = _load_ingest_module()


def _causes(groups) -> list[list[str]]:
    return [[cause.cause for cause in group] for group in groups]


def test_letter_causes_split_at_each_a() -> None:
    lines = [
        (10.0, "a) 过滤器堵塞。"),
        (12.0, "严重时流量下降。"),
        (20.0, "b) 叶轮磨损。"),
        (30.0, "a) 密封泄漏。"),
    ]

    groups = manuals._letter_causes(lines, page_index=1)

    # a) and b) are causes of the same fault; the next a) starts a new fault.
    assert _causes(groups) == [
        ["过滤器堵塞。严重时流量下降。", "叶轮磨损。"],
        ["密封泄漏。"],
    ]
    assert [cause.page for group in groups for cause in group] == [1, 1, 1]


def test_letter_causes_unlettered_cause_on_heading_baseline_starts_group() -> None:
    lines = [
        (10.0, "a) 过滤器堵塞。"),
        (20.0, "b) 密封泄漏。"),
        (30.0, "电机过载。"),
    ]

    groups = manuals._letter_causes(lines, page_index=2, heading_tops=[30.0])

    assert _causes(groups) == [
        ["过滤器堵塞。", "密封泄漏。"],
        ["电机过载。"],
    ]


def test_letter_causes_continuation_on_heading_baseline_does_not_split() -> None:
    lines = [
        (10.0, "a) 油位过低"),
        (20.0, "导致泵不能正常启动。"),
    ]

    groups = manuals._letter_causes(lines, page_index=1, heading_tops=[20.0])

    assert _causes(groups) == [["油位过低导致泵不能正常启动。"]]


def test_letter_causes_skip_headers_pages_and_footers() -> None:
    lines = [
        (1.0, "原因"),
        (2.0, "2"),
        (5.0, "a) 过滤器堵塞。"),
        (6.0, "内容可有变动。"),
        (7.0, "b) 叶轮磨损。"),
    ]

    groups = manuals._letter_causes(lines, page_index=1)

    assert _causes(groups) == [["过滤器堵塞。", "叶轮磨损。"]]


def test_numbered_causes_split_when_numbering_resets() -> None:
    lines = [
        (10.0, "1. 过滤器堵塞"),
        (12.0, "严重时流量下降。"),
        (20.0, "2. 叶轮磨损"),
        (30.0, "1. 密封泄漏"),
        (40.0, "2. 轴弯曲"),
    ]

    groups = manuals._numbered_causes(lines, page_index=3)

    assert _causes(groups) == [
        ["过滤器堵塞严重时流量下降。", "叶轮磨损"],
        ["密封泄漏", "轴弯曲"],
    ]


def test_attach_remedies_keeps_multiline_remedy_with_its_cause() -> None:
    first = manuals.CauseRow(cause="过滤器堵塞", top=10.0)
    second = manuals.CauseRow(cause="叶轮磨损", top=50.0)
    remedies = [
        manuals.CauseRow(cause="", remedy="清洗过滤器。", top=11.0),
        manuals.CauseRow(cause="", remedy="更换滤芯。", top=20.0),
        manuals.CauseRow(cause="", remedy="更换叶轮。", top=51.0),
    ]

    manuals._attach_remedies([[first, second]], remedies)

    assert first.remedy == "清洗过滤器。更换滤芯。"
    assert second.remedy == "更换叶轮。"


def test_attach_remedies_does_not_cross_pages() -> None:
    cause = manuals.CauseRow(cause="过滤器堵塞", top=10.0, page=1)
    remedies = [
        manuals.CauseRow(cause="", remedy="清洗过滤器。", top=11.0, page=2),
    ]

    manuals._attach_remedies([[cause]], remedies)

    assert cause.remedy == ""


def test_clean_fragment_strips_running_header_noise() -> None:
    assert manuals._clean_fragment("排除方法NC") == ""
    assert manuals._clean_fragment("(CN)") == ""
    assert manuals._clean_fragment("（更换叶轮）") == "更换叶轮"
    assert manuals._clean_fragment("更换密封圈。") == "更换密封圈。"
    # "文中" is the running-header fragment appended after the real "见上".
    assert manuals._clean_fragment("见上文中") == "见上"


def test_coox_causes_reads_circled_list_and_strips_separators() -> None:
    body = (
        "泵振动大。\n"
        "原因：①过滤器堵塞；②、叶轮汽蚀。\n"
        "排除方法：清洗过滤器或更换叶轮。\n"
    )

    causes = manuals._coox_causes(body)

    assert [cause.cause for cause in causes] == ["过滤器堵塞", "叶轮汽蚀"]


def test_coox_causes_joins_wrapped_lines() -> None:
    body = "原因：①密封圈\n损坏；②轴弯曲。\n排除方法：更换密封圈。"

    causes = manuals._coox_causes(body)

    assert [cause.cause for cause in causes] == ["密封圈损坏", "轴弯曲"]


def test_coox_causes_removes_category_headings() -> None:
    body = (
        "原因：一是机械方面：①叶轮磨损；二是水力方面：②进口堵塞。\n"
        "可分别采取下列措施。\n"
    )

    causes = manuals._coox_causes(body)

    assert [cause.cause for cause in causes] == ["叶轮磨损", "进口堵塞"]


def test_coox_causes_recognises_bare_fault_list() -> None:
    body = "该泵常见的故障有：①汽蚀；②气缚。"

    causes = manuals._coox_causes(body)

    assert [cause.cause for cause in causes] == ["汽蚀", "气缚"]


def test_coox_causes_without_a_cause_block_is_empty() -> None:
    body = "检查泵体是否有裂纹。检查密封是否完好。更换损坏的零件。"

    assert manuals._coox_causes(body) == []


def test_build_target_marks_cause_as_causing_fault() -> None:
    target = manuals._build_target("水泵不吸水", "过滤器堵塞")

    assert [entity["local_id"] for entity in target["entities"]] == ["f1", "c1"]
    assert target["entities"][0]["class"] == "FaultEvent"
    assert target["entities"][0]["name"] == "水泵不吸水"
    assert target["entities"][1]["name"] == "过滤器堵塞"
    relation = target["relations"][0]
    assert relation["relation_code"] == "CAUSES"
    assert relation["source_local_id"] == "c1"
    assert relation["target_local_id"] == "f1"


def test_build_target_omits_self_relation() -> None:
    target = manuals._build_target("汽蚀", "汽蚀")

    assert len(target["entities"]) == 2
    assert target["relations"] == []


def test_build_rows_emits_valid_unique_chunks() -> None:
    manual = {
        "id": "test-manual",
        "url": "https://example.invalid/manual.pdf",
        "license": "public-manufacturer-manual",
    }
    faults = [
        manuals.ManualFault(
            fault="水泵不吸水",
            page=1,
            section="水泵不吸水",
            causes=[
                manuals.CauseRow(cause="过滤器堵塞", remedy="清洗过滤器。", page=1),
                manuals.CauseRow(cause="过滤器堵塞", remedy="清洗过滤器。", page=1),
                manuals.CauseRow(cause="进口阀门关闭", remedy="打开阀门。", page=1),
            ],
        )
    ]

    chunks, labels, errors = manuals._build_rows(
        manual, faults, holdout=False
    )

    assert not errors
    assert len(chunks) == 2
    assert [chunk["chunk_id"] for chunk in chunks] == [
        "test-manual-f01-c01",
        "test-manual-f01-c03",
    ]
    assert chunks[0]["text"].splitlines() == [
        "故障现象：水泵不吸水",
        "可能原因：过滤器堵塞",
        "排除方法：清洗过滤器。",
    ]
    assert chunks[0]["holdout"] is False
    assert {label["chunk_id"] for label in labels} == {
        "test-manual-f01-c01",
        "test-manual-f01-c03",
    }
    assert all(
        label["stats"]["relations"] == 1 for label in labels
    )


def test_build_rows_skips_fault_without_a_name() -> None:
    manual = {"id": "test-manual"}
    faults = [
        manuals.ManualFault(
            fault="",
            section="",
            causes=[manuals.CauseRow(cause="过滤器堵塞")],
        )
    ]

    chunks, labels, errors = manuals._build_rows(
        manual, faults, holdout=True
    )

    assert chunks == []
    assert labels == []
    assert errors == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1. 过滤器堵塞", "过滤器堵塞"),
        ("a）密封泄漏", "密封泄漏"),
        ("• 叶轮磨损。", "叶轮磨损"),
        ("  正常原因  ", "正常原因"),
    ],
)
def test_clean_name_removes_list_markers(raw: str, expected: str) -> None:
    assert manuals._clean_name(raw) == expected


def test_chunk_text_omits_empty_remedy() -> None:
    assert manuals._chunk_text("汽蚀", "进口压力低", "") == (
        "故障现象：汽蚀\n可能原因：进口压力低"
    )
