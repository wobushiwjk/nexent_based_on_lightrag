"""Storage-backed read-only repository for imported fault-tree domains.

The runtime bundle contains entities, facts, rules, and the tree but **no
evidence quotes**.  Evidence rows live in SQLite and are exposed through lazy
mappings, so constructing the repository for 100k evidence records does not
load any quote text.  Tool contracts stay identical to the snapshot
repository.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

from lightrag.fault_tree.contracts import DomainBundle, Evidence
from lightrag.fault_tree.domain_validation import (
    errors_only,
    load_domain_bundle,
    validate_fault_tree_draft,
)
from lightrag.fault_tree.evidence_store import EvidenceStore
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.fault_tree.governance import OntologyGovernanceError, OntologyVersionStore
from mcp_server.errors import DomainError, DomainRepositoryError, NOT_FOUND
from mcp_server.repository import TenantIndex, _build_index

logger = logging.getLogger(__name__)


class SqliteEvidenceMapping(Mapping[str, Evidence]):
    """Read-only mapping that loads one evidence row per lookup."""

    def __init__(self, store: EvidenceStore, tenant_id: str) -> None:
        self._store = store
        self._tenant_id = tenant_id

    def __getitem__(self, evidence_id: str) -> Evidence:
        item = self._store.get(self._tenant_id, evidence_id)
        if item is None:
            raise KeyError(evidence_id)
        return item

    def __contains__(self, evidence_id: object) -> bool:
        return isinstance(evidence_id, str) and self._store.exists(
            self._tenant_id, evidence_id
        )

    def __iter__(self) -> Iterator[str]:
        return self._store.iter_ids(self._tenant_id)

    def __len__(self) -> int:
        return self._store.count(self._tenant_id)

    def get(
        self,
        evidence_id: str,
        default: Evidence | None = None,
    ) -> Evidence | None:
        item = self._store.get(self._tenant_id, evidence_id)
        return default if item is None else item


class SqliteTargetMapping(Mapping[tuple[str, str], tuple[str, ...]]):
    """Read-only target index backed by the SQLite lookup index."""

    def __init__(self, store: EvidenceStore, tenant_id: str) -> None:
        self._store = store
        self._tenant_id = tenant_id

    def __getitem__(self, key: tuple[str, str]) -> tuple[str, ...]:
        target_type, target_id = key
        ids = self._store.ids_for_target(self._tenant_id, target_type, target_id)
        if not ids:
            raise KeyError(key)
        return ids

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, tuple) or len(key) != 2:
            return False
        target_type, target_id = key
        return bool(
            self._store.ids_for_target(
                self._tenant_id, str(target_type), str(target_id)
            )
        )

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return self._store.target_keys(self._tenant_id)

    def __len__(self) -> int:
        return sum(1 for _ in self._store.target_keys(self._tenant_id))


class DomainStorageRepository:
    """Serve imported tenant data without loading evidence quotes."""

    def __init__(
        self,
        pack_dir: str | Path,
        storage_dir: str | Path,
        *,
        default_tenant: str | None = None,
        governance_root: str | Path | None = None,
    ) -> None:
        self.pack_dir = Path(pack_dir).resolve()
        self.storage_dir = Path(storage_dir).resolve()
        governance_root = (
            Path(governance_root).resolve()
            if governance_root is not None
            else self.storage_dir / "governance"
        )
        ontology_path = self.pack_dir / "ontology.yml"
        try:
            if (governance_root / "active.json").is_file():
                governance = OntologyVersionStore(governance_root)
                active = governance.load_active()
                ontology_path = active.path.parent / "ontology.yml"
                raw_ontology = active.document()
                self.ontology = OntologyRegistry.from_file(ontology_path)
                self.governance_root = governance_root
                self.active_governance_version = active.manifest.ontology_version
            else:
                if not ontology_path.is_file():
                    raise DomainRepositoryError(
                        f"ontology profile not found at {ontology_path}"
                    )
                self.ontology = OntologyRegistry.from_file(ontology_path)
                raw_ontology = yaml.safe_load(ontology_path.read_text(encoding="utf-8"))
                self.governance_root = None
                self.active_governance_version = None
        except OntologyGovernanceError as exc:
            raise DomainRepositoryError(
                f"cannot load active governance ontology: {exc}"
            ) from exc
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise DomainRepositoryError(
                f"cannot load ontology profile {ontology_path}: {exc}"
            ) from exc
        if not isinstance(raw_ontology, dict):
            raise DomainRepositoryError(
                f"ontology profile {ontology_path} must contain a mapping"
            )
        self.ontology_document: dict[str, Any] = raw_ontology

        tenant_paths = _tenant_bundle_paths(self.storage_dir)
        if not tenant_paths:
            raise DomainRepositoryError(
                f"no runtime domain bundles found under {self.storage_dir}"
            )
        indexes: dict[str, TenantIndex] = {}
        stores: dict[str, EvidenceStore] = {}
        try:
            for bundle_path in tenant_paths:
                try:
                    bundle = load_domain_bundle(bundle_path)
                except (OSError, ValueError) as exc:
                    raise DomainRepositoryError(
                        f"cannot load runtime bundle {bundle_path}: {exc}"
                    ) from exc
                if bundle.evidence:
                    raise DomainRepositoryError(
                        f"runtime bundle {bundle_path} contains evidence "
                        "quotes; use a bundle written by the importer"
                    )
                if bundle.tenant_id in indexes:
                    raise DomainRepositoryError(
                        f"duplicate runtime bundle for tenant {bundle.tenant_id!r}"
                    )
                db_path = bundle_path.parent / "evidence.sqlite3"
                if not db_path.is_file():
                    raise DomainRepositoryError(
                        f"evidence database not found at {db_path}"
                    )
                store = EvidenceStore(db_path)
                stores[bundle.tenant_id] = store
                self._check_bundle(bundle, store, bundle_path)
                indexes[bundle.tenant_id] = _build_index(
                    bundle,
                    evidence=SqliteEvidenceMapping(store, bundle.tenant_id),
                    evidence_by_target=SqliteTargetMapping(store, bundle.tenant_id),
                )
        except Exception:
            for store in stores.values():
                store.close()
            raise

        self._tenants = indexes
        self._stores = stores
        if default_tenant is not None:
            default_tenant = default_tenant.strip() or None
        if default_tenant is not None and default_tenant not in indexes:
            raise DomainRepositoryError(
                f"default tenant {default_tenant!r} has no runtime bundle"
            )
        if default_tenant is None and len(indexes) == 1:
            default_tenant = next(iter(indexes))
        self.default_tenant = default_tenant

    def refresh_active_ontology(self) -> bool:
        """Refresh the served ontology from the active governance pointer."""

        if self.governance_root is None:
            return False
        governance = OntologyVersionStore(self.governance_root)
        if not governance.active_path.is_file():
            return False
        active = governance.load_active()
        if active.manifest.ontology_version == self.active_governance_version:
            return False
        ontology_path = active.path.parent / "ontology.yml"
        ontology = OntologyRegistry.from_file(ontology_path)
        raw = active.document()
        self.ontology = ontology
        self.ontology_document = raw
        self.active_governance_version = active.manifest.ontology_version
        return True

    def _check_bundle(
        self,
        bundle: DomainBundle,
        store: EvidenceStore,
        bundle_path: Path,
    ) -> None:
        entities = {entity.uid: entity for entity in bundle.entities}
        rules = {rule.rule_uid: rule for rule in bundle.rules}
        referenced = _referenced_evidence_ids(bundle)
        missing = store.missing_ids(bundle.tenant_id, referenced)
        if missing:
            raise DomainRepositoryError(
                f"runtime bundle {bundle_path} references evidence missing "
                f"from SQLite: {', '.join(missing[:5])}"
            )
        if bundle.tree is not None:
            issues = validate_fault_tree_draft(bundle.tree, rules, entities)
            errors = errors_only(issues)
            if errors:
                summary = "; ".join(
                    f"{issue.code} [{issue.target_id or '-'}]: {issue.message}"
                    for issue in errors[:5]
                )
                raise DomainRepositoryError(
                    f"runtime tree {bundle.tree.draft_id!r} failed "
                    f"validation: {summary}",
                    issues=issues,
                )
        if bundle.ontology_version and bundle.ontology_version != self.ontology.version:
            logger.warning(
                "runtime bundle %s uses ontology %s, server has %s",
                bundle.tenant_id,
                bundle.ontology_version,
                self.ontology.version,
            )

    @classmethod
    def from_storage_dir(
        cls,
        pack_dir: str | Path,
        storage_dir: str | Path,
        *,
        default_tenant: str | None = None,
        governance_root: str | Path | None = None,
    ) -> "DomainStorageRepository":
        return cls(
            pack_dir,
            storage_dir,
            default_tenant=default_tenant,
            governance_root=governance_root,
        )

    @property
    def tenants(self) -> tuple[str, ...]:
        return tuple(sorted(self._tenants))

    @property
    def quotes_loaded(self) -> int:
        return sum(store.quotes_loaded for store in self._stores.values())

    def tenant_index(self, tenant_id: str | None) -> TenantIndex:
        effective = (tenant_id or "").strip() or self.default_tenant
        if not effective:
            raise DomainError(
                "invalid_argument",
                "tenant_id is required when more than one tenant is loaded",
                {"parameter": "tenant_id"},
            )
        index = self._tenants.get(effective)
        if index is None:
            raise DomainError(
                NOT_FOUND,
                f"tenant {effective!r} is not available",
                {"tenant_id": effective},
            )
        return index

    def close(self) -> None:
        for store in self._stores.values():
            store.close()


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _tenant_bundle_paths(storage_dir: Path) -> list[Path]:
    """Resolve one runtime bundle per tenant.

    A versioned layout is read through ``tenants/<tenant>/active.json``; the
    pre-versioning ``tenants/<tenant>/bundle.json`` remains supported for
    callers that still build it by hand (the memory benchmark does).  An
    active pointer that cannot be resolved is a hard error: silently falling
    back to an older run would serve stale facts without saying so.
    """

    tenants_dir = storage_dir / "tenants"
    if not tenants_dir.is_dir():
        direct = storage_dir / "bundle.json"
        return [direct] if direct.is_file() else []

    paths: list[Path] = []
    for tenant_dir in sorted(
        entry for entry in tenants_dir.iterdir() if entry.is_dir()
    ):
        active_path = tenant_dir / "active.json"
        if active_path.is_file():
            paths.append(_active_bundle_path(tenant_dir, active_path))
            continue
        legacy = tenant_dir / "bundle.json"
        if legacy.is_file():
            paths.append(legacy)
            continue
        if (tenant_dir / "runs").is_dir():
            raise DomainRepositoryError(
                f"tenant {tenant_dir.name!r} has import runs but no "
                f"active.json; re-run the importer"
            )
    return paths


def _active_bundle_path(tenant_dir: Path, active_path: Path) -> Path:
    try:
        payload = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainRepositoryError(
            f"cannot read active pointer {active_path}: {exc}"
        ) from exc
    run_id = payload.get("run_id") if isinstance(payload, dict) else None
    if (
        not isinstance(run_id, str)
        or not _RUN_ID_RE.fullmatch(run_id)
        or run_id in {".", ".."}
    ):
        raise DomainRepositoryError(
            f"active pointer {active_path} has invalid run_id {run_id!r}"
        )
    bundle_path = tenant_dir / "runs" / run_id / "bundle.json"
    if not bundle_path.is_file():
        raise DomainRepositoryError(
            f"active run {run_id!r} for tenant {tenant_dir.name!r} has no "
            f"runtime bundle at {bundle_path}"
        )
    return bundle_path


def _referenced_evidence_ids(bundle: DomainBundle) -> set[str]:
    result: set[str] = set()
    for entity in bundle.entities:
        result.update(entity.evidence_ids)
    for fact in bundle.facts:
        result.update(fact.evidence_ids)
    for rule in bundle.rules:
        result.update(rule.evidence_ids)
    if bundle.trace is not None:
        result.update(bundle.trace.evidence_ids)
        for step in bundle.trace.steps:
            result.update(step.evidence_ids)
    return result


__all__ = [
    "DomainStorageRepository",
    "SqliteEvidenceMapping",
    "SqliteTargetMapping",
]
