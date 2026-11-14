#!/usr/bin/env python3
"""Convert public Chinese pump-manual fault tables into W7 JSONL.

The structured manuals put a fault, its causes and a remedy in adjacent table
columns.  This importer reads those columns by x-coordinate (``pdfplumber``),
turns every cause row into one training chunk, and emits a conservative
``FaultEvent + CAUSES`` target.  The remedy stays in the chunk text but never
becomes an entity: maintenance actions are not causes.

Supported layouts are deliberately narrow and versioned by the ``format`` key
in ``training/corpus/manual_sources.yml``:

``grundfos_letter``
    Fault headings ``1.`` and lettered causes ``a)``/``a）``; used by the
    Grundfos NK and S service manuals.
``grundfos_section``
    Fault headings ``8.1`` followed by ``原因 | 纠正方法``; used by the
    Grundfos installation-and-operation manuals.
``tianjin_table``
    Three columns ``故障 | 产生原因 | 排除方法`` with numbered rows that reset
    at ``1.`` for every fault; used by the Tianjin water-plant standard.
``coox_sections``
    Prose sections ``1、...`` with ``原因：`` lists; used by the Coox pump
    maintenance handbook.

The archive is read in place; nothing is extracted or executed.  Run from the
repository root:

    python scripts/ingest_pump_manuals.py

The default outputs are gitignored:
``training/corpus/parsed/manual_chunks.jsonl``,
``training/data/manual_bootstrap_labels.jsonl`` and
``training/data/manual_report.json``.  Manuals marked ``holdout: true`` are
written to ``training/data/manual_holdout_*.jsonl`` instead so the evaluation
set cannot leak into training.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lightrag.fault_tree.distillation.labels import (  # noqa: E402
    StudentLabelError,
    validate_student_target,
)

DEFAULT_SPEC = REPO_ROOT / "training/corpus/manual_sources.yml"
DEFAULT_CHUNKS = REPO_ROOT / "training/corpus/parsed/manual_chunks.jsonl"
DEFAULT_LABELS = REPO_ROOT / "training/data/manual_bootstrap_labels.jsonl"
DEFAULT_REPORT = REPO_ROOT / "training/data/manual_report.json"
DEFAULT_HOLDOUT_CHUNKS = REPO_ROOT / "training/data/manual_holdout_chunks.jsonl"
DEFAULT_HOLDOUT_LABELS = REPO_ROOT / "training/data/manual_holdout_labels.jsonl"

_HEADER_TEXTS = {"原因", "纠正方法", "故障", "产生原因", "排除方法", "处理方法"}
_PAGE_NOISE = re.compile(r"^(?:\d+|中文\s*\(CN\)|中文版本)$")
_FOOTER_NOISE = re.compile(r"^内容可?有?变动[。.]?$")
# Running headers are split into floating fragments by the PDF text layer.
# They are not content; strip them before pairing causes with remedies.
_HEADER_FRAGMENT = re.compile(
    r"^(?:[)）(（]?\s*(?:排除方法|纠正方法)\s*(?:NC)?\s*[)）(（]?\s*文中?\s*|"
    r"文中|NC|中文|\(?CN\)?|[)）(（])\s*$"
)
_EXPLANATION = re.compile(r"^(?:泵的实际工作点|结果是|其结果是)")


@dataclass
class CauseRow:
    """One cause and the remedy printed beside it."""

    cause: str
    remedy: str = ""
    page: int = 0
    top: float = 0.0


@dataclass
class ManualFault:
    """One fault section extracted from a manual table."""

    fault: str
    page: int = 0
    section: str = ""
    causes: list[CauseRow] = field(default_factory=list)
    start_top: float = 0.0


def _clean_text(value: str) -> str:
    value = value.replace("　", " ").replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def _clean_name(value: str) -> str:
    value = _clean_text(value)
    value = re.sub(
        r"^(?:\d+[.．、]\s*|[a-z][）)]\s*|[•·]\s*)", "", value
    )
    value = value.strip("，,。；;：: ")
    return value if len(value) <= 100 else value[:97] + "..."


def _require_pdfplumber():
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "pdfplumber is required for manual ingestion; "
            "install it with `pip install pdfplumber`"
        ) from exc
    return pdfplumber


def _lines_by_top(words: list[dict[str, Any]], tol: float = 2.5):
    """Group words into visual lines and sort each line by x."""
    words = sorted(words, key=lambda word: (word["top"], word["x0"]))
    lines: list[tuple[float, list[dict[str, Any]]]] = []
    current: list[dict[str, Any]] = []
    top: float | None = None
    for word in words:
        if top is None or abs(word["top"] - top) <= tol:
            current.append(word)
            top = word["top"] if top is None else top
        else:
            lines.append((top, sorted(current, key=lambda item: item["x0"])))
            current = [word]
            top = word["top"]
    if current:
        lines.append((top or 0.0, sorted(current, key=lambda item: item["x0"])))
    return lines


def _columns(
    page: Any,
    bounds: dict[str, tuple[float, float]],
    *,
    max_size: float = 16,
) -> dict[str, list[tuple[float, str]]]:
    """Split a page into text columns by each word's left edge."""
    words = page.extract_words(extra_attrs=["size"])
    words = [
        word
        for word in words
        if word["size"] <= max_size and word["x0"] < page.width - 15
    ]
    out: dict[str, list[tuple[float, str]]] = {key: [] for key in bounds}
    for top, line_words in _lines_by_top(words):
        for key, (low, high) in bounds.items():
            segment = [word for word in line_words if low <= word["x0"] < high]
            if segment:
                out[key].append((top, "".join(word["text"] for word in segment)))
    return out


