"""MCP tools that make uploaded documents usable from Nexent."""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from ingestion_mcp_server.service import IngestionToolService, invoke
from mcp_server.models import ToolResponse

logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

INGEST_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SERVER_INSTRUCTIONS = """
This MCP service connects files uploaded by the user in Nexent to the
industrial fault domain.

When the user says they uploaded or attached a document:
1. Call list_uploaded_documents.
2. If there is one unambiguous latest file, use its document_id. Prefer
   document_id over file_name when both are available.
3. For equipment/manual/inspection documents, call ingest_uploaded_document.
   It parses the file locally, runs the W7 student extraction model, validates
   every quote, and appends a new immutable tenant snapshot.
4. For a standard or regulation that should change the ontology, call
   read_uploaded_document first, then use the returned source_text and chunk
   locators with create_ontology_change_draft.

Never claim a file was imported unless ingest_uploaded_document returns
ok=true. Never invent a document_id. If several files match, ask the user
which one to use. Failed ingestion leaves the previous active snapshot intact.
""".strip()


def build_server(
    service: IngestionToolService,
    *,
    name: str = "industrial-fault-ingestion",
    log_level: str = "INFO",
    host: str = "127.0.0.1",
    port: int = 8767,
) -> FastMCP:
    """Build the upload ingestion MCP server."""

    mcp = FastMCP(
        name=name,
        instructions=SERVER_INSTRUCTIONS,
        log_level=log_level,
        host=host,
        port=port,
    )

    @mcp.tool(
        name="list_uploaded_documents",
        title="List uploaded documents",
        description=(
            "List recent PDF, CSV, XLSX, PNG/JPEG, Markdown and text files from "
            "the configured Nexent upload inbox. Returns document_id, filename, "
            "size and modification time."
        ),
        annotations=READ_ONLY,
    )
    def list_uploaded_documents(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ToolResponse:
        return invoke(
            service,
            lambda: {
                "documents": [
                    item.to_dict() for item in service.service.list_uploads(limit=limit)
                ]
            },
            read_only=True,
        )

    @mcp.tool(
        name="read_uploaded_document",
        title="Read uploaded document",
        description=(
            "Parse one uploaded document locally and return its exact transcript "
            "with page/section/character locators. Use this for standards before "
            "creating an ontology change draft. It does not modify the domain."
        ),
        annotations=READ_ONLY,
    )
    def read_uploaded_document(
        document_id: str | None = None,
        file_name: str | None = None,
        max_chars: Annotated[int, Field(ge=1000, le=200000)] = 50000,
    ) -> ToolResponse:
        return invoke(
            service,
            lambda: service.service.read_uploaded_document(
                document_id=document_id,
                file_name=file_name,
                max_chars=max_chars,
            ),
            read_only=True,
        )

    @mcp.tool(
        name="ingest_uploaded_document",
        title="Ingest uploaded equipment document",
        description=(
            "Parse an uploaded equipment document, run the configured local W7 "
            "student model, validate evidence quotes, and append the resulting "
            "fault events, causes, evidence and source asset to the tenant's "
            "active domain snapshot. Pass equipment_ref (tag/name/UID) when the "
            "user identified the asset so extracted faults are linked by a "
            "fact-backed HAS_FAULT relation. The old snapshot is retained."
        ),
        annotations=INGEST_WRITE,
    )
    def ingest_uploaded_document(
        tenant_id: str,
        document_id: str | None = None,
        file_name: str | None = None,
        equipment_ref: str | None = None,
    ) -> ToolResponse:
        return invoke(
            service,
            lambda: service.service.ingest_uploaded_document(
                tenant_id=tenant_id,
                document_id=document_id,
                file_name=file_name,
                equipment_ref=equipment_ref,
            ),
            tenant_id=tenant_id,
        )

    @mcp.tool(
        name="get_upload_ingestion_status",
        title="Get upload ingestion status",
        description=(
            "Return success or failure status for documents ingested under the "
            "current tenant. Read-only."
        ),
        annotations=READ_ONLY,
    )
    def get_upload_ingestion_status(
        tenant_id: str,
        document_id: str | None = None,
    ) -> ToolResponse:
        return invoke(
            service,
            lambda: service.service.status(
                tenant_id=tenant_id,
                document_id=document_id,
            ),
            tenant_id=tenant_id,
            read_only=True,
        )

    return mcp


__all__ = ["SERVER_INSTRUCTIONS", "build_server"]
