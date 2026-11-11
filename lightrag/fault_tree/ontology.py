"""Load and validate the fault-tree ontology profile."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


@dataclass(frozen=True)
class ClassDefinition:
    code: str
    label: str
    parent: str | None
    prompt_visible: bool
    required_fields: tuple[str, ...]
    extract_properties: dict[str, Any]


@dataclass(frozen=True)
class RelationDefinition:
    code: str
    source_types: frozenset[str]
    target_types: frozenset[str]
    directed: bool
    prompt_visible: bool
    system_generated: bool
    evidence_required: bool


class OntologyRegistry:
    """Validated view over an ontology/profile YAML file."""

    def __init__(
        self,
        *,
        version: str,
        classes: dict[str, ClassDefinition],
        relations: dict[str, RelationDefinition],
        schema_version: str | None = None,
    ) -> None:
        self.version = version
        self.schema_version = schema_version
        self.classes = classes
        self.relations = relations
        self._class_aliases = {code.casefold(): code for code in classes}

    @classmethod
    def from_file(cls, path: str | Path) -> "OntologyRegistry":
        source = Path(path)
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"ontology profile {source} must contain a mapping")

        version = str(raw.get("ontology_version") or "").strip()
        if not version:
            raise ValueError(f"ontology profile {source} must define ontology_version")

        raw_classes = raw.get("classes")
        raw_relations = raw.get("relations")
        if not isinstance(raw_classes, dict) or not raw_classes:
            raise ValueError(f"ontology profile {source} must define classes")
        if not isinstance(raw_relations, dict) or not raw_relations:
            raise ValueError(f"ontology profile {source} must define relations")

        classes: dict[str, ClassDefinition] = {}
        for code, definition in raw_classes.items():
            if not isinstance(definition, dict):
                raise ValueError(f"class {code!r} must contain a mapping")
            classes[str(code)] = ClassDefinition(
                code=str(code),
                label=str(definition.get("label") or code),
                parent=_optional_string(definition.get("parent")),
                prompt_visible=bool(definition.get("prompt_visible", False)),
                required_fields=tuple(
                    str(item) for item in definition.get("required", [])
                ),
                extract_properties=dict(definition.get("extract_properties") or {}),
            )
        _validate_class_parents(classes)

        relations: dict[str, RelationDefinition] = {}
        for code, definition in raw_relations.items():
            if not isinstance(definition, dict):
                raise ValueError(f"relation {code!r} must contain a mapping")
            source_types = _as_type_set(definition.get("source"))
            target_types = _as_type_set(definition.get("target"))
            unknown = (source_types | target_types) - set(classes)
            if unknown:
                raise ValueError(
                    f"relation {code!r} references unknown classes: "
                    f"{', '.join(sorted(unknown))}"
                )
            relations[str(code)] = RelationDefinition(
                code=str(code),
                source_types=frozenset(source_types),
                target_types=frozenset(target_types),
                directed=bool(definition.get("directed", True)),
                prompt_visible=bool(definition.get("prompt_visible", False)),
                system_generated=bool(definition.get("system_generated", False)),
                evidence_required=bool(definition.get("evidence_required", False)),
            )

        schema_version = str(raw.get("schema_version") or "").strip() or None
        return cls(
            version=version,
            schema_version=schema_version,
            classes=classes,
            relations=relations,
        )

    @staticmethod
    def has_ontology_definition(path: str | Path) -> bool:
        """Return whether a prompt profile declares domain classes and relations."""

        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return (
            isinstance(raw, dict)
            and isinstance(raw.get("classes"), dict)
            and bool(raw.get("classes"))
            and isinstance(raw.get("relations"), dict)
            and bool(raw.get("relations"))
        )

    def canonical_class(self, value: str) -> str | None:
        return self._class_aliases.get(value.casefold())

    def prompt_visible_classes(self) -> dict[str, ClassDefinition]:
        return {
            code: definition
            for code, definition in self.classes.items()
            if definition.prompt_visible
        }

    def prompt_visible_relations(self) -> dict[str, RelationDefinition]:
        return {
            code: definition
            for code, definition in self.relations.items()
            if definition.prompt_visible
        }

    def validate_relation(
        self,
        relation_code: str,
        source_type: str,
        target_type: str,
    ) -> None:
        definition = self.relations.get(relation_code)
        if definition is None:
            raise ValueError(f"unknown relation_code {relation_code!r}")
        canonical_source = self.canonical_class(source_type)
        canonical_target = self.canonical_class(target_type)
        if canonical_source is None or canonical_target is None:
            raise ValueError(
                f"unknown endpoint class for {relation_code!r}: "
                f"{source_type!r} -> {target_type!r}"
            )
        if (
            canonical_source not in definition.source_types
            or canonical_target not in definition.target_types
        ):
            raise ValueError(
                f"invalid endpoints for {relation_code!r}: "
                f"{canonical_source} -> {canonical_target}"
            )


def _as_type_set(value: Any) -> set[str]:
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return {str(item).strip() for item in values if str(item).strip()}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _validate_class_parents(classes: dict[str, ClassDefinition]) -> None:
    for definition in classes.values():
        if definition.parent is None:
            continue
        if definition.parent not in classes:
            raise ValueError(
                f"class {definition.code!r} references unknown parent "
                f"{definition.parent!r}"
            )

    for code in classes:
        seen: set[str] = set()
        current: str | None = code
        while current is not None:
            if current in seen:
                raise ValueError(f"class hierarchy contains a cycle at {current!r}")
            seen.add(current)
            current = classes[current].parent