def _numbered_headings(
    lines: list[tuple[float, str]], expected: int
) -> tuple[list[str], list[float], int]:
    """Read ``1.``/``2.`` headings, skipping section titles before them."""
    headings: list[str] = []
    tops: list[float] = []
    current: list[str] = []
    current_top = 0.0
    started = False
    for top, text in lines:
        value = _clean_text(text)
        if not value or _PAGE_NOISE.match(value):
            continue
        match = re.match(r"^(\d+)[.．]\s*(.*)$", value)
        if match:
            number = int(match.group(1))
            body = match.group(2)
            if number == expected:
                if current:
                    headings.append("".join(current))
                    tops.append(current_top)
                current = [body]
                current_top = top
                expected += 1
                started = True
                continue
            if started and number < expected:
                continue
            if number > expected:
                if started:
                    break
                continue
        if current:
            current.append(value)
    if current:
        headings.append("".join(current))
        tops.append(current_top)
    return headings, tops, expected


def _letter_causes(
    lines: list[tuple[float, str]],
    page_index: int,
    heading_tops: list[float] | None = None,
) -> list[list[CauseRow]]:
    """Read lettered causes and split the list at every new ``a)``.

    A fault with a single cause is sometimes printed without a letter, on the
    same baseline as its heading (Grundfos fault 7).  ``heading_tops`` lets
    that line start its own fault group instead of continuing the previous
    cause's sentence.
    """
    heading_tops = heading_tops or []
    groups: list[list[CauseRow]] = []
    current: list[CauseRow] = []
    item: CauseRow | None = None
    for top, text in lines:
        value = _clean_text(text)
        if (
            not value
            or value in _HEADER_TEXTS
            or _PAGE_NOISE.match(value)
            or _FOOTER_NOISE.match(value)
        ):
            continue
        match = re.match(r"^([a-z])[）)]\s*", value)
        if match:
            letter = match.group(1)
            if letter == "a":
                if item is not None:
                    current.append(item)
                    item = None
                if current:
                    groups.append(current)
                    current = []
            elif item is not None:
                current.append(item)
            item = CauseRow(
                cause=re.sub(r"^[a-z][）)]\s*", "", value),
                page=page_index,
                top=top,
            )
        elif (
            item is not None
            and item.cause.endswith(("。", "！", "？"))
            and any(abs(top - heading_top) <= 3 for heading_top in heading_tops)
        ):
            current.append(item)
            item = CauseRow(cause=value, page=page_index, top=top)
            groups.append(current)
            current = []
        elif item is not None:
            item.cause += value
    if item is not None:
        current.append(item)
    if current:
        groups.append(current)
    return groups


