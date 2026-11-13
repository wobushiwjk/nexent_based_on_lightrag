"""Bootstrap the canonical ontology into an empty governance store."""

from __future__ import annotations

from pathlib import Path

from lightrag.fault_tree.governance import (
    OntologyVersionManifest,
    OntologyVersionStore,
)


def bootstrap_ontology_governance(
    *,
    governance_root: str | Path,
    ontology_path: str | Path,
    actor: str = "system:bootstrap",
) -> OntologyVersionManifest | None:
    """Install and activate the current ontology when governance is empty.

    Existing stores are never migrated or reactivated implicitly. If a store
    contains versions but has no active pointer, startup remains fail closed
    and an operator must recover it explicitly.
    """

    store = OntologyVersionStore(governance_root)
    if store.active_path.is_file():
        store.read_active()
        return None
    if store.list_versions():
        raise RuntimeError(
            "governance store has installed ontology versions but no active "
            "pointer; recover the active pointer explicitly"
        )
    return store.install(
        ontology_path,
        actor=actor,
        notes="Bootstrap the canonical domain ontology.",
        activate=True,
    )


__all__ = ["bootstrap_ontology_governance"]
