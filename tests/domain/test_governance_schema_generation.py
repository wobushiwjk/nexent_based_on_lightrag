"""Tests for versioned Schema projection during ontology evolution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lightrag.fault_tree.governance.evolution import apply_change_to_ontology
from lightrag.fault_tree.governance.models import (
    OntologyChangeOperation,
    OntologyChangeSet,
)
from lightrag.fault_tree.governance.schema_generation import (
    build_versioned_schema_artifacts,
)

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "domain_packs" / "industrial_fault"


def test_versioned_schema_extends_entity_and_bundle_unions() -> None:
    active = yaml.safe_load((PACK / "ontology.yml").read_text(encoding="utf-8"))
    change = OntologyChangeSet(
        change_id="CHG-SCHEMA-1",
        base_version="industrial-fault-v0.1",
        proposed_version="industrial-fault-v0.2",
        author="engineer",
        rationale="Condition-monitoring sensor coverage.",
        created_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
        operations=[
            OntologyChangeOperation(
                kind="class",
                action="add",
                code="Sensor",
                after={
                    "label": "传感器",
                    "parent": "Asset",
                    "prompt_visible": True,
                    "schema_ref": "entity.schema.json#/$defs/Sensor",
                    "required": ["uid", "name", "sensor_tag"],
                    "extract_properties": {
                        "name": {"type": "string"},
                        "sensor_tag": {"type": "string"},
                        "measured_variable": {"type": "string"},
                    },
                    "metadata_properties": {
                        "lifecycle_status": {
                            "type": "enum",
                            "values": ["in_service", "stopped", "retired"],
                        }
                    },
                },
            ),
            OntologyChangeOperation(
                kind="class",
                action="add",
                code="RotatingEquipment",
                after={
                    "label": "旋转设备",
                    "parent": "Equipment",
                    "prompt_visible": True,
                    "schema_ref": (
                        "schemas/entity.schema.json#/$defs/RotatingEquipment"
                    ),
                },
            ),
            OntologyChangeOperation(
                kind="relation",
                action="add",
                code="INSTALLED_ON",
                after={
                    "source_types": ["Sensor"],
                    "target_types": ["RotatingEquipment"],
                    "prompt_visible": True,
                    "evidence_required": True,
                },
            ),
        ],
    )
    document, _, schema_version = apply_change_to_ontology(
        change,
        active_document=active,
    )
    artifacts = build_versioned_schema_artifacts(
        base_schemas={
            path.name: path.read_bytes() for path in (PACK / "schemas").glob("*.json")
        },
        ontology_document=document,
        schema_version=schema_version,
    )

    entity = json.loads(artifacts["entity.schema.json"])
    bundle = json.loads(artifacts["domain_bundle.schema.json"])
    sensor = entity["$defs"]["Sensor"]
    rotating = entity["$defs"]["RotatingEquipment"]
    mapping = entity["discriminator"]["mapping"]

    assert sensor["properties"]["canonical_name"]["type"] == "string"
    assert sensor["properties"]["sensor_tag"]["type"] == "string"
    assert sensor["properties"]["lifecycle_status"]["enum"] == [
        "in_service",
        "stopped",
        "retired",
    ]
    assert sensor["required"] == ["canonical_name", "sensor_tag", "uid"]
    assert rotating["properties"]["class_code"]["const"] == "RotatingEquipment"
    assert "equipment_tag" in rotating["properties"]
    assert mapping["Sensor"] == "#/$defs/Sensor"
    assert mapping["RotatingEquipment"] == "#/$defs/RotatingEquipment"
    assert (
        bundle["properties"]["entities"]["items"]["discriminator"]["mapping"]["Sensor"]
        == "#/$defs/Sensor"
    )
    assert entity["x-ontology-version"] == "industrial-fault-v0.2"
    assert entity["x-schema-version"] == "industrial-fault-schema-v0.2"
    assert "manifest.json" in artifacts