def _numbered_causes(
    lines: list[tuple[float, str]], page_index: int
) -> list[list[CauseRow]]:
    """Read ``1.`` rows and split the list whenever numbering resets."""
    groups: list[list[CauseRow]] = []
    current: list[CauseRow] = []
    item: CauseRow | None = None
    for top, text in lines:
        value = _clean_text(text)
        if (
            not value
            or value in _HEADER_TEXTS
            or _PAGE_NOISE.match(value)
            or _FOOTER_NOISE.match(value)
        ):
            continue
        match = re.match(r"^(\d+)[.．、]\s*(.*)$", value)
        if match:
            if match.group(1) == "1" and current:
                if item is not None:
                    current.append(item)
                    item = None
                groups.append(current)
                current = []
            elif item is not None:
                current.append(item)
            item = CauseRow(cause=match.group(2), page=page_index, top=top)
        elif item is not None:
            item.cause += value
    if item is not None:
        current.append(item)
    if current:
        groups.append(current)
    return groups


def _split_at_heading_tops(
    groups: list[list[CauseRow]], tops: list[float]
) -> list[list[CauseRow]]:
    """Split a group when a heading sits exactly on one of its cause rows.

    Grundfos sometimes prints the last fault heading on the same baseline as
    its only cause; the cause then has no letter marker and would otherwise be
    appended to the previous fault.
    """
    if len(groups) >= len(tops):
        return groups
    for heading_top in tops:
        for index, group in enumerate(groups):
            split_at = next(
                (
                    position
                    for position, cause in enumerate(group)
                    if position > 0 and abs(cause.top - heading_top) <= 3
                ),
                None,
            )
            if split_at is not None:
                groups[index : index + 1] = [
                    group[:split_at],
                    group[split_at:],
                ]
                break
    return groups


def _clean_fragment(value: str) -> str:
    """Remove a running-header fragment while keeping genuine remedy text."""
    value = _clean_text(value)
    value = re.sub(r"^[)）(（]\s*", "", value)
    value = re.sub(r"\s*[)）(（]\s*$", "", value)
    value = re.sub(r"^(?:排除方法|纠正方法)\s*(?:NC)?\s*", "", value)
    value = re.sub(r"\s*文中\s*$", "", value)
    value = re.sub(r"^(?:NC|中文|CN)\s*$", "", value)
    return value.strip(" ，,；;：:")


def _remedy_lines(
    lines: list[tuple[float, str]], page_index: int
) -> list[CauseRow]:
    """Treat every remedy-column line as a remedy fragment."""
    out: list[CauseRow] = []
    for top, text in lines:
        value = _clean_text(text)
        if (
            not value
            or value in _HEADER_TEXTS
            or _PAGE_NOISE.match(value)
            or _HEADER_FRAGMENT.match(value)
        ):
            continue
        value = re.sub(r"^(?:[•·]|\d+[.．])\s*", "", value)
        value = _clean_fragment(value)
        if value:
            out.append(CauseRow(cause="", remedy=value, page=page_index, top=top))
    return out


def _attach_remedies(
    groups: list[list[CauseRow]],
    remedies: list[CauseRow],
    *,
    anchor_tol: float = 15,
) -> None:
    """Attach remedy lines in reading order, anchored on cause row tops.

    A cause can own several remedy lines, and the remedy column is sometimes
    shifted a few points from the cause column.  The first line near a cause
    anchors that cause; lines before the next anchor continue it.  This keeps
    a multi-line remedy from leaking into the following cause.
    """
    causes = [cause for group in groups for cause in group]
    current: CauseRow | None = None
    for remedy in remedies:
        if current is not None and remedy.page != current.page:
            current = None
        candidates = [
            cause
            for cause in causes
            if cause.page == remedy.page
            and abs(cause.top - remedy.top) <= anchor_tol
        ]
        if candidates:
            current = min(candidates, key=lambda cause: abs(cause.top - remedy.top))
        if current is None:
            continue
        current.remedy = f"{current.remedy}{remedy.remedy}".strip()


