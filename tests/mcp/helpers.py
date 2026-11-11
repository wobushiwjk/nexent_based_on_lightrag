"""Shared fixtures and helpers for the read-only MCP service tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from lightrag.fault_tree.contracts import DomainBundle, FaultEvent

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "domain_packs" / "industrial_fault"
DEMO_BUNDLE = PACK_DIR / "seeds" / "demo_bundle.json"
BAD_BUNDLE = PACK_DIR / "seeds" / "bad_bundle_sample.json"


def retag_bundle(
    bundle: DomainBundle,
    tenant_id: str,
    *,
    rename_fault_uid: str | None = None,
    canonical_name: str | None = None,
) -> DomainBundle:
    """Return a deep copy that belongs to another tenant.

    This is deliberately only a tenant/name change: every cross-reference,
    evidence hash, and file check still validates, so the copy exercises tenant
    isolation rather than a broken bundle.
    """

    copy = bundle.model_copy(deep=True)
    copy.tenant_id = tenant_id
    for entity in copy.entities:
        entity.tenant_id = tenant_id
    for evidence in copy.evidence:
        evidence.tenant_id = tenant_id
    for fact in copy.facts:
        fact.tenant_id = tenant_id
    for rule in copy.rules:
        rule.tenant_id = tenant_id
    if copy.tree is not None:
        copy.tree.tenant_id = tenant_id
    if copy.trace is not None:
        copy.trace.tenant_id = tenant_id
    if rename_fault_uid and canonical_name:
        fault = next(
            entity
            for entity in copy.entities
            if isinstance(entity, FaultEvent) and entity.uid == rename_fault_uid
        )
        fault.canonical_name = canonical_name
        fault.fault_name = canonical_name
    return copy


def directory_digest(root: Path) -> dict[str, str]:
    """Hash every file below ``root`` so a test can prove it was untouched."""

    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result
