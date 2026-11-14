"""Apply reviewed ontology changes and migrate tenant snapshots safely.

Publication prepares complete tenant runs first and records the old/new run
map before switching pointers. The final switch is still cross-store: the
ontology pointer is changed before each tenant pointer. A crash can therefore
leave the new ontology active while one or more tenants still point at their
old complete run. This residue is recoverable by retrying the same publication
record; no snapshot is deleted, and readers never observe a partially written
snapshot.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from lightrag.fault_tree.contracts import DomainBundle
from lightrag.fault_tree.governance.models import (
    OntologyChangeSet,
    TenantMigrationRecord,
)
from lightrag.fault_tree.governance.store import OntologyGovernanceError
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.file_atomic import atomic_write


class OntologyEvolutionError(OntologyGovernanceError):
    """A reviewed change cannot be applied or migrated safely."""


def apply_change_to_ontology(
    change: OntologyChangeSet,
    *,
    active_document: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, str]:
    """Apply one approved change set to a copy of the active ontology."""

    document = copy.deepcopy(dict(active_document))
    classes = document.setdefault("classes", {})
    relations = document.setdefault("relations", {})
    constraints = document.setdefault("constraints", [])
    if not isinstance(classes, dict):
        raise OntologyEvolutionError("active ontology classes must be a mapping")
    if not isinstance(relations, dict):
        raise OntologyEvolutionError("active ontology relations must be a mapping")
    if not isinstance(constraints, list):
        raise OntologyEvolutionError("active ontology constraints must be a list")

    for operation in change.operations:
        if operation.kind == "class":
            _apply_class(classes, operation.action, operation.code, operation.after)
        elif operation.kind == "relation":
            _apply_relation(
                relations,
                operation.action,
                operation.code,
                operation.after,
            )
        elif operation.kind == "constraint":
            _apply_constraint(
                constraints,
                operation.action,
                operation.code,
                operation.after,
            )
        elif operation.kind == "gap":
            _apply_gap(
                document,
                operation.action,
                operation.code,
                operation.after,
            )

    schema_version = derive_schema_version(
        change.proposed_version,
        str(active_document.get("schema_version") or ""),
    )
    document["ontology_version"] = change.proposed_version
    document["schema_version"] = schema_version
    content = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    ).encode("utf-8")
    _validate_ontology_bytes(content)
    return document, content, schema_version


def derive_schema_version(proposed_version: str, active_schema: str) -> str:
    """Derive a schema version from the ontology version naming convention."""

    if "industrial-fault-" in proposed_version:
        return proposed_version.replace(
            "industrial-fault-",
            "industrial-fault-schema-",
            1,
        )
    if active_schema:
        suffix = active_schema.rsplit("-", 1)[-1]
        if suffix and suffix in proposed_version:
            return active_schema.replace(suffix, proposed_version.rsplit("-", 1)[-1])
    return f"{proposed_version}-schema"


def prepare_tenant_migrations(
    domain_storage_root: str | Path,
    *,
    change: OntologyChangeSet,
    schema_version: str,
) -> list[TenantMigrationRecord]:
    """Build new complete tenant runs without switching active pointers."""

    storage = Path(domain_storage_root).resolve()
    records: list[TenantMigrationRecord] = []
    for active_path in sorted((storage / "tenants").glob("*/active.json")):
        tenant_dir = active_path.parent
        try:
            pointer = json.loads(active_path.read_text(encoding="utf-8"))
            previous_run_id = str(pointer.get("run_id") or "")
        except (OSError, json.JSONDecodeError) as exc:
            raise OntologyEvolutionError(
                f"cannot read tenant active pointer {active_path}: {exc}"
            ) from exc
        if not previous_run_id or Path(previous_run_id).name != previous_run_id:
            raise OntologyEvolutionError(
                f"tenant {tenant_dir.name!r} has an invalid active run"
            )
        source_run = tenant_dir / "runs" / previous_run_id
        if not source_run.is_dir():
            raise OntologyEvolutionError(
                f"tenant {tenant_dir.name!r} active run is missing: {source_run}"
            )
        new_run_id = _prepare_tenant_run(
            tenant_dir,
            source_run,
            tenant_id=tenant_dir.name,
            change=change,
            schema_version=schema_version,
        )
        records.append(
            TenantMigrationRecord(
                tenant_id=tenant_dir.name,
                previous_run_id=previous_run_id,
                new_run_id=new_run_id,
            )
        )
    return records


def activate_tenant_migrations(
    domain_storage_root: str | Path,
    records: list[TenantMigrationRecord],
    *,
    ontology_version: str,
    schema_version: str,
) -> None:
    """Atomically switch every tenant to its prepared migrated run."""

    storage = Path(domain_storage_root).resolve()
    for record in records:
        tenant_dir = storage / "tenants" / record.tenant_id
        run_dir = tenant_dir / "runs" / record.new_run_id
        manifest = _read_json(run_dir / "manifest.json")
        pointer = {
            "tenant_id": record.tenant_id,
            "run_id": record.new_run_id,
            "bundle_digest": manifest["bundle_digest"],
            "bundle_version": manifest["bundle_version"],
            "ontology_version": ontology_version,
            "schema_version": schema_version,
            "runtime_bundle": f"runs/{record.new_run_id}/bundle.json",
            "manifest": f"runs/{record.new_run_id}/manifest.json",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_atomic(tenant_dir / "active.json", pointer)


def rollback_tenant_migrations(
    domain_storage_root: str | Path,
    records: list[TenantMigrationRecord],
    *,
    ontology_version: str,
    schema_version: str,
) -> None:
    """Switch tenants back to the exact runs captured before publication."""

    storage = Path(domain_storage_root).resolve()
    for record in records:
        tenant_dir = storage / "tenants" / record.tenant_id
        run_dir = tenant_dir / "runs" / record.previous_run_id
        manifest = _read_json(run_dir / "manifest.json")
        pointer = {
            "tenant_id": record.tenant_id,
            "run_id": record.previous_run_id,
            "bundle_digest": manifest["bundle_digest"],
            "bundle_version": manifest["bundle_version"],
            "ontology_version": ontology_version,
            "schema_version": schema_version,
            "runtime_bundle": f"runs/{record.previous_run_id}/bundle.json",
            "manifest": f"runs/{record.previous_run_id}/manifest.json",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_atomic(tenant_dir / "active.json", pointer)


def _prepare_tenant_run(
    tenant_dir: Path,
    source_run: Path,
    *,
    tenant_id: str,
    change: OntologyChangeSet,
    schema_version: str,
) -> str:
    full_path = source_run / "bundle.full.json"
    try:
        bundle = DomainBundle.model_validate(_read_json(full_path))
    except Exception as exc:
        raise OntologyEvolutionError(
            f"cannot read tenant bundle {full_path}: {exc}"
        ) from exc
    migrated = _migrate_bundle(bundle, change=change, schema_version=schema_version)
    payload = migrated.model_dump(mode="json")
    digest = hashlib.sha256(_json_bytes(payload)).hexdigest()
    run_id = f"run-{digest[:12]}"
    runs_dir = tenant_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / run_id
    if target.is_dir():
        return run_id

    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=runs_dir))
    try:
        shutil.copytree(source_run, staging, dirs_exist_ok=True)
        _write_json(staging / "bundle.full.json", payload)
        runtime = migrated.model_copy(update={"evidence": []})
        _write_json(staging / "bundle.json", runtime.model_dump(mode="json"))
        manifest = _read_json(staging / "manifest.json")
        manifest.update(
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "bundle_digest": digest,
                "bundle_version": migrated.bundle_version,
                "ontology_version": migrated.ontology_version,
                "schema_version": migrated.schema_version,
                "migrated_by_change": change.change_id,
                "run_dir": str(target),
                "evidence_db": str(target / "evidence.sqlite3"),
                "full_bundle": str(target / "bundle.full.json"),
                "runtime_bundle": str(target / "bundle.json"),
            }
        )
        _write_json(staging / "manifest.json", manifest)
        try:
            os.rename(staging, target)
        except FileExistsError:
            pass
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return run_id


def _migrate_bundle(
    bundle: DomainBundle,
    *,
    change: OntologyChangeSet,
    schema_version: str,
) -> DomainBundle:
    version = change.proposed_version
    entities = [
        entity.model_copy(
            update={
                "ontology_version": version,
                "schema_version": schema_version,
            }
        )
        for entity in bundle.entities
    ]
    facts = [
        fact.model_copy(update={"ontology_version": version}) for fact in bundle.facts
    ]
    rules = [
        rule.model_copy(update={"ontology_version": version}) for rule in bundle.rules
    ]
    tree = (
        bundle.tree.model_copy(update={"ontology_version": version})
        if bundle.tree is not None
        else None
    )
    trace = (
        bundle.trace.model_copy(update={"ontology_version": version})
        if bundle.trace is not None
        else None
    )
    return bundle.model_copy(
        update={
            "bundle_version": f"{bundle.bundle_version}+{change.change_id}",
            "ontology_version": version,
            "schema_version": schema_version,
            "entities": entities,
            "facts": facts,
            "rules": rules,
            "tree": tree,
            "trace": trace,
        }
    )


def _apply_class(
    classes: dict[str, Any],
    action: str,
    code: str,
    after: Mapping[str, Any],
) -> None:
    if action == "add":
        definition = dict(after)
        definition.setdefault("label", code)
        parent = definition.get("parent")
        if (
            not definition.get("schema_ref")
            and isinstance(parent, str)
            and _inherits_from(classes, parent, "Asset")
        ):
            definition["schema_ref"] = f"schemas/entity.schema.json#/$defs/{code}"
        classes[code] = definition
    elif action == "modify":
        current = classes.get(code)
        current = current if isinstance(current, dict) else {}
        classes[code] = {**current, **dict(after)}
    elif action == "deprecate":
        current = classes.get(code)
        current = current if isinstance(current, dict) else {}
        classes[code] = {**current, **dict(after), "deprecated": True}
    elif action == "remove":
        classes.pop(code, None)


def _inherits_from(
    classes: Mapping[str, Any],
    parent: str,
    ancestor: str,
) -> bool:
    current: str | None = parent
    seen: set[str] = set()
    while current is not None and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        definition = classes.get(current)
        if not isinstance(definition, Mapping):
            return False
        inherited = definition.get("parent")
        current = str(inherited) if isinstance(inherited, str) else None
    return False


def _apply_relation(
    relations: dict[str, Any],
    action: str,
    code: str,
    after: Mapping[str, Any],
) -> None:
    if action == "add":
        definition = _relation_definition(after)
        relations[code] = definition
    elif action == "modify":
        current = relations.get(code)
        current = current if isinstance(current, dict) else {}
        relations[code] = {**current, **_relation_definition(after, partial=True)}
    elif action == "deprecate":
        current = relations.get(code)
        current = current if isinstance(current, dict) else {}
        relations[code] = {**current, **dict(after), "deprecated": True}
    elif action == "remove":
        relations.pop(code, None)


def _apply_constraint(
    constraints: list[Any],
    action: str,
    code: str,
    after: Mapping[str, Any],
) -> None:
    existing = next(
        (
            index
            for index, item in enumerate(constraints)
            if isinstance(item, dict) and item.get("id") == code
        ),
        None,
    )
    if action == "add":
        constraints.append({"id": code, **dict(after)})
    elif action == "modify" and existing is not None:
        constraints[existing] = {**constraints[existing], **dict(after)}
    elif action == "deprecate" and existing is not None:
        constraints[existing] = {
            **constraints[existing],
            **dict(after),
            "deprecated": True,
        }
    elif action == "remove" and existing is not None:
        constraints.pop(existing)


def _apply_gap(
    document: dict[str, Any],
    action: str,
    code: str,
    after: Mapping[str, Any],
) -> None:
    gaps = document.setdefault("governance_gaps", [])
    if not isinstance(gaps, list):
        raise OntologyEvolutionError("governance_gaps must be a list")
    existing = next(
        (
            index
            for index, item in enumerate(gaps)
            if isinstance(item, dict) and item.get("id") == code
        ),
        None,
    )
    if action == "add":
        gaps.append({"id": code, **dict(after)})
    elif action == "modify" and existing is not None:
        gaps[existing] = {**gaps[existing], **dict(after)}
    elif action == "deprecate" and existing is not None:
        gaps[existing] = {
            **gaps[existing],
            **dict(after),
            "deprecated": True,
        }
    elif action == "remove" and existing is not None:
        gaps.pop(existing)


def _relation_definition(
    after: Mapping[str, Any],
    *,
    partial: bool = False,
) -> dict[str, Any]:
    result = dict(after)
    if "source_types" in result:
        result["source"] = result.pop("source_types")
    if "target_types" in result:
        result["target"] = result.pop("target_types")
    if not partial:
        result.setdefault("directed", True)
        result.setdefault("evidence_required", True)
    return result


def _validate_ontology_bytes(content: bytes) -> None:
    staging = Path(tempfile.mkdtemp(prefix="ontology-version-"))
    try:
        path = staging / "ontology.yml"
        path.write_bytes(content)
        OntologyRegistry.from_file(path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OntologyEvolutionError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OntologyEvolutionError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    data = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        str(path),
        lambda temporary: Path(temporary).write_bytes(data),
        workspace="ontology-governance",
    )


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "OntologyEvolutionError",
    "activate_tenant_migrations",
    "apply_change_to_ontology",
    "derive_schema_version",
    "prepare_tenant_migrations",
    "rollback_tenant_migrations",
]