def parse_grundfos_letter(path: Path, pages: list[int]) -> list[ManualFault]:
    """Parse the Grundfos NK/S service-manual fault tables."""
    pdfplumber = _require_pdfplumber()
    headings: list[str] = []
    heading_tops: list[float] = []
    groups: list[list[CauseRow]] = []
    remedies: list[CauseRow] = []
    expected = 1
    with pdfplumber.open(path) as pdf:
        for page_index in pages:
            page = pdf.pages[page_index - 1]
            columns = _columns(
                page,
                {
                    "fault": (0, 150),
                    "cause": (150, 320),
                    "remedy": (320, page.width - 15),
                },
            )
            page_headings, page_tops, expected = _numbered_headings(
                columns["fault"], expected
            )
            headings.extend(page_headings)
            heading_tops.extend(page_tops)
            groups.extend(
                _letter_causes(columns["cause"], page_index, page_tops)
            )
            remedies.extend(_remedy_lines(columns["remedy"], page_index))
    groups = _split_at_heading_tops(groups, heading_tops)
    _attach_remedies(groups, remedies)
    if len(headings) != len(groups):
        raise ValueError(
            f"{path.name}: {len(headings)} headings but {len(groups)} cause "
            f"groups on pages {pages}"
        )
    return [
        ManualFault(
            fault=_clean_name(heading),
            page=page_index,
            section=_clean_name(heading),
            causes=group,
        )
        for heading, group in zip(headings, groups)
        for page_index in [group[0].page if group else 0]
    ]


def parse_grundfos_section(path: Path, pages: list[int]) -> list[ManualFault]:
    """Parse ``8.x`` sections with a ``原因 | 纠正方法`` two-column table.

    Cause and remedy rows carry no ruling lines, so the section is rebuilt
    from reading order: a ``8.x`` heading opens a fault, every following
    non-explanation line in the left column is one cause, and remedy lines
    are anchored to the cause printed on the same baseline.  The two
    ``泵的实际工作点...`` paragraphs are consequences, not causes; dropping
    them keeps the entity set a fault list rather than a narrative.
    """
    pdfplumber = _require_pdfplumber()
    left_lines: list[tuple[int, float, str]] = []
    remedies: list[CauseRow] = []
    with pdfplumber.open(path) as pdf:
        for page_index in pages:
            page = pdf.pages[page_index - 1]
            columns = _columns(
                page,
                {
                    "left": (0, 200),
                    "remedy": (200, page.width - 15),
                },
            )
            for top, text in columns["left"]:
                value = _clean_text(text)
                if (
                    not value
                    or value in _HEADER_TEXTS
                    or _PAGE_NOISE.match(value)
                    or _HEADER_FRAGMENT.match(value)
                    or _FOOTER_NOISE.match(value)
                ):
                    continue
                left_lines.append((page_index, top, value))
            for top, text in columns["remedy"]:
                value = _clean_text(text)
                if (
                    not value
                    or value in _HEADER_TEXTS
                    or _PAGE_NOISE.match(value)
                    or _HEADER_FRAGMENT.match(value)
                ):
                    continue
                value = re.sub(r"^(?:[•·]|\d+[.．])\s*", "", value)
                value = _clean_fragment(value)
                if value:
                    remedies.append(
                        CauseRow(cause="", remedy=value, page=page_index, top=top)
                    )

    faults: list[ManualFault] = []
    current: ManualFault | None = None
    for page_index, top, value in left_lines:
        heading = re.match(r"^\d+\.\d+\s*(.*)$", value)
        if heading:
            current = ManualFault(
                fault=_clean_name(heading.group(1)),
                page=page_index,
                section=_clean_name(heading.group(1)),
                start_top=top,
            )
            faults.append(current)
            continue
        if current is None or _EXPLANATION.match(value):
            continue
        current.causes.append(CauseRow(cause=value, page=page_index, top=top))

    for index, fault in enumerate(faults):
        if index + 1 < len(faults):
            next_fault = faults[index + 1]
            end = (next_fault.page, next_fault.start_top)
        else:
            end = (10**9, float("inf"))
        subset = [
            remedy
            for remedy in remedies
            if (remedy.page, remedy.top) >= (fault.page, fault.start_top)
            and (remedy.page, remedy.top) < end
        ]
        _attach_remedies([fault.causes], subset, anchor_tol=6)
    return [fault for fault in faults if fault.causes]


