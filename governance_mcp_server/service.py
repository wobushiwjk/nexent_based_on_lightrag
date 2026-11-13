"""Service methods behind the ontology governance MCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lightrag.fault_tree.governance import (
    ChangeSetConflictError,
    OntologyChangeStore,
    OntologyGovernanceError,
    OntologyVersionStore,
)
from lightrag.fault_tree.governance.drafting import (
    OntologyDraftConflictError,
    build_standard_change_set,
)
from lightrag.fault_tree.governance.models import (
    OntologyStandardDraftRequest,
    ReviewEvent,
)
from mcp_server.models import ToolErrorPayload, ToolMeta, ToolResponse


class GovernanceServiceError(Exception):
    """A safe governance error returned through the MCP envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class GovernanceService:
    """Expose immutable ontology governance state without mutating it."""

    def __init__(self, governance_root: str | Path) -> None:
        self.root = Path(governance_root).resolve()
        self.versions = OntologyVersionStore(self.root)
        self.changes = OntologyChangeStore(self.root)

    def meta(self) -> ToolMeta:
        try:
            active = self.versions.read_active()
        except OntologyGovernanceError:
            active = None
        if active is None:
            return ToolMeta(
                ontology_version="",
                schema_version="",
                tenant_id=None,
                read_only=True,
            )
        loaded = self.versions.load_version(active.active_version)
        return ToolMeta(
            ontology_version=loaded.manifest.ontology_version,
            schema_version=loaded.manifest.schema_version or "",
            tenant_id=None,
            read_only=True,
        )

    def success(self, data: dict[str, Any]) -> ToolResponse:
        return ToolResponse(ok=True, data=data, meta=self.meta())

    def failure(self, error: GovernanceServiceError) -> ToolResponse:
        return ToolResponse(
            ok=False,
            error=ToolErrorPayload(
                code=error.code,
                message=error.message,
                details=error.details,
            ),
            meta=self.meta(),
        )

    def governance_status(self) -> dict[str, Any]:
        """Return whether governance is initialized and what is active."""

        versions = self.versions.list_versions()
        initialized = self.versions.active_path.is_file()
        if not initialized:
            return {
                "initialized": False,
                "active_version": None,
                "previous_version": None,
                "installed_versions": list(versions),
                "version_count": len(versions),
                "change_count": self._change_count(),
                "message": (
                    "Ontology governance is not initialized. No version is "
                    "active; ask an administrator to run the governance "
                    "bootstrap/import flow."
                ),
            }
        try:
            pointer = self.versions.read_active()
            loaded = self.versions.load_version(pointer.active_version)
        except OntologyGovernanceError as exc:
            raise GovernanceServiceError(
                "state_invalid",
                "The active ontology governance pointer is invalid.",
                {"reason": str(exc)},
            ) from exc
        return {
            "initialized": True,
            "active_version": pointer.active_version,
            "previous_version": pointer.previous_version,
            "ontology_sha256": pointer.ontology_sha256,
            "schema_version": loaded.manifest.schema_version,
            "installed_at": loaded.manifest.installed_at.isoformat(),
            "installed_by": loaded.manifest.installed_by,
            "installed_versions": list(versions),
            "version_count": len(versions),
            "change_count": self._change_count(),
        }

    def list_versions(self) -> dict[str, Any]:
        """Return installed versions with manifest metadata."""

        active_version: str | None = None
        if self.versions.active_path.is_file():
            try:
                active_version = self.versions.read_active().active_version
            except OntologyGovernanceError as exc:
                raise GovernanceServiceError(
                    "state_invalid",
                    "Cannot determine the active ontology version.",
                    {"reason": str(exc)},
                ) from exc

        items: list[dict[str, Any]] = []
        for version in self.versions.list_versions():
            try:
                loaded = self.versions.load_version(version)
            except OntologyGovernanceError as exc:
                raise GovernanceServiceError(
                    "state_invalid",
                    f"Ontology version {version!r} cannot be read.",
                    {"version": version, "reason": str(exc)},
                ) from exc
            manifest = loaded.manifest
            items.append(
                {
                    "version": manifest.ontology_version,
                    "schema_version": manifest.schema_version,
                    "ontology_sha256": manifest.ontology_sha256,
                    "installed_at": manifest.installed_at.isoformat(),
                    "installed_by": manifest.installed_by,
                    "notes": manifest.notes,
                    "active": version == active_version,
                }
            )
        return {
            "active_version": active_version,
            "count": len(items),
            "versions": items,
        }

    def get_version(self, version: str) -> dict[str, Any]:
        """Return one immutable version and a small ontology summary."""

        try:
            loaded = self.versions.load_version(version)
            registry = loaded.registry()
        except OntologyGovernanceError as exc:
            raise GovernanceServiceError(
                "not_found",
                f"Ontology version {version!r} is not available.",
                {"version": version, "reason": str(exc)},
            ) from exc
        manifest = loaded.manifest
        active = (
            self.versions.read_active().active_version
            if self.versions.active_path.is_file()
            else None
        )
        return {
            "version": manifest.ontology_version,
            "schema_version": manifest.schema_version,
            "ontology_sha256": manifest.ontology_sha256,
            "ontology_size": manifest.ontology_size,
            "installed_at": manifest.installed_at.isoformat(),
            "installed_by": manifest.installed_by,
            "notes": manifest.notes,
            "active": manifest.ontology_version == active,
            "counts": {
                "classes": len(registry.classes),
                "relations": len(registry.relations),
            },
            "classes": [
                {
                    "code": definition.code,
                    "label": definition.label,
                    "parent": definition.parent,
                }
                for definition in sorted(
                    registry.classes.values(),
                    key=lambda item: item.code,
                )
            ],
            "relations": [
                {
                    "code": definition.code,
                    "source_types": sorted(definition.source_types),
                    "target_types": sorted(definition.target_types),
                }
                for definition in sorted(
                    registry.relations.values(),
                    key=lambda item: item.code,
                )
            ],
        }

    def list_changes(self, limit: int = 20) -> dict[str, Any]:
        """Return recent immutable change sets and their latest review action."""

        items: list[dict[str, Any]] = []
        if self.changes.changes_dir.is_dir():
            paths = sorted(
                (
                    path
                    for path in self.changes.changes_dir.iterdir()
                    if path.is_dir()
                    and not path.name.startswith(".")
                    and (path / "change.json").is_file()
                ),
                key=lambda path: path.name,
                reverse=True,
            )
            for path in paths[:limit]:
                change = self.changes.load_change_set(path.name)
                events = self.changes.list_review_events(change.change_id)
                items.append(
                    {
                        "change_id": change.change_id,
                        "base_version": change.base_version,
                        "proposed_version": change.proposed_version,
                        "author": change.author,
                        "created_at": change.created_at.isoformat(),
                        "operation_count": len(change.operations),
                        "latest_review_action": (events[-1].action if events else None),
                    }
                )
        return {"count": len(items), "changes": items}

    def create_standard_draft(
        self,
        request: OntologyStandardDraftRequest,
    ) -> dict[str, Any]:
        """Create an immutable draft from a standard excerpt.

        This never installs, activates, approves, or publishes an ontology
        version. The active pointer is read only so the server can stamp the
        correct base version.
        """

        if not self.versions.active_path.is_file():
            raise GovernanceServiceError(
                "state_invalid",
                "Ontology governance is not initialized.",
                {"root": str(self.root)},
            )
        try:
            pointer = self.versions.read_active()
            loaded = self.versions.load_version(pointer.active_version)
            change = build_standard_change_set(
                request,
                active_version=pointer.active_version,
                ontology_document=loaded.document(),
            )
            self.changes.create_change_set(
                change,
                source_text=request.source_text,
                source_name=request.source_name,
                stored_by=request.author,
            )
            source = self.changes.load_change_source(change.change_id)
        except (OntologyDraftConflictError, ChangeSetConflictError) as exc:
            raise GovernanceServiceError(
                "invalid_argument",
                str(exc),
                {"change_id": request.change_id},
            ) from exc
        except ValidationError as exc:
            raise GovernanceServiceError(
                "invalid_argument",
                "ontology change draft does not match the storage contract",
                {"errors": _validation_errors(exc)},
            ) from exc
        except OntologyGovernanceError as exc:
            raise GovernanceServiceError(
                "state_invalid",
                "The ontology governance store cannot accept this draft.",
                {"reason": str(exc)},
            ) from exc
        if source is None:
            raise GovernanceServiceError(
                "internal_error",
                "The draft was stored without its source snapshot.",
                {"change_id": change.change_id},
            )
        source_manifest, _ = source
        return {
            "change": change.model_dump(mode="json"),
            "source": source_manifest.model_dump(mode="json"),
            "status": "draft",
            "applied": False,
            "message": (
                "Ontology change draft saved. It has not been approved or published."
            ),
            "operation_count": len(change.operations),
            "evidence_quote_count": sum(
                len(operation.evidence) for operation in change.operations
            ),
        }

    def get_change(
        self,
        change_id: str,
        *,
        include_source: bool = False,
    ) -> dict[str, Any]:
        """Return one change set and its immutable review timeline."""

        try:
            change = self.changes.load_change_set(change_id)
            events = self.changes.list_review_events(change_id)
            source = self.changes.load_change_source(change_id)
        except OntologyGovernanceError as exc:
            raise GovernanceServiceError(
                "not_found",
                f"Ontology change {change_id!r} is not available.",
                {"change_id": change_id, "reason": str(exc)},
            ) from exc
        data: dict[str, Any] = {
            "change": change.model_dump(mode="json"),
            "review_events": [_review_event(event) for event in events],
            "source": (
                source[0].model_dump(mode="json") if source is not None else None
            ),
        }
        if include_source and source is not None:
            data["source_text"] = source[1]
        return data

    def _change_count(self) -> int:
        if not self.changes.changes_dir.is_dir():
            return 0
        return sum(
            1
            for path in self.changes.changes_dir.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and (path / "change.json").is_file()
        )


def _review_event(event: ReviewEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "action": event.action,
        "actor": event.actor,
        "occurred_at": event.occurred_at.isoformat(),
        "reason": event.reason,
    }


def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "loc": ".".join(str(part) for part in error.get("loc", ())),
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()[:20]
    ]


__all__ = [
    "GovernanceService",
    "GovernanceServiceError",
]
