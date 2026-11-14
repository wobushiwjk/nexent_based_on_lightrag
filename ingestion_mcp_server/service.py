"""Business envelope for upload ingestion tool calls."""

from __future__ import annotations

from typing import Any, Callable

from lightrag.fault_tree.ingestion import (
    DocumentIngestionService,
    IngestionError,
)
from mcp_server.models import ToolErrorPayload, ToolMeta, ToolResponse


class IngestionToolService:
    """Adapt the domain ingestion service to the common MCP envelope."""

    def __init__(self, service: DocumentIngestionService) -> None:
        self.service = service

    def success(
        self,
        data: dict[str, Any],
        *,
        tenant_id: str | None = None,
        read_only: bool = False,
    ) -> ToolResponse:
        ontology = self.service.ontology
        return ToolResponse(
            ok=True,
            data=data,
            meta=ToolMeta(
                ontology_version=ontology.version,
                schema_version=ontology.schema_version or "",
                tenant_id=tenant_id,
                read_only=read_only,
            ),
        )

    def failure(
        self,
        exc: IngestionError,
        *,
        tenant_id: str | None = None,
    ) -> ToolResponse:
        ontology = self.service.ontology
        return ToolResponse(
            ok=False,
            error=ToolErrorPayload(
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
            meta=ToolMeta(
                ontology_version=ontology.version,
                schema_version=ontology.schema_version or "",
                tenant_id=tenant_id,
                read_only=False,
            ),
        )


def invoke(
    service: IngestionToolService,
    action: Callable[[], dict[str, Any]],
    *,
    tenant_id: str | None = None,
    read_only: bool = False,
) -> ToolResponse:
    try:
        return service.success(
            action(),
            tenant_id=tenant_id,
            read_only=read_only,
        )
    except IngestionError as exc:
        return service.failure(exc, tenant_id=tenant_id)
    except Exception as exc:
        return service.failure(
            IngestionError(
                "internal_error",
                f"unexpected ingestion error: {type(exc).__name__}: {exc}",
            ),
            tenant_id=tenant_id,
        )


__all__ = ["IngestionToolService", "invoke"]
