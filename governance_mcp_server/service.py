"""Service methods behind the ontology governance MCP tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
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
from lightrag.fault_tree.governance.evolution import (
    OntologyEvolutionError,
    activate_tenant_migrations,
    apply_change_to_ontology,
    prepare_tenant_migrations,
    rollback_tenant_migrations,
)
from lightrag.fault_tree.governance.impact import analyze_ontology_impact
from lightrag.fault_tree.governance.models import (
    OntologyPublicationRecord,
    OntologyStandardDraftRequest,
    ReviewEvent,
)
from lightrag.fault_tree.governance.review import (
    OntologyReviewError,
    append_lifecycle_event,
    change_status,
    review_change,
    submit_for_review,
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

    def __init__(
        self,
        governance_root: str | Path,
        *,
        domain_storage_root: str | Path | None = None,
    ) -> None:
        self.root = Path(governance_root).resolve()
        self.domain_storage_root = (
            Path(domain_storage_root).resolve()
            if domain_storage_root is not None
            else self.root.parent
        )
        self.versions = OntologyVersionStore(self.root)
        self.changes = OntologyChangeStore(self.root)
        self._write_lock = RLock()

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
                        "status": change_status(events),
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
            "status": change_status(events),
            "review_events": [_review_event(event) for event in events],
            "source": (
                source[0].model_dump(mode="json") if source is not None else None
            ),
        }
        if include_source and source is not None:
            data["source_text"] = source[1]
        publication = self.changes.load_publication_record(change_id)
        data["publication"] = (
            publication.model_dump(mode="json") if publication is not None else None
        )
        return data

    def submit_change_review(
        self,
        change_id: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Submit a draft for explicit human review."""

        change = self._load_change(change_id)[0]
        try:
            event = submit_for_review(
                self.changes,
                change,
                actor=actor,
                reason=reason,
            )
        except OntologyReviewError as exc:
            raise GovernanceServiceError(
                "invalid_argument",
                str(exc),
                {"change_id": change_id},
            ) from exc
        return self._review_result(change_id, event)

    def review_ontology_change(
        self,
        change_id: str,
        *,
        decision: str,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record an explicit approve or reject decision."""

        change = self._load_change(change_id)[0]
        try:
            event = review_change(
                self.changes,
                change,
                decision=decision,
                actor=actor,
                reason=reason,
            )
        except OntologyReviewError as exc:
            raise GovernanceServiceError(
                "invalid_argument",
                str(exc),
                {"change_id": change_id, "decision": decision},
            ) from exc
        return self._review_result(change_id, event)

    def analyze_change_impact(self, change_id: str) -> dict[str, Any]:
        """Return current deterministic impact for one change."""

        change = self._load_change(change_id)[0]
        active = self._active_ontology()
        return analyze_ontology_impact(
            change,
            ontology_document=active.document(),
            domain_storage_root=self.domain_storage_root,
        )

    def publish_change(
        self,
        change_id: str,
        *,
        actor: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Migrate tenant snapshots and publish one approved ontology change."""

        with self._write_lock:
            change, _events, status = self._load_change(change_id)
            publication = self.changes.load_publication_record(change_id)
            if status == "published" and publication is not None:
                return {
                    "status": "published",
                    "change_id": change_id,
                    "publication": publication.model_dump(mode="json"),
                }
            if publication is not None and publication.status == "published":
                append_lifecycle_event(
                    self.changes,
                    change,
                    action="publish",
                    actor=actor,
                    reason=f"Published ontology {change.proposed_version}",
                    metadata={
                        "schema_version": publication.schema_version,
                        "tenants": [
                            item.model_dump(mode="json") for item in publication.tenants
                        ],
                    },
                )
                return {
                    "status": "published",
                    "change_id": change_id,
                    "publication": publication.model_dump(mode="json"),
                }
            if status != "approved":
                raise GovernanceServiceError(
                    "invalid_argument",
                    f"cannot publish change in status {status!r}",
                    {"change_id": change_id},
                )
            if publication is None:
                active = self._active_ontology()
                if active.manifest.ontology_version != change.base_version:
                    raise GovernanceServiceError(
                        "invalid_argument",
                        "active ontology changed after this draft was created",
                        {
                            "change_id": change_id,
                            "base_version": change.base_version,
                            "active_version": active.manifest.ontology_version,
                        },
                    )
                try:
                    _, content, schema_version = apply_change_to_ontology(
                        change,
                        active_document=active.document(),
                    )
                except OntologyEvolutionError as exc:
                    raise GovernanceServiceError(
                        "invalid_argument",
                        "reviewed change does not apply to the active ontology",
                        {"change_id": change_id, "reason": str(exc)},
                    ) from exc
                if dry_run:
                    tenant_ids = self._tenant_ids()
                    return {
                        "status": "dry_run",
                        "change_id": change_id,
                        "ontology_version": change.proposed_version,
                        "schema_version": schema_version,
                        "tenants_to_migrate": tenant_ids,
                        "tenant_count": len(tenant_ids),
                        "impact": self.analyze_change_impact(change_id),
                    }
                try:
                    self.versions.install(
                        self._write_ontology_candidate(change, content),
                        actor=actor,
                        notes=f"Prepared by change {change_id}",
                    )
                    migrations = prepare_tenant_migrations(
                        self.domain_storage_root,
                        change=change,
                        schema_version=schema_version,
                    )
                    publication = OntologyPublicationRecord(
                        change_id=change_id,
                        ontology_version=change.proposed_version,
                        schema_version=schema_version,
                        previous_ontology_version=active.manifest.ontology_version,
                        previous_schema_version=(
                            active.manifest.schema_version or "unknown"
                        ),
                        prepared_at=_utc_now(),
                        prepared_by=actor,
                        status="prepared",
                        tenants=migrations,
                    )
                    self.changes.write_publication_record(publication)
                except Exception as exc:
                    raise GovernanceServiceError(
                        "state_invalid",
                        "publication preparation failed; previous snapshots "
                        "remain active",
                        {"change_id": change_id, "reason": str(exc)},
                    ) from exc

            try:
                self.versions.activate(change.proposed_version, actor=actor)
                activate_tenant_migrations(
                    self.domain_storage_root,
                    publication.tenants,
                    ontology_version=publication.ontology_version,
                    schema_version=publication.schema_version,
                )
            except Exception as exc:
                raise GovernanceServiceError(
                    "state_invalid",
                    "publication preparation or activation failed; previous "
                    "snapshots remain available for retry",
                    {"change_id": change_id, "reason": str(exc)},
                ) from exc
            completed = publication.model_copy(update={"status": "published"})
            self.changes.write_publication_record(completed)
            append_lifecycle_event(
                self.changes,
                change,
                action="publish",
                actor=actor,
                reason=f"Published ontology {change.proposed_version}",
                metadata={
                    "schema_version": schema_version,
                    "tenants": [
                        item.model_dump(mode="json") for item in completed.tenants
                    ],
                },
            )
            return {
                "status": "published",
                "change_id": change_id,
                "publication": completed.model_dump(mode="json"),
            }

    def rollback_publication(
        self,
        change_id: str,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Return ontology and tenant pointers to pre-publication snapshots."""

        if not reason.strip():
            raise GovernanceServiceError(
                "invalid_argument",
                "rollback requires a reason",
                {"change_id": change_id},
            )
        with self._write_lock:
            change, _events, status = self._load_change(change_id)
            publication = self.changes.load_publication_record(change_id)
            if publication is None:
                raise GovernanceServiceError(
                    "not_found",
                    "change has no publication record",
                    {"change_id": change_id},
                )
            if status == "rolled_back" and publication.status == "rolled_back":
                return {
                    "status": "rolled_back",
                    "change_id": change_id,
                    "publication": publication.model_dump(mode="json"),
                }
            if status != "published":
                raise GovernanceServiceError(
                    "invalid_argument",
                    f"cannot rollback change in status {status!r}",
                    {"change_id": change_id},
                )
            try:
                self.versions.activate(
                    publication.previous_ontology_version,
                    actor=actor,
                )
                rollback_tenant_migrations(
                    self.domain_storage_root,
                    publication.tenants,
                    ontology_version=publication.previous_ontology_version,
                    schema_version=publication.previous_schema_version,
                )
            except Exception as exc:
                raise GovernanceServiceError(
                    "state_invalid",
                    "rollback failed; no snapshot was deleted",
                    {"change_id": change_id, "reason": str(exc)},
                ) from exc
            rolled_back = publication.model_copy(update={"status": "rolled_back"})
            self.changes.write_publication_record(rolled_back)
            append_lifecycle_event(
                self.changes,
                change,
                action="rollback",
                actor=actor,
                reason=reason,
                metadata={
                    "restored_ontology_version": (
                        publication.previous_ontology_version
                    ),
                    "tenants": [
                        item.model_dump(mode="json") for item in rolled_back.tenants
                    ],
                },
            )
            return {
                "status": "rolled_back",
                "change_id": change_id,
                "publication": rolled_back.model_dump(mode="json"),
            }

    def tenant_snapshot_history(self, tenant_id: str) -> dict[str, Any]:
        """List preserved domain runs and identify the active one."""

        if (
            not tenant_id
            or Path(tenant_id).name != tenant_id
            or tenant_id in {".", ".."}
        ):
            raise GovernanceServiceError(
                "invalid_argument",
                "tenant_id is invalid",
                {"tenant_id": tenant_id},
            )
        tenant_dir = self.domain_storage_root / "tenants" / tenant_id
        active_path = tenant_dir / "active.json"
        if not active_path.is_file():
            raise GovernanceServiceError(
                "not_found",
                f"tenant {tenant_id!r} is not available",
                {"tenant_id": tenant_id},
            )
        try:
            active = json.loads(active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GovernanceServiceError(
                "state_invalid",
                "tenant active pointer is invalid",
                {"tenant_id": tenant_id, "reason": str(exc)},
            ) from exc
        runs = []
        for manifest_path in sorted((tenant_dir / "runs").glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            runs.append(
                {
                    "run_id": manifest.get("run_id"),
                    "ontology_version": manifest.get("ontology_version"),
                    "schema_version": manifest.get("schema_version"),
                    "bundle_version": manifest.get("bundle_version"),
                    "migrated_by_change": manifest.get("migrated_by_change"),
                    "active": manifest.get("run_id") == active.get("run_id"),
                }
            )
        return {
            "tenant_id": tenant_id,
            "active_run_id": active.get("run_id"),
            "runs": runs,
        }

    def _load_change(
        self,
        change_id: str,
    ) -> tuple[Any, tuple[ReviewEvent, ...], str]:
        try:
            change = self.changes.load_change_set(change_id)
            events = self.changes.list_review_events(change_id)
        except OntologyGovernanceError as exc:
            raise GovernanceServiceError(
                "not_found",
                f"ontology change {change_id!r} is not available",
                {"change_id": change_id, "reason": str(exc)},
            ) from exc
        return change, events, change_status(events)

    def _review_result(
        self,
        change_id: str,
        event: ReviewEvent,
    ) -> dict[str, Any]:
        change, events, status = self._load_change(change_id)
        return {
            "change_id": change_id,
            "status": status,
            "event": _review_event(event),
            "review_events": [_review_event(item) for item in events],
            "change": change.model_dump(mode="json"),
        }

    def _active_ontology(self):
        try:
            return self.versions.load_active()
        except OntologyGovernanceError as exc:
            raise GovernanceServiceError(
                "state_invalid",
                "active ontology is unavailable",
                {"reason": str(exc)},
            ) from exc

    def _tenant_ids(self) -> list[str]:
        tenants = self.domain_storage_root / "tenants"
        if not tenants.is_dir():
            return []
        return sorted(
            path.name
            for path in tenants.iterdir()
            if path.is_dir() and (path / "active.json").is_file()
        )

    def _write_ontology_candidate(self, change, content: bytes) -> Path:
        path = self.root / "staging" / f"{change.change_id}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "GovernanceService",
    "GovernanceServiceError",
]
