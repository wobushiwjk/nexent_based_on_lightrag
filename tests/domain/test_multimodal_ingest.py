"""Tests for the W6 PDF/table/image ingestion front end."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lightrag.fault_tree.contracts import FileAsset
from lightrag.fault_tree.domain_validation import (
    errors_only,
    validate_domain_bundle,
)
from lightrag.fault_tree.importer import build_imported_domain_from_workspace
from lightrag.fault_tree.multimodal import (
    DocumentSpec,
    DocumentStatus,
    DocumentStatusStore,
    FixtureVlm,
    MultimodalError,
    build_multimodal_workspace,
    ensure_local_endpoint,
    load_manifest,
    parse_image,
    parse_pdf_pages,
    parse_table_rows,
)
from lightrag.fault_tree.verification import verify_evidence
from scripts.make_multimodal_samples import (
    CSV_HEADER,
    CSV_ROWS,
    EQUIPMENT_ROWS,
    NAMEPLATE_LINES,
    PDF_PAGES,
    write_csv,
    write_png,
    write_simple_pdf,
    write_xlsx,
)

ROOT = Path(__file__).resolve().parents[2]
REAL_PACK = ROOT / "domain_packs" / "industrial_fault"
FIXTURES = (
    "pdf_manual.json",
    "table_row3.json",
    "xlsx_row3.json",
    "image_nameplate.json",
    "image_transcript.json",
)


def _spec(
    path: Path,
    *,
    asset_id: str = "FILE-TEST",
    modality: str = "pdf",
    extraction: tuple[Path, ...] = (),
    header_row: bool = True,
) -> DocumentSpec:
    return DocumentSpec(
        asset_id=asset_id,
        path=path,
        relative_path=path.name,
        modality=modality,
        extraction=extraction,
        canonical_name=None,
        description=None,
        mime_type="application/octet-stream",
        language=None,
        header_row=header_row,
    )


def _write_pack(tmp_path: Path, *, missing: set[str] | None = None) -> Path:
    pack = tmp_path / "pack"
    extraction = pack / "seeds" / "multimodal" / "extraction"
    generated = pack / "seeds" / "multimodal" / "generated"
    extraction.mkdir(parents=True)
    generated.mkdir(parents=True)
    shutil.copy(REAL_PACK / "ontology.yml", pack / "ontology.yml")
    shutil.copy(
        REAL_PACK / "seeds" / "multimodal" / "manifest.json",
        pack / "seeds" / "multimodal" / "manifest.json",
    )
    for name in FIXTURES:
        shutil.copy(
            REAL_PACK / "seeds" / "multimodal" / "extraction" / name,
            extraction / name,
        )
    write_simple_pdf(generated / "pump_manual_p101a.pdf", PDF_PAGES)
    write_csv(generated / "inspection_p101a.csv", CSV_HEADER, CSV_ROWS)
    write_xlsx(
        generated / "inspection_p101a.xlsx", CSV_ROWS, EQUIPMENT_ROWS
    )
    write_png(generated / "nameplate_p101a.png", NAMEPLATE_LINES)
    for name in missing or set():
        (generated / name).unlink()
    return pack


def _fixture(pack: Path) -> FixtureVlm:
    return FixtureVlm.from_file(
        pack / "seeds" / "multimodal" / "extraction" / "image_transcript.json"
    )


# ---------------------------------------------------------------------------
# Parser locators
# ---------------------------------------------------------------------------


def test_pdf_pages_keep_transcript_offsets(tmp_path: Path) -> None:
    path = tmp_path / "manual.pdf"
    write_simple_pdf(path, PDF_PAGES)
    parsed = parse_pdf_pages(_spec(path))

    assert [(chunk.page, chunk.chunk_id) for chunk in parsed.chunks] == [
        (1, "FILE-TEST:p1"),
        (2, "FILE-TEST:p2"),
    ]
    assert parsed.chunks[0].char_base == 0
    assert parsed.chunks[1].char_base == parsed.text.index(
        parsed.chunks[1].text
    )
    assert "P-101A" in parsed.chunks[0].text
    assert parsed.page_count == 2
    assert parsed.parse_engine == "legacy"


def test_pdf_without_text_layer_fails_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "scanned.pdf"
    write_simple_pdf(path, [""])
    with pytest.raises(MultimodalError) as exc:
        parse_pdf_pages(_spec(path))
    assert exc.value.code == "pdf-no-text-layer"


def test_csv_rows_use_header_value_text(tmp_path: Path) -> None:
    path = tmp_path / "inspection.csv"
    write_csv(path, CSV_HEADER, CSV_ROWS)
    parsed = parse_table_rows(_spec(path, modality="table"))

    assert [chunk.chunk_id for chunk in parsed.chunks] == [
        "FILE-TEST:row2",
        "FILE-TEST:row3",
    ]
    assert parsed.chunks[1].section_path == "row3"
    assert "出口压力 0.32 MPa，低于 0.45 MPa 下限" in parsed.chunks[1].text
    assert parsed.text[parsed.chunks[1].char_base :] == parsed.chunks[1].text
    assert parsed.chunks[1].modality == "table"


def test_xlsx_rows_are_sheet_addressable(tmp_path: Path) -> None:
    path = tmp_path / "inspection.xlsx"
    write_xlsx(path, CSV_ROWS, EQUIPMENT_ROWS)
    parsed = parse_table_rows(_spec(path, modality="table"))

    chunks = {chunk.chunk_id: chunk for chunk in parsed.chunks}
    assert "FILE-TEST:inspection:row3" in chunks
    assert "FILE-TEST:equipment:row2" in chunks
    assert chunks["FILE-TEST:inspection:row3"].section_path == (
        "inspection!row3"
    )
    assert chunks["FILE-TEST:equipment:row2"].section_path == (
        "equipment!row2"
    )


def test_empty_table_fails_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    write_csv(path, CSV_HEADER, [])
    with pytest.raises(MultimodalError) as exc:
        parse_table_rows(_spec(path, modality="table"))
    assert exc.value.code == "table-empty"


def test_image_uses_fixture_transcript(tmp_path: Path) -> None:
    path = tmp_path / "nameplate.png"
    write_png(path, NAMEPLATE_LINES)
    parsed = parse_image(
        _spec(path, modality="image", asset_id="FILE-NAMEPLATE"),
        vlm=FixtureVlm(
            {"FILE-NAMEPLATE": "P-101A\nOUTLET PRESSURE LOW\n"}
        ),
    )

    assert parsed.chunks[0].chunk_id == "FILE-NAMEPLATE:image"
    assert parsed.chunks[0].text == "P-101A\nOUTLET PRESSURE LOW"
    assert parsed.chunks[0].page == 1
    assert parsed.text == "P-101A\nOUTLET PRESSURE LOW\n"


def test_empty_image_transcript_fails(tmp_path: Path) -> None:
    path = tmp_path / "blank.png"
    write_png(path, [])
    with pytest.raises(MultimodalError) as exc:
        parse_image(
            _spec(path, modality="image"),
            vlm=FixtureVlm({"FILE-TEST": "  \n"}),
        )
    assert exc.value.code == "vlm-empty-output"


# ---------------------------------------------------------------------------
# Privacy guard
# ---------------------------------------------------------------------------


def test_endpoint_guard_accepts_loopback() -> None:
    ensure_local_endpoint("http://127.0.0.1:11434/v1")
    ensure_local_endpoint("http://localhost:11434/v1")


def test_endpoint_guard_rejects_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMAIN_ALLOW_EXTERNAL_VLM", raising=False)
    with pytest.raises(MultimodalError) as exc:
        ensure_local_endpoint("http://8.8.8.8/v1")
    assert exc.value.code == "vlm-endpoint-not-local"


def test_endpoint_guard_allows_explicit_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMAIN_ALLOW_EXTERNAL_VLM", raising=False)
    ensure_local_endpoint("http://8.8.8.8/v1", allow_external=True)
    monkeypatch.setenv("DOMAIN_ALLOW_EXTERNAL_VLM", "1")
    ensure_local_endpoint("http://8.8.8.8/v1")


# ---------------------------------------------------------------------------
# Status store
# ---------------------------------------------------------------------------


def test_status_store_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    store = DocumentStatusStore(path)
    store.load()
    store.record(
        DocumentStatus(
            asset_id="FILE-A",
            status="parsed",
            attempts=1,
            chunk_count=2,
            text_uri="text/FILE-A.txt",
        )
    )

    reloaded = DocumentStatusStore(path)
    statuses = reloaded.load()
    assert statuses["FILE-A"].status == "parsed"
    assert statuses["FILE-A"].chunk_count == 2
    assert reloaded.failed_ids() == set()


def test_status_store_reports_failed_documents(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    store = DocumentStatusStore(path)
    store.load()
    store.record(DocumentStatus(asset_id="FILE-A", status="parsed"))
    store.record(
        DocumentStatus(
            asset_id="FILE-B",
            status="failed",
            error_code="source-missing",
            error_message="missing",
        )
    )
    assert DocumentStatusStore(path).load()["FILE-B"].error_code == (
        "source-missing"
    )
    assert DocumentStatusStore(path).failed_ids() == {"FILE-B"}


def test_corrupt_status_file_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(MultimodalError) as exc:
        DocumentStatusStore(path).load()
    assert exc.value.code == "status-store-corrupt"


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_three_modalities_import_with_verbatim_evidence(
    tmp_path: Path,
) -> None:
    pack = _write_pack(tmp_path)
    manifest = load_manifest(pack)
    ingested = build_multimodal_workspace(
        manifest, vlm=_fixture(pack)
    )

    assert [status.status for status in ingested.statuses] == [
        "parsed",
        "parsed",
        "parsed",
        "parsed",
    ]
    imported = build_imported_domain_from_workspace(
        pack, ingested.workspace
    )
    assert errors_only(validate_domain_bundle(imported.bundle, pack)) == []
    checks = verify_evidence(imported)
    assert checks["evidence"] > 0
    assert checks["rules"] == 1

    assets = [
        entity
        for entity in imported.bundle.entities
        if isinstance(entity, FileAsset)
    ]
    assert {asset.modality for asset in assets} == {
        "pdf",
        "table",
        "image",
    }
    for asset in assets:
        assert asset.text_uri
        assert (pack / asset.text_uri).is_file()
    modalities = {item.modality for item in imported.bundle.evidence}
    assert modalities == {"pdf", "table", "image"}
    rule = imported.bundle.rules[0]
    assert rule.rule_uid == "RULE-MM-LOW-PRESSURE-OR"
    assert imported.tree is not None
    assert imported.tree.top_event_uid == "FAULT-LOW-PRESSURE"


def test_missing_source_is_reported_not_swallowed(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path, missing={"nameplate_p101a.png"})
    manifest = load_manifest(pack)
    ingested = build_multimodal_workspace(
        manifest, vlm=_fixture(pack)
    )

    failed = {
        status.asset_id: status for status in ingested.failed
    }
    assert set(failed) == {"FILE-P101A-NAMEPLATE"}
    assert failed["FILE-P101A-NAMEPLATE"].error_code == "source-missing"
    assert failed["FILE-P101A-NAMEPLATE"].attempts == 1
    assert len(ingested.workspace.chunks) > 0


def test_retry_failed_reuses_parsed_image(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path)
    manifest = load_manifest(pack)
    status_path = tmp_path / "status.json"
    store = DocumentStatusStore(status_path)

    first = build_multimodal_workspace(
        manifest, vlm=_fixture(pack), status_store=store
    )
    image = next(
        item
        for item in first.statuses
        if item.asset_id == "FILE-P101A-NAMEPLATE"
    )
    assert image.attempts == 1

    second = build_multimodal_workspace(
        manifest,
        vlm=_fixture(pack),
        status_store=DocumentStatusStore(status_path),
        retry_failed=True,
    )
    retried = next(
        item
        for item in second.statuses
        if item.asset_id == "FILE-P101A-NAMEPLATE"
    )
    assert retried.status == "parsed"
    assert retried.attempts == 1
    assert retried.text_uri == image.text_uri


def test_binary_asset_without_text_uri_is_a_validation_error(
    tmp_path: Path,
) -> None:
    pack = _write_pack(tmp_path)
    manifest = load_manifest(pack)
    ingested = build_multimodal_workspace(
        manifest, vlm=_fixture(pack)
    )
    imported = build_imported_domain_from_workspace(
        pack, ingested.workspace
    )
    pdf_asset = next(
        entity
        for entity in imported.bundle.entities
        if isinstance(entity, FileAsset) and entity.modality == "pdf"
    )
    broken = pdf_asset.model_copy(update={"text_uri": None})
    bundle = imported.bundle.model_copy(
        update={
            "entities": [
                broken if entity.uid == broken.uid else entity
                for entity in imported.bundle.entities
            ]
        }
    )

    issues = validate_domain_bundle(bundle, pack)
    codes = {issue.code for issue in errors_only(issues)}
    assert "evidence-text-artifact-missing" in codes
