"""User-facing document ingestion for the industrial fault domain.

The W6 pipeline was intentionally fixture-driven.  This module adds the
runtime path: discover a file uploaded through Nexent, parse it locally,
extract fault candidates with the W7 student model, and append the result to
the tenant's active immutable snapshot.

The module does not expose HTTP or MCP.  ``ingestion_mcp_server`` is the thin
transport layer; keeping the domain logic here makes the complete upload path
testable without Nexent, Docker, or a live model.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from lightrag.fault_tree.contracts import (
    DomainBundle,
    EntityRecord,
    EvidenceTarget,
    Fact,
    FaultEvent,
    FileAsset,
)
from lightrag.fault_tree.distillation.prompts import (
    build_system_prompt,
    build_user_prompt,
    load_prompt_profile,
)
from lightrag.fault_tree.distillation.teacher import (
    TeacherError,
    TeacherOutputError,
    extract_json_object,
    normalize_teacher_target,
)
from lightrag.fault_tree.domain_validation import (
    errors_only,
    load_domain_bundle,
    validate_domain_bundle,
)
from lightrag.fault_tree.governance import OntologyGovernanceError, OntologyVersionStore
from lightrag.fault_tree.importer import (
    ImportedDomain,
    build_file_asset,
    write_bundle_outputs,
    write_graph,
)
from lightrag.fault_tree.multimodal import (
    DocumentSpec,
    ImageTranscriber,
    MultimodalError,
    ParsedDocument,
    parse_image,
    parse_pdf_pages,
    parse_table_rows,
)
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.fault_tree.projection import (
    CandidateBatchInput,
    project_domain,
)
from lightrag.fault_tree.schemas import FaultCandidateBatch

SUPPORTED_SUFFIXES = frozenset(
    {".pdf", ".csv", ".xlsx", ".xlsm", ".png", ".jpg", ".jpeg", ".txt", ".md"}
)
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
TABLE_SUFFIXES = frozenset({".csv", ".xlsx", ".xlsm"})
TEXT_SUFFIXES = frozenset({".txt", ".md"})
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_TEXT_CHUNK_CHARS = 4000


class IngestionError(RuntimeError):
    """A stable ingestion failure that can be returned by an MCP tool."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class CandidateExtractor(Protocol):
    """The narrow interface shared by the student model and test doubles."""

    def extract(
        self,
        *,
        chunk_id: str,
        asset_id: str,
        chunk_text: str,
        file_name: str,
        page: int | None = None,
        section_path: str | None = None,
        modality: str = "text",
    ) -> FaultCandidateBatch: ...


@dataclass(frozen=True)
class UploadedDocument:
    """One discoverable file from an allowed upload root."""

    document_id: str
    path: Path
    file_name: str
    size: int
    modified_at: datetime
    suffix: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "file_name": self.file_name,
            "size": self.size,
            "modified_at": self.modified_at.isoformat(),
            "suffix": self.suffix,
        }


@dataclass(frozen=True)
class ParsedUpload:
    """Original file plus its locally parsed transcript."""

    source: Path
    parsed: ParsedDocument
    file_name: str
    mime_type: str
    digest: str

    @property
    def modality(self) -> str:
        return self.parsed.modality


