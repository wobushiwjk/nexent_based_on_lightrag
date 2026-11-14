"""Register ontology governance service methods as read-only MCP tools."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from governance_mcp_server.service import (
    GovernanceService,
    GovernanceServiceError,
)
from lightrag.fault_tree.governance.models import OntologyStandardDraftRequest
from mcp_server.models import ToolResponse

logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

DRAFT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

REVIEW_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SERVER_INSTRUCTIONS = """
Read-only ontology governance tools for the industrial fault domain.

Use these tools whenever the user asks which ontology version is active, what
versions exist, what a historical version contains, or which ontology changes
have been recorded. Never infer a version from domain data without calling
get_ontology_governance_status.

Every tool returns the project envelope:
{"ok": bool, "data": object|null, "error": {code, message, details}|null,
 "meta": {ontology_version, schema_version, tenant_id, read_only}}.

These tools and the industrial-fault-domain tools are separate MCP services.

`create_ontology_change_draft` saves a reviewable draft only. It never
approves, migrates, activates, or publishes an ontology version.

Review, publish, and rollback tools require explicit user confirmation. The
Agent must never infer approval from a general request to "continue".
""".strip()


def build_server(
    service: GovernanceService,
    *,
    name: str = "ontology-governance",
    log_level: str = "INFO",
    host: str = "127.0.0.1",
    port: int = 8766,
) -> FastMCP:
    """Build one FastMCP server bound to a governance store."""

    mcp = FastMCP(
        name=name,
        instructions=SERVER_INSTRUCTIONS,
        log_level=log_level,
        host=host,
        port=port,
    )

    @mcp.tool(
        name="get_ontology_governance_status",
        title="Get ontology governance status",
        description=(
            "Return whether ontology governance is initialized, which ontology "
            "version is active, and how many immutable versions and change "
            "sets are stored."
        ),
        annotations=READ_ONLY,
    )
    def get_ontology_governance_status() -> ToolResponse:
        return _invoke(service, service.governance_status)

    @mcp.tool(
        name="list_ontology_versions",
        title="List ontology versions",
        description=(
            "List every installed immutable ontology version with schema, "
            "hash, installation metadata, and the active marker."
        ),
        annotations=READ_ONLY,
    )
    def list_ontology_versions() -> ToolResponse:
        return _invoke(service, service.list_versions)

    @mcp.tool(
        name="get_ontology_version",
        title="Get ontology version",
        description=(
            "Return one historical or active ontology version, including its "
            "manifest, classes, relations, schema references, and schema "
            "artifact hashes. Set include_schema=true to return Schema JSON."
        ),
        annotations=READ_ONLY,
    )
    def get_ontology_version(
        version: str,
        include_schema: bool = False,
    ) -> ToolResponse:
        return _invoke(
            service,
            lambda: service.get_version(
                version,
                include_schema=include_schema,
            ),
        )

    @mcp.tool(
        name="list_ontology_changes",
        title="List ontology changes",
        description=(
            "List immutable ontology change sets and the latest audit action "
            "recorded for each change."
        ),
        annotations=READ_ONLY,
    )
    def list_ontology_changes(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ToolResponse:
        return _invoke(service, lambda: service.list_changes(limit))

    @mcp.tool(
        name="get_ontology_change",
        title="Get ontology change",
        description=(
            "Return one ontology change set and its immutable review-event timeline."
        ),
        annotations=READ_ONLY,
    )
    def get_ontology_change(
        change_id: str,
        include_source: bool = False,
    ) -> ToolResponse:
        return _invoke(
            service,
            lambda: service.get_change(
                change_id,
                include_source=include_source,
            ),
        )

    @mcp.tool(
        name="create_ontology_change_draft",
        title="Create ontology change draft",
        description=(
            "Validate a standard excerpt and save an immutable ontology change "
            "draft. Every operation must include at least one verbatim evidence "
            "quote from source_text. This writes a draft only; it never "
            "approves, migrates, activates, or publishes an ontology version. "
            "Supported operations: kind=class|relation|constraint|gap, "
            "action=add|modify|deprecate|remove. Put the proposed definition in "
            "`after`, and evidence as [{quote, source_name, page?, "
            "section_path?, char_start?, char_end?}]."
        ),
        annotations=DRAFT_WRITE,
    )
    def create_ontology_change_draft(
        change_id: str,
        proposed_version: str,
        author: str,
        rationale: str,
        source_name: str,
        source_text: str,
        operations: list[dict[str, Any]],
    ) -> ToolResponse:
        def action() -> dict[str, Any]:
            try:
                request = OntologyStandardDraftRequest(
                    change_id=change_id,
                    proposed_version=proposed_version,
                    author=author,
                    rationale=rationale,
                    source_name=source_name,
                    source_text=source_text,
                    operations=operations,
                )
            except ValidationError as exc:
                raise GovernanceServiceError(
                    "invalid_argument",
                    "operations do not match the ontology draft contract",
                    {"errors": _validation_errors(exc)},
                ) from exc
            return service.create_standard_draft(request)

        return _invoke(service, action)

    @mcp.tool(
        name="submit_ontology_change_review",
        title="Submit ontology change for review",
        description=(
            "Submit a draft or rejected change for explicit human review. "
            "This changes review state only; it does not approve or publish."
        ),
        annotations=REVIEW_WRITE,
    )
    def submit_ontology_change_review(
        change_id: str,
        actor: str,
        reason: str | None = None,
    ) -> ToolResponse:
        return _invoke(
            service,
            lambda: service.submit_change_review(
                change_id,
                actor=actor,
                reason=reason,
            ),
        )

    @mcp.tool(
        name="review_ontology_change",
        title="Review ontology change",
        description=(
            "Approve or reject a submitted ontology change. Use only after the "
            "user explicitly confirms the decision for this change ID. "
            "Rejections require a reason."
        ),
        annotations=REVIEW_WRITE,
    )
    def review_ontology_change(
        change_id: str,
        decision: Literal["approve", "reject"],
        actor: str,
        reason: str | None = None,
        confirm: bool = False,
    ) -> ToolResponse:
        def action() -> dict[str, Any]:
            if not confirm:
                raise GovernanceServiceError(
                    "invalid_argument",
                    "explicit confirmation is required for a review decision",
                    {"change_id": change_id, "decision": decision},
                )
            return service.review_ontology_change(
                change_id,
                decision=decision,
                actor=actor,
                reason=reason,
            )

        return _invoke(service, action)

    @mcp.tool(
        name="analyze_ontology_change_impact",
        title="Analyze ontology change impact",
        description=(
            "Return a readable diff plus affected tenants, entities, facts, "
            "rules, fault trees, schema files and test suites. Read-only."
        ),
        annotations=READ_ONLY,
    )
    def analyze_ontology_change_impact(change_id: str) -> ToolResponse:
        return _invoke(
            service,
            lambda: service.analyze_change_impact(change_id),
        )

    @mcp.tool(
        name="publish_ontology_change",
        title="Publish approved ontology change",
        description=(
            "Build migrated tenant snapshots, atomically activate the ontology "
            "version and new snapshots, and append a publication event. Run "
            "with dry_run=true first. A real publication requires confirm=true. "
            "Old snapshots are retained."
        ),
        annotations=REVIEW_WRITE,
    )
    def publish_ontology_change(
        change_id: str,
        actor: str,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> ToolResponse:
        def action() -> dict[str, Any]:
            if not dry_run and not confirm:
                raise GovernanceServiceError(
                    "invalid_argument",
                    "explicit confirmation is required for publication",
                    {"change_id": change_id},
                )
            return service.publish_change(
                change_id,
                actor=actor,
                dry_run=dry_run,
            )

        return _invoke(service, action)

    @mcp.tool(
        name="rollback_ontology_publication",
        title="Rollback ontology publication",
        description=(
            "Switch ontology and tenant pointers back to exact pre-publication "
            "snapshots. Requires a reason and explicit confirmation. No snapshot "
            "is deleted."
        ),
        annotations=REVIEW_WRITE,
    )
    def rollback_ontology_publication(
        change_id: str,
        actor: str,
        reason: str,
        confirm: bool = False,
    ) -> ToolResponse:
        def action() -> dict[str, Any]:
            if not confirm:
                raise GovernanceServiceError(
                    "invalid_argument",
                    "explicit confirmation is required for rollback",
                    {"change_id": change_id},
                )
            return service.rollback_publication(
                change_id,
                actor=actor,
                reason=reason,
            )

        return _invoke(service, action)

    @mcp.tool(
        name="list_tenant_snapshot_history",
        title="List tenant snapshot history",
        description=(
            "List every preserved runtime snapshot for one tenant, including "
            "ontology/schema versions and the active run. Read-only."
        ),
        annotations=READ_ONLY,
    )
    def list_tenant_snapshot_history(tenant_id: str) -> ToolResponse:
        return _invoke(
            service,
            lambda: service.tenant_snapshot_history(tenant_id),
        )

    return mcp


def _invoke(service: GovernanceService, action) -> ToolResponse:
    try:
        return service.success(action())
    except GovernanceServiceError as exc:
        return service.failure(exc)
    except Exception:
        logger.exception("unhandled error in ontology governance MCP tool")
        return service.failure(
            GovernanceServiceError(
                "internal_error",
                "unexpected ontology governance server error",
                {},
            )
        )


def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "loc": ".".join(str(part) for part in error.get("loc", ())),
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()[:20]
    ]


__all__ = ["SERVER_INSTRUCTIONS", "build_server"]
