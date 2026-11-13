"""Atomic, fail-closed storage for ontology versions, changes, and reviews.

Version and change directories are write-once: they are built under an ignored
staging directory and renamed into place only after their files are complete.
Review events use one immutable file per event, so appending cannot overwrite
another writer's event.

The active ontology pointer is atomically replaced but has no cross-process
compare-and-swap. Two simultaneous activations therefore have a last-writer
wins residue. W8 publication must serialize review and publication at the
service layer before calling ``activate``; this primitive only guarantees that
readers see a complete old or new pointer, never partial JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from lightrag.fault_tree.governance.models import (
    ActiveOntologyPointer,
    OntologyChangeSet,
    OntologyPublicationRecord,
    OntologyVersionManifest,
    ReviewEvent,
    StandardSourceManifest,
)
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.file_atomic import atomic_write

_WORKSPACE = "ontology-governance"


class OntologyGovernanceError(RuntimeError):
    """Base error for malformed or conflicting governance state."""


class OntologyVersionConflictError(OntologyGovernanceError):
    """A version identifier is already installed with different content."""


class OntologyVersionNotFoundError(OntologyGovernanceError):
    """A requested ontology version is not installed."""


class OntologyPointerError(OntologyGovernanceError):
    """The active pointer is missing, corrupt, or points at invalid data."""


class ChangeSetConflictError(OntologyGovernanceError):
    """A change identifier is already used by a different immutable record."""


class ReviewEventConflictError(OntologyGovernanceError):
    """A review event identifier is already used by different content."""


@dataclass(frozen=True)
class LoadedOntology:
    """One installed ontology version loaded and verified from disk."""

    manifest: OntologyVersionManifest
    ontology_yaml: bytes
    path: Path

    def document(self) -> dict[str, Any]:
        """Return the YAML document after hash verification."""

        try:
            value = yaml.safe_load(self.ontology_yaml.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise OntologyGovernanceError(
                f"cannot decode ontology {self.manifest.ontology_version!r}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise OntologyGovernanceError(
                f"ontology {self.manifest.ontology_version!r} must contain a mapping"
            )
        return value

    def registry(self) -> OntologyRegistry:
        """Return the validated registry for this immutable version."""

        registry = OntologyRegistry.from_file(self.path.parent / "ontology.yml")
        if registry.version != self.manifest.ontology_version:
            raise OntologyGovernanceError(
                f"manifest version {self.manifest.ontology_version!r} does "
                f"not match ontology version {registry.version!r}"
            )
        if self.document().get("ontology_version") != self.manifest.ontology_version:
            raise OntologyGovernanceError(
                "verified YAML and manifest disagree on ontology_version"
            )
        return registry

    def manifest_path(self) -> Path:
        """Return the manifest path associated with this loaded version."""

        return self.path


class OntologyVersionStore:
    """Store immutable ontology versions and a single active pointer."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.versions_dir = self.root / "versions"
        self.active_path = self.root / "active.json"

    def install(
        self,
        ontology_path: str | Path,
        *,
        actor: str,
        notes: str | None = None,
        activate: bool = False,
    ) -> OntologyVersionManifest:
        """Install one validated ontology version without mutating old versions.

        Reinstalling the same version with identical bytes is idempotent.
        Different bytes under an existing version fail closed.
        """

        source = Path(ontology_path).resolve()
        if not source.is_file():
            raise OntologyVersionNotFoundError(f"ontology source not found: {source}")
        raw = source.read_bytes()
        version = _version_from_yaml_bytes(raw, source)
        _validate_identifier(version, "ontology_version")
        actor = _required_text(actor, "actor")

        self.versions_dir.mkdir(parents=True, exist_ok=True)
        version_dir = self.versions_dir / version
        if version_dir.exists():
            return self._existing_install(version, raw)
        if version_dir.is_symlink():
            raise OntologyGovernanceError(
                f"ontology version path must not be a symlink: {version_dir}"
            )

        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.versions_dir))
        try:
            ontology_target = staging / "ontology.yml"
            ontology_target.write_bytes(raw)
            registry = OntologyRegistry.from_file(ontology_target)
            if registry.version != version:
                raise OntologyGovernanceError(
                    "ontology_version changed during version installation"
                )
            manifest = OntologyVersionManifest(
                ontology_version=version,
                schema_version=registry.schema_version,
                ontology_sha256=_sha256(raw),
                ontology_size=len(raw),
                installed_at=_utc_now(),
                installed_by=actor,
                notes=_optional_text(notes),
            )
            _atomic_write_bytes(
                staging / "manifest.json",
                _canonical_bytes(manifest),
            )
            try:
                os.rename(staging, version_dir)
            except FileExistsError:
                return self._existing_install(version, raw)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

        if activate:
            self.activate(version, actor=actor)
        return manifest

    def activate(
        self,
        version: str,
        *,
        actor: str,
    ) -> ActiveOntologyPointer:
        """Atomically point readers at an installed, verified version."""

        version = _validate_identifier(version, "version")
        actor = _required_text(actor, "actor")
        loaded = self.load_version(version)
        manifest_bytes = _read_bytes_or_error(
            loaded.manifest_path(),
            "ontology manifest",
        )
        previous: str | None = None
        if self.active_path.is_symlink():
            raise OntologyPointerError(
                f"active ontology pointer must not be a symlink: {self.active_path}"
            )
        if self.active_path.exists():
            current = self.read_active()
            if current.active_version == version and current.manifest_sha256 == _sha256(
                manifest_bytes
            ):
                return current
            previous = current.active_version

        pointer = ActiveOntologyPointer(
            active_version=version,
            ontology_sha256=loaded.manifest.ontology_sha256,
            manifest_sha256=_sha256(manifest_bytes),
            updated_at=_utc_now(),
            updated_by=actor,
            previous_version=previous,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(self.active_path, _canonical_bytes(pointer))
        return pointer

    def read_active(self) -> ActiveOntologyPointer:
        """Read and verify the active pointer; never fall back to another run."""

        if self.active_path.is_symlink():
            raise OntologyPointerError(
                f"active ontology pointer must not be a symlink: {self.active_path}"
            )
        if not self.active_path.is_file():
            raise OntologyPointerError(
                f"active ontology pointer not found: {self.active_path}"
            )
        try:
            payload = json.loads(self.active_path.read_text(encoding="utf-8"))
            pointer = ActiveOntologyPointer.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise OntologyPointerError(
                f"cannot read active ontology pointer: {exc}"
            ) from exc

        loaded = self.load_version(pointer.active_version)
        manifest_bytes = _read_bytes_or_error(
            loaded.manifest_path(),
            "ontology manifest",
        )
        if _sha256(manifest_bytes) != pointer.manifest_sha256:
            raise OntologyPointerError(
                "active pointer manifest digest does not match installed manifest"
            )
        if loaded.manifest.ontology_sha256 != pointer.ontology_sha256:
            raise OntologyPointerError(
                "active pointer ontology digest does not match manifest"
            )
        return pointer

    def load_active(self) -> LoadedOntology:
        """Load the verified active ontology and its immutable manifest."""

        pointer = self.read_active()
        return self.load_version(pointer.active_version)

    def load_version(self, version: str) -> LoadedOntology:
        """Load one installed version without consulting the active pointer."""

        version = _validate_identifier(version, "version")
        version_dir = self.versions_dir / version
        if version_dir.is_symlink() or not version_dir.is_dir():
            raise OntologyVersionNotFoundError(
                f"ontology version {version!r} is not installed"
            )
        manifest_path = version_dir / "manifest.json"
        ontology_path = version_dir / "ontology.yml"
        if manifest_path.is_symlink() or ontology_path.is_symlink():
            raise OntologyGovernanceError(
                f"ontology version {version!r} contains a symlink"
            )
        manifest = self._read_manifest(version, manifest_path)
        raw = _read_bytes_or_error(ontology_path, "ontology file")
        if _sha256(raw) != manifest.ontology_sha256:
            raise OntologyGovernanceError(
                f"ontology {version!r} content does not match its manifest"
            )
        if len(raw) != manifest.ontology_size:
            raise OntologyGovernanceError(
                f"ontology {version!r} size does not match its manifest"
            )
        return LoadedOntology(
            manifest=manifest,
            ontology_yaml=raw,
            path=manifest_path,
        )

    def list_versions(self) -> tuple[str, ...]:
        """List installed versions without exposing staging directories."""

        if not self.versions_dir.is_dir():
            return ()
        versions: list[str] = []
        for entry in self.versions_dir.iterdir():
            if (
                entry.is_dir()
                and not entry.name.startswith(".")
                and (entry / "manifest.json").is_file()
            ):
                versions.append(entry.name)
        return tuple(sorted(versions))

    def _existing_install(
        self,
        version: str,
        raw: bytes,
    ) -> OntologyVersionManifest:
        loaded = self.load_version(version)
        if loaded.ontology_yaml != raw:
            raise OntologyVersionConflictError(
                f"ontology version {version!r} is already installed with "
                "different content"
            )
        return loaded.manifest

    def _read_manifest(
        self,
        version: str,
        path: Path,
    ) -> OntologyVersionManifest:
        if not path.is_file():
            raise OntologyGovernanceError(
                f"ontology version {version!r} has no manifest"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            manifest = OntologyVersionManifest.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise OntologyGovernanceError(
                f"cannot read ontology manifest for {version!r}: {exc}"
            ) from exc
        if manifest.ontology_version != version:
            raise OntologyGovernanceError(
                f"manifest version {manifest.ontology_version!r} does not "
                f"match directory {version!r}"
            )
        return manifest


class OntologyChangeStore:
    """Store immutable change sets and immutable review events."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.changes_dir = self.root / "changes"

    def create_change_set(
        self,
        change: OntologyChangeSet,
        *,
        source_text: str | None = None,
        source_name: str | None = None,
        stored_by: str | None = None,
    ) -> OntologyChangeSet:
        """Create an immutable change set, or return the identical existing one."""

        data = _canonical_bytes(change)
        source_manifest: StandardSourceManifest | None = None
        source_bytes: bytes | None = None
        source_manifest_bytes: bytes | None = None
        if source_text is not None:
            if not source_name or not source_name.strip():
                raise OntologyGovernanceError(
                    "source_name is required when source_text is stored"
                )
            if not stored_by or not stored_by.strip():
                raise OntologyGovernanceError(
                    "stored_by is required when source_text is stored"
                )
            source_bytes = source_text.encode("utf-8")
            source_manifest = StandardSourceManifest(
                source_name=source_name.strip(),
                source_sha256=_sha256(source_bytes),
                source_size=len(source_bytes),
                stored_at=_utc_now(),
                stored_by=stored_by.strip(),
            )
            source_manifest_bytes = _canonical_bytes(source_manifest)
        change_dir = self.changes_dir / change.change_id
        record_path = change_dir / "change.json"
        if change_dir.is_symlink():
            raise OntologyGovernanceError(
                f"change directory must not be a symlink: {change_dir}"
            )
        if change_dir.exists():
            existing = _read_bytes_or_error(
                record_path,
                "change set record",
            )
            if existing != data:
                raise ChangeSetConflictError(
                    f"change set {change.change_id!r} already exists with "
                    "different content"
                )
            self._verify_existing_source(
                change_dir,
                source_bytes=source_bytes,
                source_manifest_bytes=source_manifest_bytes,
            )
            return change

        self.changes_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.changes_dir))
        try:
            _atomic_write_bytes(staging / "change.json", data)
            if source_bytes is not None and source_manifest_bytes is not None:
                (staging / "source.txt").write_bytes(source_bytes)
                _atomic_write_bytes(
                    staging / "source.json",
                    source_manifest_bytes,
                )
            try:
                os.rename(staging, change_dir)
            except FileExistsError:
                existing = _read_bytes_or_error(
                    record_path,
                    "change set record",
                )
                if existing != data:
                    raise ChangeSetConflictError(
                        f"change set {change.change_id!r} already exists "
                        "with different content"
                    )
                self._verify_existing_source(
                    change_dir,
                    source_bytes=source_bytes,
                    source_manifest_bytes=source_manifest_bytes,
                )
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return change

    def load_change_source(
        self,
        change_id: str,
    ) -> tuple[StandardSourceManifest, str] | None:
        """Return the immutable standard excerpt attached to a change."""

        change_id = _validate_identifier(change_id, "change_id")
        change_dir = self.changes_dir / change_id
        text_path = change_dir / "source.txt"
        manifest_path = change_dir / "source.json"
        if not text_path.exists() and not manifest_path.exists():
            return None
        if text_path.is_symlink() or manifest_path.is_symlink():
            raise OntologyGovernanceError(
                f"change source {change_id!r} must not contain symlinks"
            )
        try:
            source_bytes = text_path.read_bytes()
            manifest_bytes = manifest_path.read_bytes()
            payload = json.loads(manifest_bytes.decode("utf-8"))
            manifest = StandardSourceManifest.model_validate(payload)
            source_text = source_bytes.decode("utf-8")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
        ) as exc:
            raise OntologyGovernanceError(
                f"cannot read change source {change_id!r}: {exc}"
            ) from exc
        if _sha256(source_bytes) != manifest.source_sha256:
            raise OntologyGovernanceError(
                f"change source {change_id!r} does not match its manifest"
            )
        if len(source_bytes) != manifest.source_size:
            raise OntologyGovernanceError(
                f"change source {change_id!r} size does not match its manifest"
            )
        return manifest, source_text

    def write_publication_record(
        self,
        record: OntologyPublicationRecord,
    ) -> None:
        """Atomically persist publication state for retry and rollback."""

        self.load_change_set(record.change_id)
        path = self.changes_dir / record.change_id / "publication.json"
        if path.is_symlink():
            raise OntologyGovernanceError(
                f"publication record must not be a symlink: {path}"
            )
        _atomic_write_bytes(path, _canonical_bytes(record))

    def load_publication_record(
        self,
        change_id: str,
    ) -> OntologyPublicationRecord | None:
        """Load the publication record when one has been prepared."""

        self.load_change_set(change_id)
        path = self.changes_dir / change_id / "publication.json"
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink():
            raise OntologyGovernanceError(f"publication record is invalid: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return OntologyPublicationRecord.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise OntologyGovernanceError(
                f"cannot read publication record {change_id!r}: {exc}"
            ) from exc

    def load_change_set(self, change_id: str) -> OntologyChangeSet:
        """Load one immutable change set by identifier."""

        change_id = _validate_identifier(change_id, "change_id")
        path = self.changes_dir / change_id / "change.json"
        if not path.is_file() or path.is_symlink():
            raise OntologyGovernanceError(f"change set {change_id!r} is not available")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            change = OntologyChangeSet.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise OntologyGovernanceError(
                f"cannot read change set {change_id!r}: {exc}"
            ) from exc
        if change.change_id != change_id:
            raise OntologyGovernanceError(
                f"change record id {change.change_id!r} does not match "
                f"directory {change_id!r}"
            )
        return change

    def append_review_event(self, event: ReviewEvent) -> ReviewEvent:
        """Persist one immutable review event without rewriting older events."""

        self.load_change_set(event.change_id)
        event_dir = self.changes_dir / event.change_id / "events"
        if event_dir.is_symlink():
            raise OntologyGovernanceError(
                f"review event directory must not be a symlink: {event_dir}"
            )
        event_dir.mkdir(parents=True, exist_ok=True)
        path = event_dir / f"{event.event_id}.json"
        if path.is_symlink():
            raise OntologyGovernanceError(f"review event must not be a symlink: {path}")
        data = _canonical_bytes(event)
        if path.exists():
            existing = _read_bytes_or_error(path, "review event")
            if existing != data:
                raise ReviewEventConflictError(
                    f"review event {event.event_id!r} already exists with "
                    "different content"
                )
            return event
        _atomic_write_bytes(path, data)
        return event

    def list_review_events(self, change_id: str) -> tuple[ReviewEvent, ...]:
        """Return all review events in stable time and identifier order."""

        self.load_change_set(change_id)
        event_dir = self.changes_dir / change_id / "events"
        if event_dir.is_symlink():
            raise OntologyGovernanceError(
                f"review event directory must not be a symlink: {event_dir}"
            )
        if not event_dir.is_dir():
            return ()
        events: list[ReviewEvent] = []
        ids: set[str] = set()
        for path in sorted(event_dir.glob("*.json")):
            if path.is_symlink():
                raise OntologyGovernanceError(
                    f"review event must not be a symlink: {path}"
                )
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                event = ReviewEvent.model_validate(payload)
            except (OSError, json.JSONDecodeError, ValidationError) as exc:
                raise OntologyGovernanceError(
                    f"cannot read review event {path.name!r}: {exc}"
                ) from exc
            if event.change_id != change_id:
                raise OntologyGovernanceError(
                    f"review event {event.event_id!r} belongs to change "
                    f"{event.change_id!r}, not {change_id!r}"
                )
            if event.event_id in ids:
                raise OntologyGovernanceError(
                    f"duplicate review event id {event.event_id!r}"
                )
            ids.add(event.event_id)
            events.append(event)
        return tuple(
            sorted(
                events,
                key=lambda item: (
                    item.occurred_at,
                    item.event_id,
                ),
            )
        )

    def _verify_existing_source(
        self,
        change_dir: Path,
        *,
        source_bytes: bytes | None,
        source_manifest_bytes: bytes | None,
    ) -> None:
        if source_bytes is None or source_manifest_bytes is None:
            return
        text_path = change_dir / "source.txt"
        manifest_path = change_dir / "source.json"
        if (
            not text_path.is_file()
            or not manifest_path.is_file()
            or text_path.is_symlink()
            or manifest_path.is_symlink()
        ):
            raise ChangeSetConflictError(
                f"change set {change_dir.name!r} has no compatible source snapshot"
            )
        if text_path.read_bytes() != source_bytes:
            raise ChangeSetConflictError(
                f"change set {change_dir.name!r} has a different source snapshot"
            )
        try:
            expected = json.loads(source_manifest_bytes.decode("utf-8"))
            actual = json.loads(manifest_path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChangeSetConflictError(
                f"change set {change_dir.name!r} has an invalid source manifest"
            ) from exc
        stable_fields = (
            "source_name",
            "source_sha256",
            "source_size",
            "stored_by",
        )
        if any(expected.get(key) != actual.get(key) for key in stable_fields):
            raise ChangeSetConflictError(
                f"change set {change_dir.name!r} has a different source manifest"
            )


class OntologyGovernanceStore:
    """Facade over the version and change stores sharing one root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.versions = OntologyVersionStore(self.root)
        self.changes = OntologyChangeStore(self.root)


def _version_from_yaml_bytes(raw: bytes, source: Path) -> str:
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OntologyGovernanceError(
            f"cannot read ontology source {source}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise OntologyGovernanceError(
            f"ontology source {source} must contain a mapping"
        )
    version = str(document.get("ontology_version") or "").strip()
    if not version:
        raise OntologyGovernanceError(
            f"ontology source {source} must define ontology_version"
        )
    return version


def _validate_identifier(value: str, field: str) -> str:
    text = _required_text(value, field)
    if (
        text in {".", ".."}
        or "/" in text
        or "\\" in text
        or not all(
            character.isascii() and (character.isalnum() or character in "._-")
            for character in text
        )
        or not text[0].isalnum()
    ):
        raise OntologyGovernanceError(
            f"{field} must be a safe ASCII identifier: {text!r}"
        )
    return text


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OntologyGovernanceError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_bytes(model: Any) -> bytes:
    payload = model.model_dump(mode="json")
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{text}\n".encode("utf-8")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        str(path),
        lambda temporary: Path(temporary).write_bytes(data),
        workspace=_WORKSPACE,
    )


def _read_bytes_or_error(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise OntologyGovernanceError(f"cannot read {label} {path}: {exc}") from exc


__all__ = [
    "ChangeSetConflictError",
    "LoadedOntology",
    "OntologyChangeStore",
    "OntologyGovernanceError",
    "OntologyGovernanceStore",
    "OntologyPointerError",
    "OntologyVersionConflictError",
    "OntologyVersionNotFoundError",
    "OntologyVersionStore",
    "ReviewEventConflictError",
]
