"""Generate immutable JSON Schema artifacts for an evolved ontology version."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping


class OntologySchemaGenerationError(RuntimeError):
    """The active schema and the proposed ontology cannot be projected safely."""


def build_versioned_schema_artifacts(
    *,
    base_schemas: Mapping[str, bytes | Mapping[str, Any]],
    ontology_document: Mapping[str, Any],
    schema_version: str,
) -> dict[str, bytes]:
    """Return a complete, deterministic schema directory for one version.

    The entity union is extended for concrete classes introduced by the
    ontology. A new class inherits its parent's entity projection when one
    exists; otherwise it receives the common entity envelope. Other domain
    contracts are copied byte-for-byte at the JSON-document level and stamped
    with the new ontology/schema versions.
    """

    documents: dict[str, dict[str, Any]] = {}
    for name, value in base_schemas.items():
        if not name.endswith(".json"):
            continue
        if isinstance(value, bytes):
            try:
                parsed = json.loads(value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise OntologySchemaGenerationError(
                    f"schema artifact {name!r} is not valid JSON"
                ) from exc
        else:
            parsed = copy.deepcopy(dict(value))
        if not isinstance(parsed, dict):
            raise OntologySchemaGenerationError(
                f"schema artifact {name!r} must contain a JSON object"
            )
        documents[name] = parsed

    entity = documents.get("entity.schema.json")
    if entity is None:
        raise OntologySchemaGenerationError(
            "base schema set must include entity.schema.json"
        )
    ontology_version = str(ontology_document.get("ontology_version") or "").strip()
    if not ontology_version:
        raise OntologySchemaGenerationError(
            "ontology document must define ontology_version"
        )

    classes = ontology_document.get("classes")
    if not isinstance(classes, Mapping) or not classes:
        raise OntologySchemaGenerationError("ontology document must define classes")

    definitions = entity.setdefault("$defs", {})
    if not isinstance(definitions, dict):
        raise OntologySchemaGenerationError("entity schema $defs must be an object")
    common_properties = _common_entity_properties(definitions)
    entity_mapping: dict[str, str] = {}

    for code, raw_definition in classes.items():
        if not isinstance(raw_definition, Mapping):
            continue
        definition = dict(raw_definition)
        if bool(definition.get("abstract", False)):
            continue
        schema_ref = str(definition.get("schema_ref") or "").strip()
        definition_name = _entity_definition_name(code, schema_ref)
        if definition_name is None:
            continue
        if definition_name not in definitions:
            definitions[definition_name] = _new_entity_definition(
                code=code,
                definition=definition,
                classes=classes,
                definitions=definitions,
                common_properties=common_properties,
            )
        entity_mapping[str(code)] = f"#/$defs/{definition_name}"

    union = {
        "discriminator": {
            "mapping": dict(sorted(entity_mapping.items())),
            "propertyName": "class_code",
        },
        "oneOf": [{"$ref": ref} for ref in sorted(set(entity_mapping.values()))],
    }
    entity.update(union)

    bundle = documents.get("domain_bundle.schema.json")
    if bundle is not None:
        try:
            bundle["properties"]["entities"]["items"] = copy.deepcopy(union)
        except (KeyError, TypeError) as exc:
            raise OntologySchemaGenerationError(
                "domain_bundle schema does not expose properties.entities.items"
            ) from exc

    for document in documents.values():
        document["x-ontology-version"] = ontology_version
        document["x-schema-version"] = schema_version

    manifest_files: list[dict[str, str]] = []
    for name in sorted(documents):
        if name == "manifest.json":
            continue
        raw = _json_bytes(documents[name])
        manifest_files.append(
            {
                "name": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "title": str(documents[name].get("title") or name),
            }
        )
    documents["manifest.json"] = {
        "files": manifest_files,
        "generated_by": "ontology-governance publication",
        "ontology_version": ontology_version,
        "schema_version": schema_version,
    }
    return {name: _json_bytes(document) for name, document in sorted(documents.items())}


def _new_entity_definition(
    *,
    code: str,
    definition: Mapping[str, Any],
    classes: Mapping[str, Any],
    definitions: Mapping[str, Any],
    common_properties: Mapping[str, Any],
) -> dict[str, Any]:
    parent = str(definition.get("parent") or "").strip()
    parent_ref = _entity_definition_name(parent, _schema_ref(classes.get(parent)))
    parent_schema = definitions.get(parent_ref) if parent_ref else None
    if isinstance(parent_schema, Mapping):
        result = copy.deepcopy(dict(parent_schema))
    else:
        result = {
            "additionalProperties": False,
            "properties": copy.deepcopy(dict(common_properties)),
            "type": "object",
        }

    properties = result.setdefault("properties", {})
    if not isinstance(properties, dict):
        raise OntologySchemaGenerationError(
            f"parent schema properties for {code!r} must be an object"
        )
    properties["class_code"] = {
        "const": code,
        "default": code,
        "title": "Class Code",
        "type": "string",
    }
    declared: dict[str, Any] = {}
    for group in ("extract_properties", "metadata_properties"):
        value = definition.get(group)
        if isinstance(value, Mapping):
            declared.update(
                (str(name), dict(spec))
                for name, spec in value.items()
                if isinstance(spec, Mapping)
            )
    for name, spec in declared.items():
        target_name = "canonical_name" if name == "name" else name
        properties[target_name] = _property_schema(spec)

    required = {
        _required_name(str(name))
        for name in definition.get("required") or []
        if str(name).strip()
    }
    required.update(
        str(name) for name in result.get("required") or [] if str(name) in properties
    )
    result["required"] = sorted(required)
    result["title"] = str(definition.get("label") or code)
    result["description"] = str(
        definition.get("description") or f"Ontology entity class {code}."
    )
    result["x-ontology-class"] = code
    if parent:
        result["x-ontology-parent"] = parent
    return result


def _common_entity_properties(
    definitions: Mapping[str, Any],
) -> dict[str, Any]:
    property_sets = [
        set(value.get("properties", {}))
        for value in definitions.values()
        if isinstance(value, Mapping) and isinstance(value.get("properties"), Mapping)
    ]
    if not property_sets:
        raise OntologySchemaGenerationError(
            "entity schema contains no reusable entity definitions"
        )
    common = set.intersection(*property_sets)
    source = next(
        value
        for value in definitions.values()
        if isinstance(value, Mapping) and isinstance(value.get("properties"), Mapping)
    )
    return {name: copy.deepcopy(source["properties"][name]) for name in sorted(common)}


def _property_schema(spec: Mapping[str, Any]) -> dict[str, Any]:
    property_type = str(spec.get("type") or "string")
    if property_type == "string_list":
        result: dict[str, Any] = {"items": {"type": "string"}, "type": "array"}
    elif property_type == "enum":
        result = {"type": "string"}
        values = [str(item) for item in spec.get("values") or []]
        if values:
            result["enum"] = values
    elif property_type in {"integer", "number", "boolean"}:
        result = {"type": property_type}
    elif property_type == "array":
        result = {"items": {}, "type": "array"}
    elif property_type in {"object", "expression"}:
        result = {"additionalProperties": True, "type": "object"}
    elif property_type == "date":
        result = {"format": "date", "type": "string"}
    elif property_type == "datetime":
        result = {"format": "date-time", "type": "string"}
    else:
        result = {"type": "string"}
    minimum = spec.get("minimum")
    if isinstance(minimum, (int, float)):
        result["minimum"] = minimum
    min_items = spec.get("min_items")
    if isinstance(min_items, int):
        result["minItems"] = min_items
    if spec.get("unique_within_tenant") is True:
        result["x-unique-within-tenant"] = True
    return result


def _required_name(name: str) -> str:
    return "canonical_name" if name == "name" else name


def _schema_ref(value: Any) -> str:
    return str(value.get("schema_ref") or "") if isinstance(value, Mapping) else ""


def _entity_definition_name(code: str, schema_ref: str) -> str | None:
    marker = "#/$defs/"
    if marker in schema_ref:
        path = schema_ref.split(marker, 1)[0]
        if path not in {"entity.schema.json", "schemas/entity.schema.json"}:
            return None
        return schema_ref.split(marker, 1)[1] or None
    if code in {"Asset", "KnowledgeEntity"}:
        return None
    return code or None


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


__all__ = [
    "OntologySchemaGenerationError",
    "build_versioned_schema_artifacts",
]