def parse_tianjin_table(path: Path, pages: list[int]) -> list[ManualFault]:
    """Parse ``故障 | 产生原因 | 排除方法`` tables with numbered rows."""
    pdfplumber = _require_pdfplumber()
    headings: list[str] = []
    heading_tops: list[float] = []
    cause_groups: list[list[CauseRow]] = []
    remedy_groups: list[list[CauseRow]] = []
    with pdfplumber.open(path) as pdf:
        for page_index in pages:
            page = pdf.pages[page_index - 1]
            columns = _columns(
                page,
                {
                    "fault": (0, 190),
                    "cause": (190, 320),
                    "remedy": (320, page.width - 15),
                },
                max_size=15,
            )
            start_top = 0.0
            if page_index == pages[0]:
                for top, text in columns["fault"]:
                    if "故障" in text and "原因" in text:
                        start_top = top
                        break
                for top, text in columns["cause"]:
                    if "产生原因" in text or (
                        "原因" in text and "故障" not in text and top >= start_top
                    ):
                        start_top = max(start_top, top)
                        break
            fault_lines = [
                (top, text)
                for top, text in columns["fault"]
                if top >= start_top
                and not _PAGE_NOISE.match(_clean_text(text))
                and text.strip() not in _HEADER_TEXTS
            ]
            clusters: list[list[tuple[float, str]]] = []
            for top, text in fault_lines:
                value = _clean_text(text)
                if not value or "续表" in value:
                    continue
                if clusters and top - clusters[-1][-1][0] <= 15:
                    clusters[-1].append((top, value))
                else:
                    clusters.append([(top, value)])
            headings.extend("".join(item[1] for item in cluster) for cluster in clusters)
            heading_tops.extend(cluster[0][0] for cluster in clusters)
            cause_groups.extend(
                _numbered_causes(
                    [
                        (top, text)
                        for top, text in columns["cause"]
                        if top >= start_top
                    ],
                    page_index,
                )
            )
            remedy_groups.extend(
                _numbered_causes(
                    [
                        (top, text)
                        for top, text in columns["remedy"]
                        if top >= start_top
                    ],
                    page_index,
                )
            )
    if len(headings) != len(cause_groups) or len(headings) != len(remedy_groups):
        raise ValueError(
            f"{path.name}: {len(headings)} headings, {len(cause_groups)} cause "
            f"groups, {len(remedy_groups)} remedy groups"
        )
    faults: list[ManualFault] = []
    for heading, causes, remedies in zip(headings, cause_groups, remedy_groups):
        for cause, remedy in zip(causes, remedies):
            cause.remedy = remedy.cause
        faults.append(
            ManualFault(
                fault=_clean_name(heading),
                page=causes[0].page if causes else 0,
                section=_clean_name(heading),
                causes=causes,
            )
        )
    return faults


def parse_coox_sections(path: Path, pages: list[int]) -> list[ManualFault]:
    """Parse the Coox prose handbook's numbered sections."""
    pdfplumber = _require_pdfplumber()
    text = ""
    with pdfplumber.open(path) as pdf:
        for page_index in pages:
            page = pdf.pages[page_index - 1]
            page_text = page.extract_text() or ""
            page_text = re.sub(r"(?m)^\s*\d+\s*$", "", page_text)
            text += "\n" + page_text
    sections = re.split(r"(?m)^\s*(\d+)、\s*", text)
    faults: list[ManualFault] = []
    for index in range(1, len(sections) - 1, 2):
        title = _clean_text(sections[index + 1].splitlines()[0])
        body = "\n".join(sections[index + 1].splitlines()[1:])
        causes = _coox_causes(body)
        if not causes:
            continue
        faults.append(
            ManualFault(
                fault=_clean_name(title),
                page=pages[0] if pages else 0,
                section=_clean_name(title),
                causes=causes,
            )
        )
    return faults


