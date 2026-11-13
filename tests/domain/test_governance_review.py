"""Tests for ontology review state transitions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lightrag.fault_tree.governance import (
    OntologyChangeOperation,
    OntologyChangeSet,
    OntologyChangeStore,
)
from lightrag.fault_tree.governance.review import (
    OntologyReviewError,
    append_lifecycle_event,
    change_status,
    review_change,
    submit_for_review,
)


def _change() -> OntologyChangeSet:
    return OntologyChangeSet(
        change_id="CHG-REVIEW-1",
        base_version="industrial-fault-v0.1",
        proposed_version="industrial-fault-v0.2",
        author="author",
        rationale="review test",
        created_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
        operations=[
            OntologyChangeOperation(
                kind="class",
                action="add",
                code="Sensor",
                after={"parent": "Asset"},
            )
        ],
    )


def test_review_state_machine(tmp_path: Path) -> None:
    store = OntologyChangeStore(tmp_path / "governance")
    change = _change()
    store.create_change_set(change)

    submit = submit_for_review(store, change, actor="alice")
    assert change_status(store.list_review_events(change.change_id)) == ("under_review")
    approve = review_change(
        store,
        change,
        decision="approve",
        actor="reviewer",
        reason="Evidence and impact accepted.",
    )
    assert change_status(store.list_review_events(change.change_id)) == "approved"
    append_lifecycle_event(
        store,
        change,
        action="publish",
        actor="publisher",
    )
    assert change_status(store.list_review_events(change.change_id)) == "published"
    append_lifecycle_event(
        store,
        change,
        action="rollback",
        actor="publisher",
        reason="verification failed",
    )
    assert change_status(store.list_review_events(change.change_id)) == "rolled_back"
    assert submit.action == "submit"
    assert approve.action == "approve"


def test_approval_requires_submission(tmp_path: Path) -> None:
    store = OntologyChangeStore(tmp_path / "governance")
    change = _change()
    store.create_change_set(change)

    with pytest.raises(OntologyReviewError):
        review_change(
            store,
            change,
            decision="approve",
            actor="reviewer",
        )


def test_rejection_requires_reason(tmp_path: Path) -> None:
    store = OntologyChangeStore(tmp_path / "governance")
    change = _change()
    store.create_change_set(change)
    submit_for_review(store, change, actor="alice")

    with pytest.raises(OntologyReviewError):
        review_change(
            store,
            change,
            decision="reject",
            actor="reviewer",
        )


def test_repeated_submit_is_idempotent(tmp_path: Path) -> None:
    store = OntologyChangeStore(tmp_path / "governance")
    change = _change()
    store.create_change_set(change)

    first = submit_for_review(store, change, actor="alice", reason="review")
    second = submit_for_review(store, change, actor="alice", reason="review")

    assert first == second
    assert len(store.list_review_events(change.change_id)) == 1
