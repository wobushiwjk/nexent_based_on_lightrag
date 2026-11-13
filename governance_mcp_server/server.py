"""Register ontology governance service methods as read-only MCP tools."""

from __future__ import annotations

import logging
from typing import Annotated, Any

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
            "manifest, classes, and relations."
        ),
        annotations=READ_ONLY,
    )
    def get_ontology_version(version: str) -> ToolResponse:
        return _invoke(service, lambda: service.get_version(version))

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
