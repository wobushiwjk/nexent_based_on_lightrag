"""An in-process fake of the six read-only MCP tools.

The fake exists so skill unit tests can run without a subprocess.  It returns
the same envelope shape as ``mcp_server.server._invoke``; the real transport is
covered separately by ``test_cli.py`` and ``tests/mcp/test_transport.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mcp_server.errors import DomainError
from mcp_server.service import DomainService

Override = Mapping[str, Any] | Callable[[dict[str, Any]], Mapping[str, Any]]


class FakeToolClient:
    """Dispatch tool calls to a DomainService, with per-tool overrides."""

    def __init__(
        self,
        service: DomainService | None = None,
        overrides: dict[str, Override] | None = None,
    ) -> None:
        self.service = service
        self.overrides = dict(overrides or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        override = self.overrides.get(name)
        if override is not None:
            value = (
                override(arguments)
                if callable(override)
                else override
            )
            return dict(value)
        if self.service is None:
            raise AssertionError(f"no fake configured for tool {name!r}")
        return _dispatch(self.service, name, arguments)


def envelope(
    *,
    ok: bool = True,
    data: dict[str, Any] | None = None,
    code: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    tenant_id: str = "tenant-demo",
) -> dict[str, Any]:
    """Build one MCP-shaped response envelope."""

    return {
        "ok": ok,
        "data": data,
        "error": (
            None
            if ok
            else {
                "code": code or "internal_error",
                "message": message or "error",
                "details": details or {},
            }
        ),
        "meta": {
            "ontology_version": "industrial-fault-v0.1",
            "schema_version": "industrial-fault-schema-v0.1",
            "tenant_id": tenant_id,
            "read_only": True,
        },
    }


def _dispatch(
    service: DomainService,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    tenant_id = arguments.get("tenant_id")
    try:
        if name == "get_ontology":
            return _dump(service.success(service.get_ontology()))
        if name == "get_equipment":
            return _dump(
                service.success(
                    service.get_equipment(
                        arguments["tenant_id"],
                        arguments["equipment_ref"],
                    ),
                    arguments["tenant_id"],
                )
            )
        if name == "search_faults":
            return _dump(
                service.success(
                    service.search_faults(
                        arguments["tenant_id"],
                        arguments["query"],
                        arguments.get("limit", 10),
                    ),
                    arguments["tenant_id"],
                )
            )
        if name == "get_fault_tree":
            return _dump(
                service.success(
                    service.get_fault_tree(
                        arguments["tenant_id"],
                        draft_id=arguments.get("draft_id"),
                        top_event_uid=arguments.get("top_event_uid"),
                    ),
                    arguments["tenant_id"],
                )
            )
        if name == "trace_evidence":
            return _dump(
                service.success(
                    service.trace_evidence(
                        arguments["tenant_id"],
                        arguments["target_type"],
                        arguments["target_id"],
                    ),
                    arguments["tenant_id"],
                )
            )
        if name == "validate_fault_tree":
            return _dump(
                service.success(
                    service.validate_fault_tree(
                        arguments["tenant_id"],
                        draft_id=arguments.get("draft_id"),
                        draft=arguments.get("draft"),
                    ),
                    arguments["tenant_id"],
                )
            )
    except DomainError as exc:
        return _dump(service.failure(exc, tenant_id))
    raise AssertionError(f"unknown tool {name!r}")


def _dump(response: Any) -> dict[str, Any]:
    return response.model_dump(mode="json")


__all__ = ["FakeToolClient", "envelope"]
