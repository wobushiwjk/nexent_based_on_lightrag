"""Validate the student's ``FaultEvent + CAUSES`` target.

The student never emits UIDs, page numbers, or audit fields.  Its target is a
small JSON object with ``entities`` and ``relations``; this module is the
single validation gate shared by the public-corpus converter, the teacher
label importer, and the dataset builder.
"""

from __future__ import annotations

from typing import Any

_GATE_CODES = {"AND", "OR"}


class StudentLabelError(ValueError):
    """A student target violates the extraction contract."""


def _validate_expression(
    expression: dict[str, Any], local_ids: set[str], sample_id: str
) -> None:
    event_id = expression.get("event_local_id")
    op = expression.get("op")
    children = expression.get("children") or []
    if bool(event_id) == bool(op):
        raise StudentLabelError(f"{sample_id}: invalid cause_rule shape")
    if event_id:
        if event_id not in local_ids:
            raise StudentLabelError(f"{sample_id}: dangling event_local_id")
        return
    if op not in _GATE_CODES or len(children) < 2:
        raise StudentLabelError(f"{sample_id}: invalid AND/OR expression")
    for child in children:
        if not isinstance(child, dict):
            raise StudentLabelError(f"{sample_id}: invalid expression child")
        _validate_expression(child, local_ids, sample_id)


def validate_student_target(
    target: dict[str, Any],
    chunk_text: str,
    *,
    sample_id: str = "sample",
) -> None:
    """Raise :class:`StudentLabelError` if ``target`` is not trainable.

    The check is deliberately narrow: it enforces the fields the student must
    emit and the verbatim-evidence rule.  Ontology class/relation checks stay
    in the existing FaultCandidateValidator, which needs an ontology registry.
    """
    if not isinstance(target, dict):
        raise StudentLabelError(f"{sample_id}: target must be an object")
    entities = target.get("entities")
    relations = target.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        raise StudentLabelError(
            f"{sample_id}: target must contain entities and relations arrays"
        )

    # Collect every local ID first: a teacher may list a parent before the
    # children its cause_rule references, and forward references are valid.
    local_ids: set[str] = set()
    for entity in entities:
        if not isinstance(entity, dict):
            raise StudentLabelError(f"{sample_id}: entity must be an object")
        local_id = entity.get("local_id")
        if not isinstance(local_id, str) or not local_id:
            raise StudentLabelError(f"{sample_id}: invalid local_id")
        if local_id in local_ids:
            raise StudentLabelError(f"{sample_id}: duplicate local_id {local_id}")
        local_ids.add(local_id)

    for entity in entities:
        local_id = entity["local_id"]
        if entity.get("class") != "FaultEvent":
            raise StudentLabelError(
                f"{sample_id}: {local_id} must be a FaultEvent"
            )
        if not str(entity.get("name") or "").strip():
            raise StudentLabelError(f"{sample_id}: {local_id} has no name")
        evidence = entity.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise StudentLabelError(f"{sample_id}: {local_id} has no evidence")
        for item in evidence:
            if not isinstance(item, dict):
                raise StudentLabelError(
                    f"{sample_id}: {local_id} evidence must be an object"
                )
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote:
                raise StudentLabelError(
                    f"{sample_id}: {local_id} has an empty quote"
                )
            if quote not in chunk_text:
                raise StudentLabelError(
                    f"{sample_id}: {local_id} quote is not verbatim: {quote!r}"
                )
        rule = entity.get("cause_rule")
        if rule is not None:
            if not isinstance(rule, dict):
                raise StudentLabelError(
                    f"{sample_id}: {local_id} cause_rule must be an object"
                )
            _validate_expression(rule, local_ids, sample_id)

    for relation in relations:
        if not isinstance(relation, dict):
            raise StudentLabelError(f"{sample_id}: relation must be an object")
        if relation.get("relation_code") != "CAUSES":
            raise StudentLabelError(
                f"{sample_id}: only CAUSES relations are trainable"
            )
        source = relation.get("source_local_id")
        target_id = relation.get("target_local_id")
        if source not in local_ids or target_id not in local_ids:
            raise StudentLabelError(f"{sample_id}: relation endpoint missing")
        evidence = relation.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise StudentLabelError(f"{sample_id}: relation has no evidence")
        for item in evidence:
            if not isinstance(item, dict):
                raise StudentLabelError(
                    f"{sample_id}: relation evidence must be an object"
                )
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote:
                raise StudentLabelError(
                    f"{sample_id}: relation has an empty quote"
                )
            if quote not in chunk_text:
                raise StudentLabelError(
                    f"{sample_id}: relation quote is not verbatim: {quote!r}"
                )
