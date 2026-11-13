"""Validate standard excerpts into immutable ontology change drafts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from lightrag.fault_tree.governance.models import (
    ChangeEvidence,
    OntologyChangeOperation,
    OntologyChangeSet,
    OntologyStandardDraftRequest,
)
from lightrag.fault_tree.governance.store import OntologyGovernanceError


class OntologyDraftConflictError(OntologyGovernanceError):
    """A proposed draft conflicts with the active ontology."""


def build_standard_change_set(
    request: OntologyStandardDraftRequest,
    *,
    active_version: str,
    ontology_document: Mapping[str, Any],
) -> OntologyChangeSet:
    """Validate quotes and ontology conflicts, returning an immutable draft.

    This function does not install or activate a version. The caller stores the
    returned change set as a draft and source snapshot for later review.
    """

    if request.proposed_version == active_version:
        raise OntologyDraftConflictError(
            "proposed_version must differ from the active ontology version"
        )
    classes = set(_mapping(ontology_document.get("classes")))
    relations = set(_mapping(ontology_document.get("relations")))
    constraints = {
        str(item.get("id"))
        for item in ontology_document.get("constraints") or []
        if isinstance(item, Mapping) and item.get("id")
    }

    operations: list[OntologyChangeOperation] = []
    for operation in request.operations:
        _validate_operation(
            operation,
            classes=classes,
            relations=relations,
            constraints=constraints,
        )
        if operation.action == "add":
            if operation.kind == "class":
                classes.add(operation.code)
            elif operation.kind == "relation":
                relations.add(operation.code)
            elif operation.kind == "constraint":
                constraints.add(operation.code)
        elif operation.action == "remove":
            if operation.kind == "class":
                classes.discard(operation.code)
            elif operation.kind == "relation":
                relations.discard(operation.code)
            elif operation.kind == "constraint":
                constraints.discard(operation.code)
        evidence = [
            _normalize_evidence(
                item,
                source_name=request.source_name,
                source_text=request.source_text,
            )
            for item in operation.evidence
        ]
        operations.append(operation.model_copy(update={"evidence": evidence}))

    return OntologyChangeSet(
        change_id=request.change_id,
        base_version=active_version,
        proposed_version=request.proposed_version,
        author=request.author,
        rationale=request.rationale,
        created_at=datetime.now(timezone.utc),
        source_standard=request.source_name,
        operations=operations,
        metadata={
            "source_name": request.source_name,
            "source_sha256": _sha256_text(request.source_text),
            "source_size": len(request.source_text.encode("utf-8")),
            "draft_only": True,
            "applied": False,
        },
    )


def _validate_operation(
    operation: OntologyChangeOperation,
    *,
    classes: set[str],
    relations: set[str],
    constraints: set[str],
) -> None:
    if operation.kind == "class":
        _validate_class_operation(operation, classes)
    elif operation.kind == "relation":
        _validate_relation_operation(operation, classes, relations)
    elif operation.kind == "constraint":
        _validate_constraint_operation(operation, classes, constraints)
    elif operation.action in {"remove", "deprecate"} and not operation.code:
        raise OntologyDraftConflictError("gap operation requires a code")


def _validate_class_operation(
    operation: OntologyChangeOperation,
    classes: set[str],
) -> None:
    exists = operation.code in classes
    if operation.action == "add":
        if exists:
            raise OntologyDraftConflictError(f"class {operation.code!r} already exists")
        parent = operation.after.get("parent")
        if parent and parent not in classes:
            raise OntologyDraftConflictError(f"class parent {parent!r} does not exist")
        return
    if not exists:
        raise OntologyDraftConflictError(f"class {operation.code!r} does not exist")
    if operation.action == "deprecate":
        replacement = operation.after.get("replacement")
        if replacement == operation.code:
            raise OntologyDraftConflictError("deprecated class cannot replace itself")
        if replacement and replacement not in classes:
            raise OntologyDraftConflictError(
                f"replacement class {replacement!r} does not exist"
            )


def _validate_relation_operation(
    operation: OntologyChangeOperation,
    classes: set[str],
    relations: set[str],
) -> None:
    exists = operation.code in relations
    if operation.action == "add":
        if exists:
            raise OntologyDraftConflictError(
                f"relation {operation.code!r} already exists"
            )
        source_types = _string_list(operation.after.get("source_types"))
        target_types = _string_list(operation.after.get("target_types"))
        if not source_types or not target_types:
            raise OntologyDraftConflictError(
                f"relation {operation.code!r} needs source_types and target_types"
            )
        unknown = sorted(
            {code for code in [*source_types, *target_types] if code not in classes}
        )
        if unknown:
            raise OntologyDraftConflictError(
                f"relation {operation.code!r} references unknown classes: "
                f"{', '.join(unknown)}"
            )
        return
    if not exists:
        raise OntologyDraftConflictError(f"relation {operation.code!r} does not exist")


def _validate_constraint_operation(
    operation: OntologyChangeOperation,
    classes: set[str],
    constraints: set[str],
) -> None:
    exists = operation.code in constraints
    if operation.action == "add":
        if exists:
            raise OntologyDraftConflictError(
                f"constraint {operation.code!r} already exists"
            )
        applies_to = _string_list(operation.after.get("applies_to"))
        if not applies_to:
            raise OntologyDraftConflictError(
                f"constraint {operation.code!r} needs applies_to"
            )
        unknown = sorted(code for code in applies_to if code not in classes)
        if unknown:
            raise OntologyDraftConflictError(
                f"constraint {operation.code!r} references unknown classes: "
                f"{', '.join(unknown)}"
            )
        return
    if not exists:
        raise OntologyDraftConflictError(
            f"constraint {operation.code!r} does not exist"
        )


def _normalize_evidence(
    evidence: ChangeEvidence,
    *,
    source_name: str,
    source_text: str,
) -> ChangeEvidence:
    if evidence.source_name != source_name:
        raise OntologyDraftConflictError(
            f"evidence source {evidence.source_name!r} does not match "
            f"request source {source_name!r}"
        )
    position = source_text.find(evidence.quote)
    if position < 0:
        raise OntologyDraftConflictError(
            f"evidence quote is not verbatim in {source_name!r}: "
            f"{evidence.quote[:80]!r}"
        )
    if evidence.char_start is not None:
        if evidence.char_end is None:
            raise OntologyDraftConflictError("evidence char_start requires char_end")
        if source_text[evidence.char_start : evidence.char_end] != (evidence.quote):
            raise OntologyDraftConflictError(
                "evidence character span does not match quote"
            )
        return evidence
    return evidence.model_copy(
        update={
            "char_start": position,
            "char_end": position + len(evidence.quote),
        }
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _sha256_text(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "OntologyDraftConflictError",
    "build_standard_change_set",
]
