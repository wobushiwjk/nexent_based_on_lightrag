"""Export JSON Schemas for the canonical industrial fault-tree contracts.

The Pydantic models are the executable contract. This script writes their JSON
Schema projection into the domain pack so non-Python services (Nexent tools,
MCP clients, review UIs) can validate the same payloads without importing
LightRAG.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.contracts import (  # noqa: E402
    ONTOLOGY_VERSION,
    SCHEMA_VERSION,
    DecisionTrace,
    DomainBundle,
    EntityRecord,
    Evidence,
    Fact,
    FaultCauseRule,
    FaultTreeDraft,
)

DEFAULT_OUTPUT = ROOT / "domain_packs" / "industrial_fault" / "schemas"
SCHEMA_BASE = "https://pump-copilot.local/schemas/industrial-fault"

SCHEMA_TARGETS: dict[str, BaseModel | TypeAdapter[Any]] = {
    "entity": TypeAdapter(EntityRecord),
    "evidence": Evidence,
    "fact": Fact,
    "fault_rule": FaultCauseRule,
    "fault_tree_draft": FaultTreeDraft,
    "decision_trace": DecisionTrace,
    "domain_bundle": DomainBundle,
}


def build_schemas() -> dict[str, dict[str, Any]]:
    """Return the JSON Schema document for each named contract."""

    schemas: dict[str, dict[str, Any]] = {}
    for name, target in SCHEMA_TARGETS.items():
        if isinstance(target, TypeAdapter):
            schema = target.json_schema()
        else:
            schema = target.model_json_schema()
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
        schema.setdefault("$id", f"{SCHEMA_BASE}/{name}.schema.json")
        schema.setdefault("title", name.replace("_", " ").title())
        schema["x-ontology-version"] = ONTOLOGY_VERSION
        schema["x-schema-version"] = SCHEMA_VERSION
        schemas[name] = schema
    return schemas


def write_schemas(output_dir: Path = DEFAULT_OUTPUT) -> list[Path]:
    """Write all schemas plus a manifest and return the written paths."""

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    manifest_files: list[dict[str, str]] = []
    for name, schema in build_schemas().items():
        path = output_dir / f"{name}.schema.json"
        text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)
        path.write_text(text + "\n", encoding="utf-8")
        written.append(path)
        manifest_files.append(
            {
                "name": path.name,
                "title": str(schema.get("title", name)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "ontology_version": ONTOLOGY_VERSION,
        "generated_by": "scripts/export_domain_schemas.py",
        "files": manifest_files,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written.append(manifest_path)
    return written


if __name__ == "__main__":
    for written_path in write_schemas():
        print(written_path.relative_to(ROOT))
