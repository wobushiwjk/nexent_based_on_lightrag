"""Review state transitions for immutable ontology change sets."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from lightrag.fault_tree.governance.models import (
    OntologyChangeSet,
    OntologyChangeStatus,
    ReviewEvent,
)
from lightrag.fault_tree.governance.store import OntologyChangeStore


class OntologyReviewError(RuntimeError):
    """A requested review action is invalid for the current state."""


def change_status(events: tuple[ReviewEvent, ...]) -> OntologyChangeStatus:
    """Derive a change status from its append-only review history."""

    status: OntologyChangeStatus = "draft"
    for event in events:
        if event.action == "submit" and status in {
            "draft",
            "under_review",
            "rejected",
        }:
            status = "under_review"
        elif event.action == "approve" and status == "under_review":
            status = "approved"
        elif event.action == "reject" and status == "under_review":
            status = "rejected"
        elif event.action == "withdraw" and status in {
            "draft",
            "under_review",
            "rejected",
        }:
            status = "draft"
        elif event.action == "publish" and status == "approved":
            status = "published"
        elif event.action == "rollback" and status == "published":
            status = "rolled_back"
    return status


def submit_for_review(
    store: OntologyChangeStore,
    change: OntologyChangeSet,
    *,
    actor: str,
    reason: str | None = None,
) -> ReviewEvent:
    """Move a draft or rejected change into review."""

    events = store.list_review_events(change.change_id)
    status = change_status(events)
    if status == "under_review":
        return _matching_event(
            events,
            action="submit",
            actor=actor,
            reason=reason,
            content_sha256=change_digest(change),
        )
    if status not in {"draft", "rejected"}:
        raise OntologyReviewError(f"cannot submit change in status {status!r}")
    return _append(
        store,
        change,
        action="submit",
        actor=actor,
        reason=reason,
    )


def review_change(
    store: OntologyChangeStore,
    change: OntologyChangeSet,
    *,
    decision: str,
    actor: str,
    reason: str | None = None,
) -> ReviewEvent:
    """Approve or reject a change currently under review."""

    if decision not in {"approve", "reject"}:
        raise OntologyReviewError(f"unsupported review decision {decision!r}")
    if decision == "reject" and not (reason or "").strip():
        raise OntologyReviewError("rejection requires a reason")
    events = store.list_review_events(change.change_id)
    status = change_status(events)
    expected = "approved" if decision == "approve" else "rejected"
    if status == expected:
        return _matching_event(
            events,
            action=decision,
            actor=actor,
            reason=reason,
            content_sha256=change_digest(change),
        )
    if status != "under_review":
        raise OntologyReviewError(f"cannot {decision} change in status {status!r}")
    return _append(
        store,
        change,
        action=decision,
        actor=actor,
        reason=reason,
    )


def append_lifecycle_event(
    store: OntologyChangeStore,
    change: OntologyChangeSet,
    *,
    action: str,
    actor: str,
    reason: str | None = None,
    metadata: dict | None = None,
) -> ReviewEvent:
    """Append a publish or rollback lifecycle event after state verification."""

    if action not in {"publish", "rollback"}:
        raise OntologyReviewError(f"unsupported lifecycle action {action!r}")
    return _append(
        store,
        change,
        action=action,
        actor=actor,
        reason=reason,
        metadata=metadata,
    )


def change_digest(change: OntologyChangeSet) -> str:
    """Return the canonical SHA-256 digest of a change set."""

    payload = json.dumps(
        change.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _append(
    store: OntologyChangeStore,
    change: OntologyChangeSet,
    *,
    action: str,
    actor: str,
    reason: str | None,
    metadata: dict | None = None,
) -> ReviewEvent:
    digest = change_digest(change)
    event_key = hashlib.sha256(
        (f"{change.change_id}|{action}|{actor}|{reason or ''}|{digest}").encode("utf-8")
    ).hexdigest()[:16]
    event = ReviewEvent(
        event_id=f"EVT-{event_key}",
        change_id=change.change_id,
        action=action,
        actor=actor.strip(),
        occurred_at=datetime.now(timezone.utc),
        reason=(reason or "").strip() or None,
        content_sha256=digest,
        metadata=metadata or {},
    )
    return store.append_review_event(event)


def _matching_event(
    events: tuple[ReviewEvent, ...],
    *,
    action: str,
    actor: str,
    reason: str | None,
    content_sha256: str,
) -> ReviewEvent:
    for event in reversed(events):
        if (
            event.action == action
            and event.actor == actor.strip()
            and event.reason == ((reason or "").strip() or None)
            and event.content_sha256 == content_sha256
        ):
            return event
    raise OntologyReviewError(f"change is already processed with action {action!r}")


__all__ = [
    "OntologyReviewError",
    "append_lifecycle_event",
    "change_digest",
    "change_status",
    "review_change",
    "submit_for_review",
]