class OpenAICompatibleStudentExtractor:
    """Call a local Qwen/Ollama OpenAI-compatible chat endpoint.

    The endpoint must be a local inference service.  We intentionally accept
    no external fallback: if the student is unavailable, ingestion fails
    loudly instead of silently replacing W7 with another model.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        api_key: str = "",
        timeout: float = 120.0,
        max_output_tokens: int = 1536,
        max_attempts: int = 2,
        system_prompt: str | None = None,
    ) -> None:
        if not endpoint.strip():
            raise IngestionError(
                "extractor_not_configured",
                "the local W7 student endpoint is not configured",
            )
        if not model.strip():
            raise IngestionError(
                "extractor_not_configured",
                "the local W7 student model name is not configured",
            )
        self.endpoint = endpoint.rstrip("/")
        self.model = model.strip()
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.max_attempts = max(1, max_attempts)
        self.system_prompt = system_prompt or ""

    def extract(
        self,
        *,
        chunk_id: str,
        asset_id: str,
        chunk_text: str,
        file_name: str,
        page: int | None = None,
        section_path: str | None = None,
        modality: str = "text",
    ) -> FaultCandidateBatch:
        user_prompt = build_user_prompt(
            {
                "chunk_id": chunk_id,
                "asset_id": asset_id,
                "text": chunk_text,
                "page": page,
                "section_path": section_path,
                "modality": modality,
            },
            file_name=file_name,
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_error: IngestionError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                content = self._request_content(payload, headers)
                raw = extract_json_object(content)
                target, warnings = normalize_teacher_target(raw, chunk_text)
                return FaultCandidateBatch(
                    chunk_id=chunk_id,
                    chunk_text=chunk_text,
                    asset_id=asset_id,
                    entities=target.get("entities") or [],
                    relations=target.get("relations") or [],
                    warnings=warnings,
                )
            except urllib.error.HTTPError:
                raise
            except (TeacherError, TeacherOutputError, ValueError) as exc:
                last_error = IngestionError(
                    "extractor_contract_error",
                    f"student output does not match the extraction contract: {exc}",
                    {"chunk_id": chunk_id, "attempt": attempt},
                )
            except IngestionError as exc:
                last_error = exc
            if attempt < self.max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
        assert last_error is not None
        raise last_error

    def _request_content(
        self,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> str:
        request = urllib.request.Request(
            self.chat_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            code = (
                "extractor_unavailable"
                if exc.code in {429, 500, 502, 503, 504}
                else "extractor_http_error"
            )
            raise IngestionError(
                code,
                f"W7 student endpoint returned HTTP {exc.code}",
                {"status": exc.code, "body": detail},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise IngestionError(
                "extractor_unavailable",
                f"cannot reach W7 student endpoint: {exc}",
                {"endpoint": self.endpoint},
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IngestionError(
                "extractor_invalid_response",
                "W7 student endpoint returned non-JSON data",
            ) from exc
        return _completion_content(body)

    def chat_url(self) -> str:
        if self.endpoint.endswith("/chat/completions"):
            return self.endpoint
        return self.endpoint + "/chat/completions"


class DocumentIngestionService:
    """Discover, parse, extract and append user documents."""

    def __init__(
        self,
        *,
        pack_dir: str | Path,
        storage_dir: str | Path,
        upload_dirs: Iterable[str | Path],
        extractor: CandidateExtractor | None = None,
        vlm: ImageTranscriber | None = None,
        ontology: OntologyRegistry | None = None,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        self.pack_dir = Path(pack_dir).resolve()
        self.storage_dir = Path(storage_dir).resolve()
        self.upload_dirs = _dedupe_paths(
            Path(item).expanduser().resolve() for item in upload_dirs
        )
        self.extractor = extractor
        self.vlm = vlm
        self.ontology = ontology or _load_active_ontology(
            self.pack_dir,
            self.storage_dir,
        )
        self.max_upload_bytes = max_upload_bytes
        self.ingestion_root = self.pack_dir / ".ingestion"
        self.status_path = self.ingestion_root / "status.json"

    def list_uploads(self, *, limit: int = 50) -> list[UploadedDocument]:
        documents: list[UploadedDocument] = []
        for root in self.upload_dirs:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if (
                    not path.is_file()
                    or path.suffix.casefold() not in SUPPORTED_SUFFIXES
                ):
                    continue
                resolved = path.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                try:
                    size = resolved.stat().st_size
                except OSError:
                    continue
                if size <= 0 or size > self.max_upload_bytes:
                    continue
                modified = datetime.fromtimestamp(
                    resolved.stat().st_mtime, tz=timezone.utc
                )
                documents.append(
                    UploadedDocument(
                        document_id=_document_id(resolved, size, modified),
                        path=resolved,
                        file_name=resolved.name,
                        size=size,
                        modified_at=modified,
                        suffix=path.suffix.casefold(),
                    )
                )
        documents.sort(key=lambda item: item.modified_at, reverse=True)
        return _dedupe_documents(documents)[: max(1, min(limit, 200))]

    def resolve_upload(
        self,
        *,
        document_id: str | None = None,
        file_name: str | None = None,
    ) -> UploadedDocument:
        candidate_id = (document_id or "").strip()
        candidate_name = (file_name or "").strip()
        if not candidate_id and not candidate_name:
            latest = self.list_uploads(limit=2)
            if len(latest) == 1:
                return latest[0]
            raise IngestionError(
                "invalid_argument",
                "pass document_id or file_name; the upload inbox is ambiguous",
                {"available": [item.to_dict() for item in latest]},
            )
        matches = self.list_uploads(limit=200)
        if candidate_id:
            matches = [item for item in matches if item.document_id == candidate_id]
        if candidate_name:
            folded = candidate_name.casefold()
            matches = [item for item in matches if item.file_name.casefold() == folded]
        if not matches:
            raise IngestionError(
                "not_found",
                "uploaded document was not found in the configured inbox",
                {
                    "document_id": candidate_id or None,
                    "file_name": candidate_name or None,
                },
            )
        if len(matches) > 1:
            raise IngestionError(
                "ambiguous_upload",
                "more than one uploaded document matches; pass document_id",
                {"matches": [item.to_dict() for item in matches]},
            )
        return matches[0]

    def read_uploaded_document(
        self,
        *,
        document_id: str | None = None,
        file_name: str | None = None,
        max_chars: int = 50000,
    ) -> dict[str, Any]:
        upload = self.resolve_upload(
            document_id=document_id,
            file_name=file_name,
        )
        parsed_upload = self._parse(upload)
        text = parsed_upload.parsed.text
        if len(text) > max(1000, min(max_chars, 200000)):
            raise IngestionError(
                "document_too_large_for_tool",
                "parsed text is too large to return through one MCP response; "
                "ingest it into the domain or split the source document",
                {"characters": len(text), "max_chars": max_chars},
            )
        return {
            "document": upload.to_dict(),
            "digest": parsed_upload.digest,
            "modality": parsed_upload.modality,
            "mime_type": parsed_upload.mime_type,
            "page_count": parsed_upload.parsed.page_count,
            "chunk_count": len(parsed_upload.parsed.chunks),
            "source_text": text,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "page": chunk.page,
                    "section_path": chunk.section_path,
                    "char_base": chunk.char_base,
                }
                for chunk in parsed_upload.parsed.chunks
            ],
        }

    def ingest_uploaded_document(
        self,
        *,
        tenant_id: str,
        document_id: str | None = None,
        file_name: str | None = None,
        equipment_ref: str | None = None,
    ) -> dict[str, Any]:
        tenant_id = tenant_id.strip()
        if not tenant_id:
            raise IngestionError("invalid_argument", "tenant_id is required")
        if self.extractor is None:
            raise IngestionError(
                "extractor_not_configured",
                "document ingestion needs the local W7 student extractor",
            )
        upload = self.resolve_upload(
            document_id=document_id,
            file_name=file_name,
        )
        try:
            parsed_upload = self._parse(upload)
        except IngestionError as exc:
            self._write_status(
                tenant_id=tenant_id,
                document_id=upload.document_id,
                file_name=upload.file_name,
                status="failed",
                error_code=exc.code,
                error_message=exc.message,
            )
            raise
        asset_id = "FILE-UPLOAD-" + parsed_upload.digest[:12].upper()
        stored = self._persist_parsed_document(
            tenant_id=tenant_id,
            upload=upload,
            parsed_upload=parsed_upload,
            asset_id=asset_id,
        )
        asset = build_file_asset(
            pack=self.pack_dir,
            spec={
                "asset_uid": asset_id,
                "path": stored["source_uri"],
                "canonical_name": upload.file_name,
                "description": "Imported through the Nexent upload workflow",
                "modality": parsed_upload.modality,
                "mime_type": parsed_upload.mime_type,
                "text_uri": stored["text_uri"],
                "parse_status": "parsed",
                "parse_engine": parsed_upload.parsed.parse_engine,
                "page_count": parsed_upload.parsed.page_count,
                "chunk_count": len(parsed_upload.parsed.chunks),
                "source_system": "nexent-upload",
                "upload_time": upload.modified_at.isoformat(),
                "last_parsed_at": upload.modified_at.isoformat(),
            },
            tenant_id=tenant_id,
            ontology_version=self.ontology.version,
            schema_version=self.ontology.schema_version or "",
            created_by="nexent-upload",
        )
        batches: list[CandidateBatchInput] = []
        extraction_warnings: list[str] = []
        for chunk in parsed_upload.parsed.chunks:
            try:
                batch = self.extractor.extract(
                    chunk_id=chunk.chunk_id,
                    asset_id=chunk.asset_id,
                    chunk_text=chunk.text,
                    file_name=upload.file_name,
                    page=chunk.page,
                    section_path=chunk.section_path,
                    modality=chunk.modality,
                )
            except IngestionError:
                self._write_status(
                    tenant_id=tenant_id,
                    document_id=upload.document_id,
                    file_name=upload.file_name,
                    status="failed",
                    error_code="extractor_failed",
                    error_message="W7 extraction failed; no snapshot was changed",
                )
                raise
            extraction_warnings.extend(batch.warnings)
            batches.append(CandidateBatchInput(chunk=chunk, batch=batch))

        projected = project_domain(
            self.ontology,
            tenant_id=tenant_id,
            batches=batches,
            assets=[asset],
            schema_version=self.ontology.schema_version,
            review_status="proposed",
            created_by="nexent-upload",
            created_at=upload.modified_at,
            extractor="W7 student",
            extraction_run_id="upload-" + parsed_upload.digest[:12],
        )
        existing = self._load_active_bundle(tenant_id)
        equipment = _associate_faults_with_equipment(
            existing,
            projected,
            equipment_ref=equipment_ref,
            text=parsed_upload.parsed.text,
        )
        merged = _merge_bundle(existing, projected)
        issues = validate_domain_bundle(merged, self.pack_dir)
        errors = errors_only(issues)
        if errors:
            self._write_status(
                tenant_id=tenant_id,
                document_id=upload.document_id,
                file_name=upload.file_name,
                status="failed",
                error_code="domain_validation_failed",
                error_message="; ".join(issue.message for issue in errors[:5]),
            )
            raise IngestionError(
                "domain_validation_failed",
                "imported candidates failed domain validation; the active "
                "snapshot was not changed",
                {
                    "issues": [
                        {
                            "code": issue.code,
                            "message": issue.message,
                            "target_id": issue.target_id,
                        }
                        for issue in errors[:20]
                    ]
                },
            )

        imported = ImportedDomain(
            workspace=_workspace(batches, [asset], tenant_id),
            projected=projected,
            tree=merged.tree,
            bundle=merged,
        )
        published = write_bundle_outputs(imported, self.storage_dir)
        graph = __import__("asyncio").run(write_graph(imported, self.storage_dir))
        result = {
            "document": upload.to_dict(),
            "asset_id": asset_id,
            "tenant_id": tenant_id,
            "run_id": published["run_id"],
            "chunk_count": len(parsed_upload.parsed.chunks),
            "projected_entities": sum(
                1 for item in projected.entities if not isinstance(item, FileAsset)
            ),
            "projected_facts": len(projected.facts),
            "projected_rules": len(projected.rules),
            "projected_evidence": len(projected.evidence),
            "associated_equipment_uid": equipment["equipment_uid"],
            "associated_faults": equipment["fault_uids"],
            "active_entities": len(merged.entities),
            "active_evidence": len(merged.evidence),
            "active_facts": len(merged.facts),
            "active_rules": len(merged.rules),
            "graph": graph,
            "warnings": [*projected.warnings, *extraction_warnings],
        }
        self._write_status(
            tenant_id=tenant_id,
            document_id=upload.document_id,
            file_name=upload.file_name,
            status="published",
            result=result,
        )
        return result

    def status(
        self,
        *,
        tenant_id: str,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self._load_status()
        documents = payload.get("documents") or {}
        tenant = documents.get(tenant_id) or {}
        if document_id:
            item = tenant.get(document_id)
            if not isinstance(item, dict):
                raise IngestionError(
                    "not_found",
                    "no ingestion status exists for that document",
                    {"document_id": document_id, "tenant_id": tenant_id},
                )
            return item
        return {
            "tenant_id": tenant_id,
            "documents": [
                value for _, value in sorted(tenant.items()) if isinstance(value, dict)
            ],
        }

    def _parse(self, upload: UploadedDocument) -> ParsedUpload:
        digest = _file_sha256(upload.path)
        asset_id = "FILE-UPLOAD-" + digest[:12].upper()
        document_spec = DocumentSpec(
            asset_id=asset_id,
            path=upload.path,
            relative_path=upload.path.name,
            modality=_modality(upload.suffix),
            extraction=(),
            canonical_name=upload.file_name,
            description="Imported uploaded document",
            mime_type=mimetypes.guess_type(upload.file_name)[0]
            or "application/octet-stream",
            language=None,
            header_row=True,
        )
        try:
            if upload.suffix == ".pdf":
                parsed = parse_pdf_pages(document_spec)
            elif upload.suffix in TABLE_SUFFIXES:
                parsed = parse_table_rows(document_spec)
            elif upload.suffix in IMAGE_SUFFIXES:
                if self.vlm is None:
                    raise IngestionError(
                        "vlm_not_configured",
                        "image ingestion needs a local OpenAI-compatible VLM",
                    )
                parsed = parse_image(document_spec, vlm=self.vlm)
            elif upload.suffix in TEXT_SUFFIXES:
                parsed = _parse_text(upload.path, asset_id)
            else:
                raise IngestionError(
                    "unsupported",
                    f"unsupported file type {upload.suffix!r}",
                )
        except MultimodalError as exc:
            raise IngestionError(exc.code, exc.message) from exc
        return ParsedUpload(
            source=upload.path,
            parsed=parsed,
            file_name=upload.file_name,
            mime_type=document_spec.mime_type,
            digest=digest,
        )

    def _persist_parsed_document(
        self,
        *,
        tenant_id: str,
        upload: UploadedDocument,
        parsed_upload: ParsedUpload,
        asset_id: str,
    ) -> dict[str, str]:
        safe_tenant = re.sub(r"[^A-Za-z0-9._-]+", "-", tenant_id).strip("-")
        if not safe_tenant:
            raise IngestionError("invalid_argument", "tenant_id is not filename-safe")
        directory = (
            self.ingestion_root
            / "tenants"
            / safe_tenant
            / "documents"
            / parsed_upload.digest
        )
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", upload.file_name).strip("-")
        source_path = directory / (safe_name or "upload.bin")
        text_path = directory / "parsed.txt"
        if (
            not source_path.is_file()
            or _file_sha256(source_path) != parsed_upload.digest
        ):
            _atomic_copy(upload.path, source_path)
        _atomic_write_text(text_path, parsed_upload.parsed.text)
        return {
            "source_uri": source_path.relative_to(self.pack_dir).as_posix(),
            "text_uri": text_path.relative_to(self.pack_dir).as_posix(),
            "asset_id": asset_id,
        }

    def _load_active_bundle(self, tenant_id: str) -> DomainBundle:
        active_path = self.storage_dir / "tenants" / tenant_id / "active.json"
        if not active_path.is_file():
            raise IngestionError(
                "domain_not_initialized",
                f"tenant {tenant_id!r} has no active domain snapshot",
                {"active_pointer": str(active_path)},
            )
        try:
            pointer = json.loads(active_path.read_text(encoding="utf-8"))
            run_id = str(pointer["run_id"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise IngestionError(
                "domain_state_invalid",
                f"cannot read active domain pointer {active_path}: {exc}",
            ) from exc
        bundle_path = (
            self.storage_dir
            / "tenants"
            / tenant_id
            / "runs"
            / run_id
            / "bundle.full.json"
        )
        if not bundle_path.is_file():
            raise IngestionError(
                "domain_state_invalid",
                f"active run has no full bundle at {bundle_path}",
            )
        try:
            return load_domain_bundle(bundle_path)
        except (OSError, ValueError) as exc:
            raise IngestionError(
                "domain_state_invalid",
                f"cannot load active full bundle: {exc}",
            ) from exc

    def _load_status(self) -> dict[str, Any]:
        if not self.status_path.is_file():
            return {"version": 1, "documents": {}}
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "documents": {}}
        return payload if isinstance(payload, dict) else {"version": 1, "documents": {}}

    def _write_status(
        self,
        *,
        tenant_id: str,
        document_id: str,
        file_name: str,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        payload = self._load_status()
        documents = payload.setdefault("documents", {})
        tenant = documents.setdefault(tenant_id, {})
        tenant[document_id] = {
            "document_id": document_id,
            "file_name": file_name,
            "tenant_id": tenant_id,
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
            "result": dict(result or {}),
            "updated_at": _now_iso(),
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            self.status_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )


def build_extractor_from_env(
    env: Mapping[str, str] | None = None,
    *,
    prompt_profile: str | Path | None = None,
) -> OpenAICompatibleStudentExtractor | None:
    """Build the W7 extractor from deployment environment variables."""

    values = os.environ if env is None else env
    endpoint = str(values.get("W7_STUDENT_ENDPOINT") or "").strip()
    model = str(values.get("W7_STUDENT_MODEL") or "").strip()
    if not endpoint and not model:
        return None
    profile_path = (
        Path(prompt_profile)
        if prompt_profile
        else (
            Path(__file__).resolve().parents[2]
            / "prompts"
            / "entity_type"
            / "industrial_fault.yml"
        )
    )
    system_prompt = build_system_prompt(load_prompt_profile(profile_path))
    try:
        timeout = float(values.get("W7_STUDENT_TIMEOUT") or 120)
        max_tokens = int(values.get("W7_STUDENT_MAX_OUTPUT_TOKENS") or 1536)
        max_attempts = int(values.get("W7_STUDENT_MAX_ATTEMPTS") or 2)
    except ValueError as exc:
        raise IngestionError(
            "extractor_not_configured",
            f"invalid W7 student numeric setting: {exc}",
        ) from exc
    return OpenAICompatibleStudentExtractor(
        endpoint,
        model,
        api_key=str(values.get("W7_STUDENT_API_KEY") or ""),
        timeout=timeout,
        max_output_tokens=max_tokens,
        max_attempts=max_attempts,
        system_prompt=system_prompt,
    )


def _completion_content(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise IngestionError(
            "extractor_invalid_response",
            "W7 student response has no choices[0].message.content",
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise IngestionError(
            "extractor_empty_output",
            "W7 student returned empty content",
        )
    return content


def _load_active_ontology(
    pack_dir: Path,
    storage_dir: Path,
) -> OntologyRegistry:
    governance_root = storage_dir / "governance"
    if not (governance_root / "active.json").is_file():
        return OntologyRegistry.from_file(pack_dir / "ontology.yml")
    try:
        active = OntologyVersionStore(governance_root).load_active()
        return OntologyRegistry.from_file(active.path.parent / "ontology.yml")
    except (OntologyGovernanceError, OSError, ValueError) as exc:
        raise IngestionError(
            "ontology_state_invalid",
            f"cannot load the active governed ontology: {exc}",
        ) from exc


def _merge_bundle(
    existing: DomainBundle,
    projected: Any,
) -> DomainBundle:
    entities = _merge_records(
        existing.entities,
        projected.entities,
        key=lambda item: item.uid,
    )
    evidence = _merge_records(
        existing.evidence,
        projected.evidence,
        key=lambda item: item.evidence_id,
    )
    facts = _merge_records(
        existing.facts,
        projected.facts,
        key=lambda item: item.fact_id,
    )
    rules = _merge_records(
        existing.rules,
        projected.rules,
        key=lambda item: item.rule_uid,
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "entities": [item.model_dump(mode="json") for item in entities],
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "facts": [item.model_dump(mode="json") for item in facts],
                "rules": [item.model_dump(mode="json") for item in rules],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:8]
    base_version = existing.bundle_version.split("+upload-", 1)[0]
    return existing.model_copy(
        update={
            "bundle_version": f"{base_version}+upload-{digest}",
            "entities": entities,
            "evidence": evidence,
            "facts": facts,
            "rules": rules,
        }
    )


def _associate_faults_with_equipment(
    existing: DomainBundle,
    projected: Any,
    *,
    equipment_ref: str | None,
    text: str,
) -> dict[str, Any]:
    """Attach extracted faults to a known equipment through HAS_FAULT facts.

    The W7 student contract is deliberately narrow (FaultEvent + CAUSES), so it
    does not emit Equipment entities. Runtime ingestion resolves equipment
    deterministically from the existing tenant snapshot and adds only a
    fact-supported association. No equipment is fabricated when the reference
    cannot be resolved.
    """

    equipment = _find_equipment(existing, equipment_ref=equipment_ref, text=text)
    if equipment is None:
        if (equipment_ref or "").strip():
            raise IngestionError(
                "equipment_not_found",
                f"equipment {equipment_ref!r} was not found in the active tenant",
            )
        return {"equipment_uid": None, "fault_uids": []}
    evidence_by_id = {item.evidence_id: item for item in projected.evidence}
    existing_fact_ids = {item.fact_id for item in projected.facts}
    associated: list[str] = []
    for entity in projected.entities:
        if not isinstance(entity, FaultEvent):
            continue
        evidence_ids = [item for item in entity.evidence_ids if item in evidence_by_id]
        if not evidence_ids:
            continue
        fact_id = (
            "FACT-"
            + hashlib.sha1(
                f"{equipment.uid}\x1fHAS_FAULT\x1f{entity.uid}".encode("utf-8")
            ).hexdigest()[:16]
        )
        if fact_id in existing_fact_ids:
            continue
        projected.facts.append(
            Fact(
                fact_id=fact_id,
                tenant_id=projected_tenant(projected),
                subject_uid=equipment.uid,
                predicate="HAS_FAULT",
                object_uid=entity.uid,
                properties={"source": "nexent-upload"},
                evidence_ids=evidence_ids,
                confidence=entity.confidence,
                review_status="proposed",
                ontology_version=entity.ontology_version,
            )
        )
        for evidence_id in evidence_ids:
            evidence = evidence_by_id[evidence_id]
            if any(
                target.target_type == "fact" and target.target_id == fact_id
                for target in evidence.targets
            ):
                continue
            evidence.targets.append(
                EvidenceTarget(
                    target_type="fact",
                    target_id=fact_id,
                    role="supports",
                )
            )
        existing_fact_ids.add(fact_id)
        associated.append(entity.uid)
    return {"equipment_uid": equipment.uid, "fault_uids": sorted(associated)}


def projected_tenant(projected: Any) -> str:
    """Return the tenant id shared by projected domain records."""

    for collection in (
        projected.entities,
        projected.evidence,
        projected.facts,
        projected.rules,
    ):
        for item in collection:
            tenant_id = getattr(item, "tenant_id", None)
            if isinstance(tenant_id, str) and tenant_id:
                return tenant_id
    raise IngestionError(
        "projection_tenant_missing",
        "projected domain contains no tenant-scoped record",
    )


def _find_equipment(
    bundle: DomainBundle,
    *,
    equipment_ref: str | None,
    text: str,
) -> EntityRecord | None:
    from lightrag.fault_tree.contracts import Equipment

    equipment = [item for item in bundle.entities if isinstance(item, Equipment)]
    reference = (equipment_ref or "").strip().casefold()
    if reference:
        for item in equipment:
            names = [
                item.uid,
                item.equipment_tag,
                item.equipment_name,
                item.canonical_name,
                *item.aliases,
            ]
            if any(reference == str(name).casefold() for name in names):
                return item
        return None
    folded_text = text.casefold()
    matches: list[EntityRecord] = []
    for item in equipment:
        names = [
            item.equipment_tag,
            item.equipment_name,
            item.canonical_name,
            *item.aliases,
        ]
        if any(
            str(name).strip() and str(name).casefold() in folded_text for name in names
        ):
            matches.append(item)
    return matches[0] if len(matches) == 1 else None


def _merge_records(
    existing: Iterable[Any],
    incoming: Iterable[Any],
    *,
    key: Any,
) -> list[Any]:
    merged: dict[str, Any] = {key(item): item for item in existing}
    for item in incoming:
        item_key = key(item)
        current = merged.get(item_key)
        if current is None:
            merged[item_key] = item
            continue
        merged[item_key] = _merge_record(current, item)
    return [merged[item_key] for item_key in sorted(merged)]


def _merge_record(existing: Any, incoming: Any) -> Any:
    """Union attribution and evidence; incoming wins other scalar fields."""

    current = existing.model_dump(mode="json")
    update = incoming.model_dump(mode="json")
    combined = {**current, **update}
    for field in (
        "aliases",
        "source_uids",
        "evidence_ids",
        "symptoms",
        "maintenance_measures",
    ):
        left = current.get(field)
        right = update.get(field)
        if isinstance(left, list) or isinstance(right, list):
            union: list[Any] = []
            for value in [*(left or []), *(right or [])]:
                if value not in union:
                    union.append(value)
            combined[field] = union
    left_confidence = current.get("confidence")
    right_confidence = update.get("confidence")
    if isinstance(left_confidence, (int, float)) or isinstance(
        right_confidence, (int, float)
    ):
        combined["confidence"] = max(
            float(left_confidence or 0),
            float(right_confidence or 0),
        )
    return type(existing).model_validate(combined)


def _workspace(
    batches: list[CandidateBatchInput],
    assets: list[EntityRecord],
    tenant_id: str,
):
    from lightrag.fault_tree.importer import SeedWorkspace

    return SeedWorkspace(
        context={"tenant_id": tenant_id},
        assets=assets,
        chunks=[item.chunk for item in batches],
        batches=batches,
    )


def _parse_text(path: Path, asset_id: str) -> ParsedDocument:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise IngestionError(
            "document_io_failed", f"cannot read {path}: {exc}"
        ) from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise IngestionError("document_empty", f"{path.name} contains no text")
    chunks = []
    offset = 0
    while offset < len(text):
        end = min(len(text), offset + DEFAULT_TEXT_CHUNK_CHARS)
        if end < len(text):
            newline = text.rfind("\n", offset, end)
            if newline > offset + 500:
                end = newline + 1
        body = text[offset:end]
        if body.strip():
            chunks.append(
                __import__(
                    "lightrag.fault_tree.projection",
                    fromlist=["SourceChunk"],
                ).SourceChunk(
                    asset_id=asset_id,
                    chunk_id=f"{asset_id}:text-{len(chunks) + 1}",
                    text=body,
                    page=1,
                    section_path=f"text-{len(chunks) + 1}",
                    modality="text",
                    char_base=offset,
                )
            )
        offset = end
    return ParsedDocument(
        asset_id=asset_id,
        modality="text",
        text=text,
        chunks=chunks,
        page_count=1,
        parse_engine="native",
    )


def _modality(suffix: str) -> str:
    if suffix == ".pdf":
        return "pdf"
    if suffix in TABLE_SUFFIXES:
        return "table"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "text"


def _document_id(path: Path, size: int, modified: datetime) -> str:
    raw = f"{path.resolve()}\0{size}\0{modified.isoformat()}"
    return "DOC-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12].upper()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dedupe_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        if path not in result:
            result.append(path)
    return tuple(result)


def _dedupe_documents(
    documents: list[UploadedDocument],
) -> list[UploadedDocument]:
    result: list[UploadedDocument] = []
    seen: set[tuple[str, str]] = set()
    for item in documents:
        key = (str(item.path), item.document_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp",
    )
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "CandidateExtractor",
    "DocumentIngestionService",
    "IngestionError",
    "OpenAICompatibleStudentExtractor",
    "ParsedUpload",
    "SUPPORTED_SUFFIXES",
    "UploadedDocument",
    "build_extractor_from_env",
]