def _coox_causes(body: str) -> list[CauseRow]:
    """Extract the ``原因：`` block from one Coox section.

    Sections 10 and 11 list inspection steps without a ``原因`` heading; they
    are left out rather than guessed at.  Section 3 splits its list under
    ``一是/二是/三是/四是`` category headings, which are removed before the
    circled items are read.
    """
    match = re.search(
        r"(?:原因|故障分析|故障原因)\s*[：:]\s*(.*?)"
        r"(?=(?:排除方法|排除方式|解决方式|解决办法|可分别采取|可采取|$))",
        body,
        re.DOTALL,
    )
    if not match:
        match = re.search(
            r"常见的?故障有\s*[：:]\s*(.*?)"
            r"(?=(?:排除方法|排除方式|解决方式|解决办法|可分别采取|可采取|$))",
            body,
            re.DOTALL,
        )
    if not match:
        return []
    # The PDF wraps a cause mid-sentence; join the visual lines before
    # splitting so a wrapped clause stays one cause.
    block = re.sub(r"[ \t]*\n[ \t]*", "", match.group(1))
    block = re.sub(
        r"(?:一|二|三|四|五|六)是[^：:；;。\n]{0,20}[：:]", "；", block
    )
    items = re.split(r"[①②③④⑤⑥⑦⑧⑨⑩]", block)
    if len(items) <= 1:
        items = re.split(r"[；;]\s*", block)
    causes: list[CauseRow] = []
    for item in items:
        value = _clean_text(item)
        value = value.strip("，,。；;：:、 ")
        if value:
            causes.append(CauseRow(cause=value))
    return causes


def _chunk_text(fault: str, cause: str, remedy: str) -> str:
    lines = [f"故障现象：{fault}", f"可能原因：{cause}"]
    if remedy:
        lines.append(f"排除方法：{remedy}")
    return "\n".join(lines)


def _build_target(fault: str, cause: str) -> dict[str, Any]:
    fault_name = _clean_name(fault)
    cause_name = _clean_name(cause)
    entities = [
        {
            "local_id": "f1",
            "class": "FaultEvent",
            "name": fault_name,
            "description": f"故障事件：{fault_name}",
            "evidence": [{"quote": fault}],
        },
        {
            "local_id": "c1",
            "class": "FaultEvent",
            "name": cause_name,
            "description": f"故障事件：{cause_name}",
            "evidence": [{"quote": cause}],
        },
    ]
    relations: list[dict[str, Any]] = []
    if cause_name and cause_name != fault_name:
        relations.append(
            {
                "relation_code": "CAUSES",
                "source_local_id": "c1",
                "target_local_id": "f1",
                "evidence": [{"quote": cause}, {"quote": fault}],
            }
        )
    return {"entities": entities, "relations": relations}


