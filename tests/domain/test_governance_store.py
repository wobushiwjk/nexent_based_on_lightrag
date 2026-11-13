"""Tests for the additive ontology governance version and change stores."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lightrag.fault_tree.governance import (
    ChangeSetConflictError,
    OntologyChangeOperation,
    OntologyChangeSet,
    OntologyChangeStore,
    OntologyGovernanceError,
    OntologyPointerError,
    OntologyVersionConflictError,
    OntologyVersionNotFoundError,
    OntologyVersionStore,
    ReviewEvent,
    ReviewEventConflictError,
)


def _ontology(version: str, label: str = "Asset") -> str:
    return f"""\
ontology_version: {version}
schema_version: industrial-fault-schema-v0.1
classes:
  Asset:
    label: {label}
    prompt_visible: true
    required: [uid]
relations:
  PART_OF:
    source: [Asset]
    target: [Asset]
"""


def _write_ontology(tmp_path: Path, version: str, label: str = "Asset") -> Path:
    path = tmp_path / f"{version}.yml"
    path.write_text(_ontology(version, label), encoding="utf-8")
    return path


def _change_set(change_id: str = "CHG-1") -> OntologyChangeSet:
    return OntologyChangeSet(
        change_id=change_id,
        base_version="industrial-fault-v0.1",
        proposed_version="industrial-fault-v0.2",
        author="reviewer",
        rationale="Add sensor coverage from the standard excerpt.",
        created_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
        source_standard="ISO 13374",
        operations=[
            OntologyChangeOperation(
                kind="class",
                action="add",
                code="Sensor",
                after={"parent": "Asset"},
                evidence_ids=["EV-1"],
            )
        ],
    )


def test_version_install_activate_and_historical_read(tmp_path: Path) -> None:
    store = OntologyVersionStore(tmp_path / "governance")
    v1 = _write_ontology(tmp_path, "industrial-fault-v0.1")
    v2 = _write_ontology(tmp_path, "industrial-fault-v0.2", "Asset v2")

    first = store.install(v1, actor="alice", activate=True)
    second = store.install(v2, actor="bob")

    assert store.read_active().active_version == "industrial-fault-v0.1"
    assert store.list_versions() == (
        "industrial-fault-v0.1",
        "industrial-fault-v0.2",
    )
    assert store.load_version("industrial-fault-v0.1").manifest == first
    assert store.load_version("industrial-fault-v0.2").manifest == second

    old_path = (
        tmp_path / "governance" / "versions" / "industrial-fault-v0.1" / "ontology.yml"
    )
    old_bytes = old_path.read_bytes()
    store.activate("industrial-fault-v0.2", actor="alice")

    assert old_path.read_bytes() == old_bytes
    active = store.load_active()
    assert active.manifest.ontology_version == "industrial-fault-v0.2"
    assert active.document()["classes"]["Asset"]["label"] == "Asset v2"
    assert (
        store.load_version("industrial-fault-v0.1").document()["classes"]["Asset"][
            "label"
        ]
        == "Asset"
    )


def test_reinstall_is_idempotent_and_content_conflict_fails(
    tmp_path: Path,
) -> None:
    store = OntologyVersionStore(tmp_path / "governance")
    source = _write_ontology(tmp_path, "industrial-fault-v0.1")
    first = store.install(source, actor="alice")
    manifest_path = (
        tmp_path / "governance" / "versions" / "industrial-fault-v0.1" / "manifest.json"
    )
    manifest_bytes = manifest_path.read_bytes()

    second = store.install(source, actor="someone-else")
    assert second == first
    assert manifest_path.read_bytes() == manifest_bytes

    changed = _write_ontology(tmp_path, "industrial-fault-v0.1", "Changed")
    with pytest.raises(OntologyVersionConflictError):
        store.install(changed, actor="alice")


def test_invalid_version_identifier_never_creates_a_path(
    tmp_path: Path,
) -> None:
    store = OntologyVersionStore(tmp_path / "governance")
    source = tmp_path / "unsafe.yml"
    source.write_text(_ontology("../escape"), encoding="utf-8")

    with pytest.raises(OntologyGovernanceError):
        store.install(source, actor="alice")

    assert not (tmp_path / "escape").exists()


def test_active_pointer_fails_closed_when_missing_or_corrupt(
    tmp_path: Path,
) -> None:
    store = OntologyVersionStore(tmp_path / "governance")
    source = _write_ontology(tmp_path, "industrial-fault-v0.1")
    store.install(source, actor="alice")

    with pytest.raises(OntologyPointerError):
        store.read_active()

    store.activate("industrial-fault-v0.1", actor="alice")
    store.active_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(OntologyPointerError):
        store.read_active()


def test_pointer_to_missing_version_is_an_error(tmp_path: Path) -> None:
    store = OntologyVersionStore(tmp_path / "governance")
    source = _write_ontology(tmp_path, "industrial-fault-v0.1")
    store.install(source, actor="alice", activate=True)
    payload = json.loads(store.active_path.read_text(encoding="utf-8"))
    payload["active_version"] = "industrial-fault-v9.9"
    store.active_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(OntologyVersionNotFoundError):
        store.read_active()


def test_tampered_ontology_content_is_rejected(tmp_path: Path) -> None:
    store = OntologyVersionStore(tmp_path / "governance")
    source = _write_ontology(tmp_path, "industrial-fault-v0.1")
    store.install(source, actor="alice", activate=True)
    target = (
        tmp_path / "governance" / "versions" / "industrial-fault-v0.1" / "ontology.yml"
    )
    target.write_text(_ontology("industrial-fault-v0.1", "Tampered"))

    with pytest.raises(OntologyGovernanceError):
        store.load_version("industrial-fault-v0.1")


def test_change_set_and_review_events_are_immutable(tmp_path: Path) -> None:
    store = OntologyChangeStore(tmp_path / "governance")
    change = _change_set()
    assert store.create_change_set(change) == change
    assert store.create_change_set(change) == change

    conflict = change.model_copy(update={"rationale": "A different reason"})
    with pytest.raises(ChangeSetConflictError):
        store.create_change_set(conflict)

    event = ReviewEvent(
        event_id="EVT-1",
        change_id=change.change_id,
        action="submit",
        actor="alice",
        occurred_at=datetime(2026, 11, 13, 1, tzinfo=timezone.utc),
        reason="Ready for review.",
    )
    assert store.append_review_event(event) == event
    assert store.append_review_event(event) == event
    assert store.list_review_events(change.change_id) == (event,)

    conflicting_event = event.model_copy(update={"actor": "mallory"})
    with pytest.raises(ReviewEventConflictError):
        store.append_review_event(conflicting_event)


def test_review_events_require_an_existing_change_set(
    tmp_path: Path,
) -> None:
    store = OntologyChangeStore(tmp_path / "governance")
    event = ReviewEvent(
        event_id="EVT-MISSING",
        change_id="CHG-MISSING",
        action="comment",
        actor="alice",
        occurred_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
    )

    with pytest.raises(OntologyGovernanceError):
        store.append_review_event(event)


def test_review_events_are_sorted_stably(tmp_path: Path) -> None:
    store = OntologyChangeStore(tmp_path / "governance")
    change = _change_set()
    store.create_change_set(change)
    later = ReviewEvent(
        event_id="EVT-B",
        change_id=change.change_id,
        action="approve",
        actor="bob",
        occurred_at=datetime(2026, 11, 13, 2, tzinfo=timezone.utc),
    )
    earlier = ReviewEvent(
        event_id="EVT-A",
        change_id=change.change_id,
        action="submit",
        actor="alice",
        occurred_at=datetime(2026, 11, 13, 1, tzinfo=timezone.utc),
    )
    store.append_review_event(later)
    store.append_review_event(earlier)

    assert [item.event_id for item in store.list_review_events(change.change_id)] == [
        "EVT-A",
        "EVT-B",
    ]
