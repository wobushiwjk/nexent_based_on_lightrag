"""Tests for standard-driven ontology change draft validation and storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from lightrag.fault_tree.governance import (
    ChangeEvidence,
    OntologyChangeOperation,
    OntologyChangeStore,
    OntologyDraftConflictError,
    OntologyStandardDraftRequest,
    build_standard_change_set,
)

SOURCE = (
    "Condition monitoring systems shall identify sensors installed on "
    "rotating equipment."
)


def _request() -> OntologyStandardDraftRequest:
    quote = SOURCE
    return OntologyStandardDraftRequest(
        change_id="CHG-STANDARD-1",
        proposed_version="industrial-fault-v0.2",
        author="engineer",
        rationale="Align the ontology with condition-monitoring concepts.",
        source_name="ISO 13374 excerpt",
        source_text=SOURCE,
        operations=[
            OntologyChangeOperation(
                kind="class",
                action="add",
                code="Sensor",
                after={"parent": "Asset", "label": "Sensor"},
                evidence=[
                    ChangeEvidence(
                        quote=quote,
                        source_name="ISO 13374 excerpt",
                        page=3,
                    )
                ],
            ),
            OntologyChangeOperation(
                kind="relation",
                action="add",
                code="MONITORS",
                after={
                    "source_types": ["Sensor"],
                    "target_types": ["Equipment"],
                },
                evidence=[
                    ChangeEvidence(
                        quote=quote,
                        source_name="ISO 13374 excerpt",
                        section_path="clause 4",
                    )
                ],
            ),
        ],
    )


def test_standard_draft_resolves_quotes_and_batch_dependencies() -> None:
    change = build_standard_change_set(
        _request(),
        active_version="industrial-fault-v0.1",
        ontology_document={
            "classes": {
                "Asset": {"label": "Asset"},
                "Equipment": {"label": "Equipment"},
            },
            "relations": {},
            "constraints": [],
        },
    )

    assert change.base_version == "industrial-fault-v0.1"
    assert change.proposed_version == "industrial-fault-v0.2"
    assert change.metadata["applied"] is False
    assert [operation.code for operation in change.operations] == [
        "Sensor",
        "MONITORS",
    ]
    for operation in change.operations:
        evidence = operation.evidence[0]
        assert evidence.evidence_id is not None
        assert evidence.evidence_id in operation.evidence_ids
        assert evidence.char_start == 0
        assert evidence.char_end == len(SOURCE)


def test_standard_draft_rejects_non_verbatim_quote() -> None:
    request = _request()
    request.operations[0].evidence[0].quote = "A paraphrased requirement."

    with pytest.raises(OntologyDraftConflictError) as info:
        build_standard_change_set(
            request,
            active_version="industrial-fault-v0.1",
            ontology_document={
                "classes": {
                    "Asset": {"label": "Asset"},
                    "Equipment": {"label": "Equipment"},
                },
                "relations": {},
            },
        )

    assert "not verbatim" in str(info.value)


def test_standard_draft_rejects_existing_class() -> None:
    request = _request()
    request.operations[0].code = "Equipment"

    with pytest.raises(OntologyDraftConflictError) as info:
        build_standard_change_set(
            request,
            active_version="industrial-fault-v0.1",
            ontology_document={
                "classes": {
                    "Asset": {"label": "Asset"},
                    "Equipment": {"label": "Equipment"},
                },
                "relations": {},
            },
        )

    assert "already exists" in str(info.value)


def test_change_store_keeps_source_snapshot_idempotently(
    tmp_path: Path,
) -> None:
    request = _request()
    change = build_standard_change_set(
        request,
        active_version="industrial-fault-v0.1",
        ontology_document={
            "classes": {
                "Asset": {"label": "Asset"},
                "Equipment": {"label": "Equipment"},
            },
            "relations": {},
        },
    )
    store = OntologyChangeStore(tmp_path / "governance")

    store.create_change_set(
        change,
        source_text=SOURCE,
        source_name=request.source_name,
        stored_by=request.author,
    )
    store.create_change_set(
        change,
        source_text=SOURCE,
        source_name=request.source_name,
        stored_by=request.author,
    )

    source = store.load_change_source(change.change_id)
    assert source is not None
    manifest, text = source
    assert text == SOURCE
    assert manifest.source_name == request.source_name
    assert manifest.source_size == len(SOURCE.encode("utf-8"))
