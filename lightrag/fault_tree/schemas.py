"""Schemas for fault-tree extraction candidates."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceCandidate(BaseModel):
    """A verbatim source fragment supplied by the extractor.

    The LLM only has to emit ``quote``.  The remaining fields are filled by a
    parser or review adapter that knows the source locator: ``evidence_id`` and
    ``chunk_id`` let an importer keep stable IDs across reruns, while
    ``page`` / ``section_path`` / ``char_start`` / ``char_end`` are optional
    locators.  When a locator is absent the projector computes the first
    occurrence from the source text.
    """

    quote: str = Field(min_length=1)
    evidence_id: str | None = None
    asset_id: str | None = None
    chunk_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    section_path: str | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    modality: Literal["text", "table", "image", "pdf", "office"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    extractor: str | None = None
    extraction_run_id: str | None = None
    review_status: str | None = None


class Expression(BaseModel):
    """A recursive fault-cause expression.

    A leaf references another fault event in the same chunk. An internal node
    contains at least two child expressions. The effect event is not part of
    this tree; it is supplied separately by the owning rule.
    """

    op: Literal["AND", "OR"] | None = None
    event_local_id: str | None = None
    children: list["Expression"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> "Expression":
        has_event = bool(self.event_local_id and self.event_local_id.strip())
        has_operator = self.op is not None
        if has_event == has_operator:
            raise ValueError(
                "expression must contain exactly one of event_local_id or op"
            )
        if has_operator and len(self.children) < 2:
            raise ValueError("AND/OR expressions require at least two children")
        if has_event and self.children:
            raise ValueError("event leaves cannot contain children")
        return self


class FaultEntityPayload(BaseModel):
    """Domain fields emitted alongside a LightRAG JSON entity."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    local_id: str = Field(min_length=1)
    class_name: str = Field(alias="class", min_length=1)
    ontology_version: str | None = None
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    cause_rule: Expression | None = None
    evidence: list[EvidenceCandidate] = Field(default_factory=list)


class FaultRelationPayload(BaseModel):
    """Domain fields emitted alongside a LightRAG JSON relationship.

    ``fact_id`` is optional and review-supplied.  A projector derives a stable
    ID from the endpoints and predicate when it is absent; fixing it in a
    curated extraction keeps reruns byte-comparable.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    local_id: str | None = None
    fact_id: str | None = None
    relation_code: str = Field(min_length=1)
    ontology_version: str | None = None
    source_ref: str | None = Field(default=None, alias="source")
    target_ref: str | None = Field(default=None, alias="target")
    source_local_id: str | None = None
    target_local_id: str | None = None
    gate_type_hint: Literal["AND", "OR"] | None = None
    gate_key: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceCandidate] = Field(default_factory=list)
    model_confidence: float | None = Field(default=None, ge=0, le=1)
    review_status: str | None = None

    @model_validator(mode="after")
    def validate_gate_group(self) -> "FaultRelationPayload":
        if self.gate_type_hint and not (self.gate_key or "").strip():
            raise ValueError("gate_key is required when gate_type_hint is set")
        return self


class FaultCandidateBatch(BaseModel):
    """Validated candidates produced from one source chunk.

    ``chunk_text`` and ``file_path`` are filled by the validator so a sink can
    project evidence without reaching back into LightRAG's chunk storage.
    """

    chunk_id: str
    chunk_text: str = ""
    asset_id: str | None = None
    file_path: str | None = None
    entities: list[FaultEntityPayload] = Field(default_factory=list)
    relations: list[FaultRelationPayload] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


Expression.model_rebuild()
