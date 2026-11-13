"""Serializable contracts for ontology versions, changes, and review audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GOVERNANCE_SCHEMA_VERSION = "ontology-governance-v1"
SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


class GovernanceModel(BaseModel):
    """Base model that rejects fields outside the declared contract."""

    model_config = ConfigDict(extra="forbid")


class OntologyVersionManifest(GovernanceModel):
    """Immutable metadata for one installed ontology version."""

    governance_schema_version: Literal[GOVERNANCE_SCHEMA_VERSION] = (
        GOVERNANCE_SCHEMA_VERSION
    )
    ontology_version: str = Field(pattern=SAFE_ID_PATTERN)
    schema_version: str | None = None
    ontology_sha256: str = Field(pattern=SHA256_PATTERN)
    ontology_size: int = Field(ge=1)
    installed_at: datetime
    installed_by: str = Field(min_length=1)
    notes: str | None = None


class ActiveOntologyPointer(GovernanceModel):
    """The atomically switched pointer to the active ontology version."""

    governance_schema_version: Literal[GOVERNANCE_SCHEMA_VERSION] = (
        GOVERNANCE_SCHEMA_VERSION
    )
    active_version: str = Field(pattern=SAFE_ID_PATTERN)
    ontology_sha256: str = Field(pattern=SHA256_PATTERN)
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    updated_at: datetime
    updated_by: str = Field(min_length=1)
    previous_version: str | None = Field(default=None, pattern=SAFE_ID_PATTERN)


class ChangeEvidence(GovernanceModel):
    """A verbatim source fragment supporting one proposed change."""

    quote: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    section_path: str | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_span(self) -> "ChangeEvidence":
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end <= self.char_start
        ):
            raise ValueError("char_end must be greater than char_start")
        return self


class OntologyChangeOperation(GovernanceModel):
    """One proposed ontology or standard-gap change."""

    kind: Literal["class", "relation", "constraint", "gap"]
    action: Literal["add", "modify", "deprecate", "remove"]
    code: str = Field(min_length=1)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    evidence: list[ChangeEvidence] = Field(default_factory=list)


class OntologyChangeSet(GovernanceModel):
    """An immutable proposal from which review events are appended."""

    change_id: str = Field(pattern=SAFE_ID_PATTERN)
    base_version: str = Field(pattern=SAFE_ID_PATTERN)
    proposed_version: str = Field(pattern=SAFE_ID_PATTERN)
    author: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    created_at: datetime
    source_standard: str | None = None
    operations: list[OntologyChangeOperation] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_version_transition(self) -> "OntologyChangeSet":
        if self.base_version == self.proposed_version:
            raise ValueError("proposed_version must differ from base_version")
        return self


class StandardSourceManifest(GovernanceModel):
    """Immutable metadata for the standard excerpt attached to a draft."""

    source_name: str = Field(min_length=1)
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    source_size: int = Field(ge=1)
    stored_at: datetime
    stored_by: str = Field(min_length=1)


class OntologyStandardDraftRequest(GovernanceModel):
    """Input contract for extracting a draft from a standard excerpt."""

    change_id: str = Field(pattern=SAFE_ID_PATTERN)
    proposed_version: str = Field(pattern=SAFE_ID_PATTERN)
    author: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    operations: list[OntologyChangeOperation] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_operation_evidence(self) -> "OntologyStandardDraftRequest":
        seen: set[tuple[str, str, str]] = set()
        for operation in self.operations:
            key = (operation.kind, operation.action, operation.code)
            if key in seen:
                raise ValueError(
                    "duplicate operation "
                    f"{operation.action} {operation.kind} {operation.code}"
                )
            seen.add(key)
            if not operation.evidence:
                raise ValueError(
                    f"operation {operation.action} {operation.kind} "
                    f"{operation.code} needs at least one evidence quote"
                )
        return self


ReviewAction = Literal[
    "submit",
    "comment",
    "approve",
    "reject",
    "withdraw",
    "publish",
    "rollback",
]

OntologyChangeStatus = Literal[
    "draft",
    "under_review",
    "approved",
    "rejected",
    "published",
    "rolled_back",
]


class ReviewEvent(GovernanceModel):
    """One immutable audit event attached to an ontology change set."""

    event_id: str = Field(pattern=SAFE_ID_PATTERN)
    change_id: str = Field(pattern=SAFE_ID_PATTERN)
    action: ReviewAction
    actor: str = Field(min_length=1)
    occurred_at: datetime
    reason: str | None = None
    content_sha256: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class TenantMigrationRecord(GovernanceModel):
    """One tenant snapshot switch performed by a publication."""

    tenant_id: str = Field(min_length=1)
    previous_run_id: str = Field(pattern=SAFE_ID_PATTERN)
    new_run_id: str = Field(pattern=SAFE_ID_PATTERN)


class OntologyPublicationRecord(GovernanceModel):
    """Prepared or completed publication used for idempotent retry and rollback."""

    change_id: str = Field(pattern=SAFE_ID_PATTERN)
    ontology_version: str = Field(pattern=SAFE_ID_PATTERN)
    schema_version: str = Field(min_length=1)
    previous_ontology_version: str = Field(pattern=SAFE_ID_PATTERN)
    previous_schema_version: str = Field(min_length=1)
    prepared_at: datetime
    prepared_by: str = Field(min_length=1)
    status: Literal["prepared", "published", "rolled_back"] = "prepared"
    tenants: list[TenantMigrationRecord] = Field(default_factory=list)


__all__ = [
    "GOVERNANCE_SCHEMA_VERSION",
    "ActiveOntologyPointer",
    "ChangeEvidence",
    "GovernanceModel",
    "OntologyChangeOperation",
    "OntologyChangeSet",
    "OntologyChangeStatus",
    "OntologyPublicationRecord",
    "OntologyStandardDraftRequest",
    "OntologyVersionManifest",
    "ReviewAction",
    "ReviewEvent",
    "StandardSourceManifest",
    "TenantMigrationRecord",
]
