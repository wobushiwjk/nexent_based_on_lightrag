"""Ontology-change-draft skill: proposal -> reviewed diff, never an apply."""

from __future__ import annotations

from skills.ontology_change_draft.handler import (
    SKILL_NAME,
    SKILL_VERSION,
    OntologyChangeRequest,
    OntologyDraftResponse,
    run,
)

__all__ = [
    "SKILL_NAME",
    "SKILL_VERSION",
    "OntologyChangeRequest",
    "OntologyDraftResponse",
    "run",
]
