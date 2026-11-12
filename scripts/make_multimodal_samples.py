"""Generate the synthetic W6 multimodal samples.

Run from the repository root::

    .venv/Scripts/python.exe scripts/make_multimodal_samples.py

The four files land under ``domain_packs/industrial_fault/seeds/multimodal/
generated`` and are git-ignored, so they can be rebuilt at any time.  The
generator is dependency-free for PDF and PNG (stdlib only); XLSX uses openpyxl.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"
OUTPUT_DIR = Path("seeds") / "multimodal" / "generated"

PDF_PAGES = [
    "\n".join(
        [
            "P-101A PUMP OPERATION MANUAL",
            "Section 2.1 Low Outlet Pressure",
            "A blocked inlet filter reduces pump inlet flow and causes low outlet pressure.",
            "Cavitation occurs when the inlet flow is restricted and causes low outlet pressure.",
        ]
    ),
    "\n".join(
        [
            "Section 4.3 Maintenance Measures",
            "Clean the inlet filter every 500 operating hours and inspect the mechanical seal.",
        ]
    ),
]

CSV_HEADER = ["设备编号", "检查项目", "检查结果", "处理措施"]
CSV_ROWS = [
    ["P-101A", "入口过滤器", "压差 0.08 MPa，滤芯堵塞", "清理滤芯后出口压力恢复"],
    [
        "P-101A",
        "出口压力",
        "出口压力 0.32 MPa，低于 0.45 MPa 下限；入口过滤器压差偏高",
        "检查入口过滤器",
    ],
]

EQUIPMENT_ROWS = [
    ["P-101A", "P-101A 离心泵", "离心泵"],
]

NAMEPLATE_LINES = ["P-101A", "CENTRIFUGAL PUMP", "OUTLET PRESSURE LOW"]

# 5x7 bitmap font.  Only these glyphs are needed by NAMEPLATE_LINES; an
# unknown character renders as a filled box so the sample stays visibly wrong
# instead of silently dropping a character.
_FONT: dict[str, tuple[int, ...]] = {
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
}
_UNKNOWN_GLYPH = (0x1F, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1F)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_simple_pdf(path: Path, pages: list[str]) -> None:
    """Write a minimal text PDF with Helvetica and one content stream per page.

    This exists so the W6 sample has no reportlab/font dependency.  Each line
    is drawn as its own ``Tj`` at a fixed leading, which keeps pypdf's text
    reconstruction close to the input line.
    """

    if not pages:
        raise ValueError("a PDF needs at least one page")
    for page in pages:
        page.encode("ascii")

    objects: list[bytes] = []
    page_object_numbers = [4 + index * 2 for index in range(len(pages))]
    content_object_numbers = [5 + index * 2 for index in range(len(pages))]
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )
    for index, page in enumerate(pages):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_object_numbers[index]} 0 R >>"
            ).encode("ascii")
        )
        lines = page.splitlines() or [""]
        stream_lines = ["BT", "/F1 12 Tf", "72 720 Td"]
        for line_index, line in enumerate(lines):
            if line_index:
                stream_lines.append("0 -18 Td")
            stream_lines.append(f"({_pdf_escape(line)}) Tj")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("ascii")
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\n"
            b"stream\n" + stream + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(output))


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_xlsx(
    path: Path,
    inspection_rows: list[list[str]],
    equipment_rows: list[list[str]],
) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    inspection = workbook.active
    inspection.title = "inspection"
    inspection.append(CSV_HEADER)
    for row in inspection_rows:
        inspection.append(row)
    equipment = workbook.create_sheet("equipment")
    equipment.append(["设备编号", "设备名称", "设备类型"])
    for row in equipment_rows:
        equipment.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _draw_text(pixels: list[list[tuple[int, int, int]]], lines: list[str]) -> None:
    scale = 6
    margin_x = 40
    margin_y = 40
    line_height = 14 * scale
    char_advance = 6 * scale
    for line_index, line in enumerate(lines):
        origin_y = margin_y + line_index * line_height
        for char_index, char in enumerate(line.upper()):
            glyph = _FONT.get(char, _UNKNOWN_GLYPH)
            origin_x = margin_x + char_index * char_advance
            for row_index, bits in enumerate(glyph):
                for column in range(5):
                    if bits & (1 << (4 - column)):
                        x = origin_x + column * scale
                        y = origin_y + row_index * scale
                        for dy in range(scale):
                            for dx in range(scale):
                                target_y = y + dy
                                target_x = x + dx
                                if (
                                    0 <= target_y < len(pixels)
                                    and 0 <= target_x < len(pixels[target_y])
                                ):
                                    pixels[target_y][target_x] = (20, 20, 20)


def write_png(path: Path, lines: list[str]) -> None:
    """Write an RGB nameplate PNG using only the stdlib."""

    width = 900
    height = 400
    background = (245, 245, 245)
    pixels = [
        [background for _ in range(width)] for _ in range(height)
    ]
    _draw_text(pixels, lines)

    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for red, green, blue in row:
            raw.extend((red, green, blue))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def generate(pack: Path) -> dict[str, Path]:
    out = (pack / OUTPUT_DIR).resolve()
    files = {
        "pdf": out / "pump_manual_p101a.pdf",
        "csv": out / "inspection_p101a.csv",
        "xlsx": out / "inspection_p101a.xlsx",
        "png": out / "nameplate_p101a.png",
    }
    write_simple_pdf(files["pdf"], PDF_PAGES)
    write_csv(files["csv"], CSV_HEADER, CSV_ROWS)
    write_xlsx(files["xlsx"], CSV_ROWS, EQUIPMENT_ROWS)
    write_png(files["png"], NAMEPLATE_LINES)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    args = parser.parse_args(argv)
    files = generate(args.pack.resolve())
    print(json.dumps(
        {name: str(path) for name, path in files.items()},
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