def _build_rows(
    manual: dict[str, Any],
    faults: list[ManualFault],
    *,
    holdout: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    chunks: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    manual_id = str(manual["id"])
    source_url = str(manual.get("url") or "")
    license_name = str(manual.get("license") or "")
    for fault_index, fault in enumerate(faults, start=1):
        for cause_index, cause_row in enumerate(fault.causes, start=1):
            cause = _clean_text(cause_row.cause)
            remedy = _clean_text(cause_row.remedy)
            if not cause or not fault.fault:
                continue
            chunk_text = _chunk_text(fault.fault, cause, remedy)
            digest = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            chunk_id = (
                f"{manual_id}-f{fault_index:02d}-c{cause_index:02d}"
            )
            chunk = {
                "chunk_id": chunk_id,
                "asset_id": manual_id,
                "text": chunk_text,
                "page": cause_row.page or fault.page,
                "section_path": fault.section or fault.fault,
                "modality": "table",
                "source_id": manual_id,
                "source_url": source_url,
                "license": license_name,
                "text_sha256": digest,
                "holdout": holdout,
            }
            chunks.append(chunk)
            target = _build_target(fault.fault, cause)
            try:
                validate_student_target(target, chunk_text, sample_id=chunk_id)
            except StudentLabelError as exc:
                errors.append({"chunk_id": chunk_id, "error": str(exc)})
                continue
            labels.append(
                {
                    "chunk_id": chunk_id,
                    "asset_id": manual_id,
                    "source_id": manual_id,
                    "target": target,
                    "warnings": [],
                    "stats": {
                        "entities": len(target["entities"]),
                        "relations": len(target["relations"]),
                        "nodes": len(target["entities"]),
                        "dropped_nodes": 0,
                    },
                }
            )
    return chunks, labels, errors


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def load_spec(path: Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    manuals = payload.get("manuals") if isinstance(payload, dict) else None
    if not isinstance(manuals, list) or not manuals:
        raise ValueError(f"{path}: expected a non-empty 'manuals' list")
    return manuals


def _parse_manual(manual: dict[str, Any]) -> list[ManualFault]:
    path = REPO_ROOT / str(manual["path"])
    if not path.exists():
        raise FileNotFoundError(path)
    pages = [int(page) for page in manual.get("pages") or []]
    if not pages:
        raise ValueError(f"{manual['id']}: 'pages' is required")
    layout = str(manual["format"])
    if layout == "grundfos_letter":
        return parse_grundfos_letter(path, pages)
    if layout == "grundfos_section":
        return parse_grundfos_section(path, pages)
    if layout == "tianjin_table":
        return parse_tianjin_table(path, pages)
    if layout == "coox_sections":
        return parse_coox_sections(path, pages)
    raise ValueError(f"{manual['id']}: unknown format {layout!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--chunks-out", type=Path, default=DEFAULT_CHUNKS)
    parser.add_argument("--labels-out", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--report-out", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--holdout-chunks-out", type=Path, default=DEFAULT_HOLDOUT_CHUNKS
    )
    parser.add_argument(
        "--holdout-labels-out", type=Path, default=DEFAULT_HOLDOUT_LABELS
    )
    parser.add_argument(
        "--only", action="append", default=[], help="manual id to process"
    )
    args = parser.parse_args(argv)
    if args.only and (
        args.chunks_out == DEFAULT_CHUNKS
        or args.labels_out == DEFAULT_LABELS
        or args.holdout_chunks_out == DEFAULT_HOLDOUT_CHUNKS
        or args.holdout_labels_out == DEFAULT_HOLDOUT_LABELS
    ):
        parser.error(
            "--only requires explicit --chunks-out, --labels-out, "
            "--holdout-chunks-out and --holdout-labels-out so a debug run "
            "cannot overwrite the full dataset"
        )

    try:
        manuals = load_spec(args.spec)
    except (OSError, ValueError) as exc:
        print(f"[manuals] {exc}", file=sys.stderr)
        return 2
    if args.only:
        selected = set(args.only)
        manuals = [manual for manual in manuals if manual.get("id") in selected]
        if not manuals:
            print(f"[manuals] no manuals matched --only {args.only}", file=sys.stderr)
            return 2

    train_chunks: list[dict[str, Any]] = []
    train_labels: list[dict[str, Any]] = []
    holdout_chunks: list[dict[str, Any]] = []
    holdout_labels: list[dict[str, Any]] = []
    report_manuals: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for manual in manuals:
        try:
            faults = _parse_manual(manual)
        except (OSError, ValueError) as exc:
            print(f"[manuals] {manual.get('id')}: {exc}", file=sys.stderr)
            return 2
        holdout = bool(manual.get("holdout"))
        chunks, labels, manual_errors = _build_rows(
            manual, faults, holdout=holdout
        )
        errors.extend(manual_errors)
        if holdout:
            holdout_chunks.extend(chunks)
            holdout_labels.extend(labels)
        else:
            train_chunks.extend(chunks)
            train_labels.extend(labels)
        report_manuals.append(
            {
                "id": manual.get("id"),
                "title": manual.get("title"),
                "format": manual.get("format"),
                "pages": manual.get("pages"),
                "holdout": holdout,
                "faults": len(faults),
                "causes": sum(len(fault.causes) for fault in faults),
                "chunks": len(chunks),
                "labels": len(labels),
            }
        )

    _write_jsonl(args.chunks_out, train_chunks)
    _write_jsonl(args.labels_out, train_labels)
    _write_jsonl(args.holdout_chunks_out, holdout_chunks)
    _write_jsonl(args.holdout_labels_out, holdout_labels)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "scripts/ingest_pump_manuals.py",
        "spec": str(args.spec),
        "manuals": report_manuals,
        "stats": {
            "manual_count": len(report_manuals),
            "faults": sum(item["faults"] for item in report_manuals),
            "causes": sum(item["causes"] for item in report_manuals),
            "train_chunks": len(train_chunks),
            "train_labels": len(train_labels),
            "holdout_chunks": len(holdout_chunks),
            "holdout_labels": len(holdout_labels),
            "invalid_labels": len(errors),
        },
        "errors": errors[:50],
        "outputs": {
            "chunks": str(args.chunks_out),
            "labels": str(args.labels_out),
            "holdout_chunks": str(args.holdout_chunks_out),
            "holdout_labels": str(args.holdout_labels_out),
        },
    }
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
