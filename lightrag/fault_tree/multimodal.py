"""Domain-side multimodal ingestion: PDF, table, and image to evidence chunks.

This module is the standalone W6 front end.  It parses a document into a
UTF-8 transcript plus :class:`SourceChunk` records whose ``page``/``char_base``
locators point into that transcript, then hands the chunks to the existing
projection/import chain.  Raw bytes never leave the process: PDF and table
parsing are in-process, and images go only to an OpenAI-compatible endpoint
that :func:`ensure_local_endpoint` has accepted as local.

Contracts a caller must respect:

* ``FileAsset.object_uri``/``checksum``/``file_size`` describe the original
  binary.  The parsed text lives in ``text_uri``; evidence quotes are verified
  against ``text_uri`` by ``domain_validation``.
* A parsed document always records a status.  One failing document does not
  abort the batch; the caller decides whether a partial import is acceptable.
* ``parse_*`` functions return chunks whose ``char_base`` is an offset into the
  full transcript, not into the chunk.  ``span_matches`` consumes that form.

Scanning, OCR, and layout analysis are deliberately out of scope; a PDF with no
text layer fails as ``pdf-no-text-layer`` and the caller should route it through
the server's MinerU/Docling sidecar instead.
"""

from __future__ import annotations

import base64
import csv
import io
import ipaddress
import json
import mimetypes
import os
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lightrag.fault_tree.contracts import EntityRecord
from lightrag.fault_tree.importer import (
    SeedInputError,
    SeedWorkspace,
    build_file_asset,
)
from lightrag.fault_tree.projection import CandidateBatchInput, SourceChunk
from lightrag.fault_tree.schemas import FaultCandidateBatch

_ALLOW_EXTERNAL_ENV = "DOMAIN_ALLOW_EXTERNAL_VLM"
_MAX_IMAGE_BYTES = 32 * 1024 * 1024

_IMAGE_PROMPT = (
    "Transcribe every visible character in this image exactly as written. "
    "Keep line breaks. Do not translate, explain, summarize, or invent text. "
    "Output only the transcription."
)


class MultimodalError(RuntimeError):
    """A document or manifest failure carrying a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ManifestError(MultimodalError):
    """The manifest is missing, malformed, or escapes the pack directory."""


class DocumentParseError(MultimodalError):
    """A source document could not be turned into a transcript."""


class ExtractionError(MultimodalError):
    """An extraction fixture does not match the parsed document."""


class VlmError(MultimodalError):
    """The local VLM endpoint failed or returned no usable text."""


class ExternalEndpointRefused(VlmError):
    """The configured endpoint is not local and external use was not allowed."""


class StatusStoreError(MultimodalError):
    """The on-disk document status file is unreadable."""


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DocumentManifest(_ManifestModel):
    asset_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    path: str = Field(min_length=1)
    modality: Literal["pdf", "table", "image"]
    canonical_name: str | None = None
    description: str | None = None
    mime_type: str | None = None
    language: str | None = None
    header_row: bool = True
    extraction: list[str] = Field(default_factory=list)


class VlmManifest(_ManifestModel):
    endpoint: str | None = None
    model: str | None = None
    api_key: str | None = None
    timeout: float = Field(default=120.0, gt=0)
    transcript_fixture: str | None = None


class _ManifestRoot(_ManifestModel):
    tenant_id: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)
    schema_version: str | None = None
    top_event_uid: str = Field(min_length=1)
    draft_id: str | None = None
    created_by: str = "multimodal-ingest"
    created_at: datetime | None = None
    review_status: str = "approved"
    bundle_version: str = "multimodal-import-v1"
    extraction_run_id: str = "multimodal-run-1"
    tree_version: int = Field(default=1, ge=1)
    text_dir: str = "seeds/multimodal/generated/text"
    documents: list[DocumentManifest] = Field(min_length=1)
    vlm: VlmManifest = Field(default_factory=VlmManifest)


@dataclass(frozen=True)
class DocumentSpec:
    asset_id: str
    path: Path
    relative_path: str
    modality: str
    extraction: tuple[Path, ...]
    canonical_name: str | None
    description: str | None
    mime_type: str
    language: str | None
    header_row: bool


@dataclass(frozen=True)
class MultimodalManifest:
    pack: Path
    tenant_id: str
    ontology_version: str
    schema_version: str
    top_event_uid: str
    draft_id: str | None
    created_by: str
    created_at: datetime
    review_status: str
    bundle_version: str
    extraction_run_id: str
    tree_version: int
    text_dir: Path
    text_uri_prefix: str
    documents: tuple[DocumentSpec, ...]
    vlm: VlmManifest

    def with_tenant(self, tenant_id: str | None) -> MultimodalManifest:
        override = str(tenant_id or "").strip()
        if not override:
            return self
        return replace(self, tenant_id=override)


def _resolve_pack_path(pack: Path, relative: str, *, field_name: str) -> Path:
    candidate = (pack / relative).resolve()
    try:
        candidate.relative_to(pack)
    except ValueError as exc:
        raise ManifestError(
            "manifest-path-escape",
            f"{field_name} {relative!r} points outside the pack directory",
        ) from exc
    return candidate


def load_manifest(
    pack: str | Path,
    manifest_path: str | Path | None = None,
) -> MultimodalManifest:
    """Load and validate a multimodal manifest.

    A relative ``manifest_path`` is resolved against ``pack``.  Every document
    and extraction path must stay inside the pack, and every asset ID must be a
    unique filename-safe token because it becomes the transcript filename.
    """

    pack_path = Path(pack).resolve()
    default_path = pack_path / "seeds" / "multimodal" / "manifest.json"
    path = (
        default_path
        if manifest_path is None
        else _resolve_pack_path(
            pack_path,
            str(manifest_path),
            field_name="manifest path",
        )
    )
    if not path.is_file():
        raise ManifestError(
            "manifest-missing", f"multimodal manifest not found at {path}"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(
            "manifest-invalid", f"cannot read {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ManifestError("manifest-invalid", f"{path} must be a JSON object")
    try:
        root = _ManifestRoot.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(
            "manifest-invalid", f"{path} is invalid: {exc}"
        ) from exc

    seen: set[str] = set()
    documents: list[DocumentSpec] = []
    for item in root.documents:
        if item.asset_id in seen:
            raise ManifestError(
                "manifest-duplicate-asset",
                f"asset {item.asset_id!r} appears more than once",
            )
        seen.add(item.asset_id)
        source = _resolve_pack_path(
            pack_path, item.path, field_name=f"asset {item.asset_id} path"
        )
        extraction = tuple(
            _resolve_pack_path(
                pack_path,
                relative,
                field_name=f"asset {item.asset_id} extraction",
            )
            for relative in item.extraction
        )
        documents.append(
            DocumentSpec(
                asset_id=item.asset_id,
                path=source,
                relative_path=item.path,
                modality=item.modality,
                extraction=extraction,
                canonical_name=item.canonical_name,
                description=item.description,
                mime_type=item.mime_type
                or mimetypes.guess_type(source.name)[0]
                or "application/octet-stream",
                language=item.language,
                header_row=item.header_row,
            )
        )

    text_dir = _resolve_pack_path(
        pack_path, root.text_dir, field_name="text_dir"
    )
    try:
        text_uri_prefix = text_dir.relative_to(pack_path).as_posix()
    except ValueError as exc:  # pragma: no cover - _resolve_pack_path guards
        raise ManifestError("manifest-invalid", "text_dir must be local") from exc

    schema_version = root.schema_version or root.ontology_version.replace(
        "industrial-fault", "industrial-fault-schema"
    )
    return MultimodalManifest(
        pack=pack_path,
        tenant_id=root.tenant_id,
        ontology_version=root.ontology_version,
        schema_version=schema_version,
        top_event_uid=root.top_event_uid,
        draft_id=root.draft_id,
        created_by=root.created_by,
        created_at=root.created_at
        or datetime.now(timezone.utc),
        review_status=root.review_status,
        bundle_version=root.bundle_version,
        extraction_run_id=root.extraction_run_id,
        tree_version=root.tree_version,
        text_dir=text_dir,
        text_uri_prefix=text_uri_prefix,
        documents=tuple(documents),
        vlm=root.vlm,
    )


# ---------------------------------------------------------------------------
# Status store
# ---------------------------------------------------------------------------


@dataclass
class DocumentStatus:
    asset_id: str
    status: Literal["pending", "parsed", "failed"] = "pending"
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
    page_count: int | None = None
    chunk_count: int | None = None
    text_uri: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "text_uri": self.text_uri,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, asset_id: str, raw: Any) -> DocumentStatus:
        if not isinstance(raw, dict):
            raise StatusStoreError(
                "status-store-corrupt",
                f"status for {asset_id!r} must be an object",
            )
        status = str(raw.get("status") or "")
        if status not in {"pending", "parsed", "failed"}:
            raise StatusStoreError(
                "status-store-corrupt",
                f"status for {asset_id!r} is invalid: {status!r}",
            )
        attempts = raw.get("attempts", 0)
        if not isinstance(attempts, int) or attempts < 0:
            raise StatusStoreError(
                "status-store-corrupt",
                f"attempts for {asset_id!r} is invalid",
            )
        return cls(
            asset_id=asset_id,
            status=status,  # type: ignore[arg-type]
            attempts=attempts,
            error_code=_optional_str(raw.get("error_code")),
            error_message=_optional_str(raw.get("error_message")),
            page_count=_optional_int(raw.get("page_count")),
            chunk_count=_optional_int(raw.get("chunk_count")),
            text_uri=_optional_str(raw.get("text_uri")),
            updated_at=_optional_str(raw.get("updated_at")),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StatusStoreError(
            "status-store-corrupt", f"expected an integer, got {value!r}"
        )
    return value


class DocumentStatusStore:
    """A small JSON status file written atomically after every document.

    The store is not multi-writer safe.  It exists so a failed ingest leaves an
    explicit record that a later ``--retry-failed`` run can pick up.
    """

    VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._statuses: dict[str, DocumentStatus] = {}
        self._loaded = False

    def load(self) -> dict[str, DocumentStatus]:
        if not self.path.is_file():
            self._statuses = {}
            self._loaded = True
            return dict(self._statuses)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StatusStoreError(
                "status-store-corrupt", f"cannot read {self.path}: {exc}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("version") != self.VERSION:
            raise StatusStoreError(
                "status-store-corrupt",
                f"{self.path} is not a version {self.VERSION} status file",
            )
        documents = raw.get("documents")
        if not isinstance(documents, dict):
            raise StatusStoreError(
                "status-store-corrupt", f"{self.path} has no documents map"
            )
        self._statuses = {
            asset_id: DocumentStatus.from_dict(asset_id, value)
            for asset_id, value in documents.items()
        }
        self._loaded = True
        return dict(self._statuses)

    def get(self, asset_id: str) -> DocumentStatus | None:
        self._ensure_loaded()
        return self._statuses.get(asset_id)

    def record(self, status: DocumentStatus) -> None:
        self._ensure_loaded()
        self._statuses[status.asset_id] = status
        self.save()

    def failed_ids(self) -> set[str]:
        self._ensure_loaded()
        return {
            asset_id
            for asset_id, status in self._statuses.items()
            if status.status == "failed"
        }

    def save(self) -> None:
        self._ensure_loaded()
        payload = {
            "version": self.VERSION,
            "documents": {
                asset_id: status.to_dict()
                for asset_id, status in sorted(self._statuses.items())
            },
        }
        _atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()


# ---------------------------------------------------------------------------
# Parsed documents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedDocument:
    asset_id: str
    modality: str
    text: str
    chunks: list[SourceChunk]
    page_count: int
    parse_engine: str
    text_uri: str | None = None


@dataclass(frozen=True)
class MultimodalWorkspace:
    workspace: SeedWorkspace
    statuses: list[DocumentStatus]

    @property
    def failed(self) -> list[DocumentStatus]:
        return [item for item in self.statuses if item.status == "failed"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
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


def _write_text_artifact(
    pack: Path, text_dir: Path, asset_id: str, text: str
) -> str:
    path = text_dir / f"{asset_id}.txt"
    _atomic_write_text(path, text)
    return path.resolve().relative_to(pack.resolve()).as_posix()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _pdf_page_texts(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentParseError(
            "parser-dependency-missing",
            "pypdf is required to parse PDF documents; install the api extra",
        ) from exc
    try:
        reader = PdfReader(str(path))
        return [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise DocumentParseError(
            "pdf-parse-failed", f"cannot read PDF {path.name}: {exc}"
        ) from exc


def parse_pdf_pages(spec: DocumentSpec) -> ParsedDocument:
    """Turn each PDF page with a text layer into one chunk.

    Blank pages produce no chunk but still occupy their newline in the
    transcript, so every ``char_base`` stays a transcript offset.  A PDF whose
    pages all lack text is a scanned document: it fails instead of silently
    falling back to OCR.
    """

    _require_source(spec)
    pages = _pdf_page_texts(spec.path)
    if not pages:
        raise DocumentParseError(
            "pdf-empty", f"{spec.path.name} contains no pages"
        )
    texts = [page.strip("\n") for page in pages]
    if not any(text.strip() for text in texts):
        raise DocumentParseError(
            "pdf-no-text-layer",
            f"{spec.path.name} has no extractable text layer; route scanned "
            "PDFs through the MinerU/Docling parser",
        )

    parts: list[str] = []
    offsets: list[int] = []
    cursor = 0
    for text in texts:
        offsets.append(cursor)
        parts.append(text)
        cursor += len(text) + 1
    transcript = "\n".join(parts)

    chunks = [
        SourceChunk(
            asset_id=spec.asset_id,
            chunk_id=f"{spec.asset_id}:p{index + 1}",
            text=text,
            page=index + 1,
            section_path=None,
            modality="pdf",
            char_base=offsets[index],
        )
        for index, text in enumerate(texts)
        if text.strip()
    ]
    return ParsedDocument(
        asset_id=spec.asset_id,
        modality="pdf",
        text=transcript,
        chunks=chunks,
        page_count=len(texts),
        parse_engine="legacy",
    )


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value).strip()


def _decode_csv(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentParseError(
        "table-encoding",
        f"{path.name} is neither UTF-8 nor GB18030 encoded",
    )


def _csv_rows(path: Path) -> list[list[str]]:
    return [
        [_cell_text(cell) for cell in row]
        for row in csv.reader(io.StringIO(_decode_csv(path), newline=""))
    ]


def _xlsx_rows(path: Path) -> list[tuple[str, list[list[str]]]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise DocumentParseError(
            "parser-dependency-missing",
            "openpyxl is required to parse XLSX documents; install the api extra",
        ) from exc
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise DocumentParseError(
            "table-parse-failed", f"cannot read workbook {path.name}: {exc}"
        ) from exc
    try:
        sheets: list[tuple[str, list[list[str]]]] = []
        for sheet in workbook.worksheets:
            rows = [
                [_cell_text(cell) for cell in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            sheets.append((sheet.title, rows))
        return sheets
    finally:
        workbook.close()


def _row_text(cells: list[str], header: list[str] | None) -> str:
    if header is None:
        return " | ".join(value for value in cells if value)
    parts: list[str] = []
    for index, value in enumerate(cells):
        if not value:
            continue
        label = header[index].strip() if index < len(header) else ""
        if not label:
            label = f"column{index + 1}"
        parts.append(f"{label}={value}")
    return " | ".join(parts)


def _table_records(
    rows: list[list[str]],
    *,
    header_row: bool,
    section_prefix: str,
    chunk_prefix: str,
) -> list[tuple[str, str, str]]:
    if not rows:
        return []
    header: list[str] | None = None
    start = 1
    if header_row:
        header = rows[0]
        start = 2
    records: list[tuple[str, str, str]] = []
    for row_number, cells in enumerate(rows[start - 1 :], start=start):
        if not any(cell.strip() for cell in cells):
            continue
        text = _row_text(cells, header)
        if not text:
            continue
        records.append(
            (
                f"{section_prefix}row{row_number}",
                f"{chunk_prefix}row{row_number}",
                text,
            )
        )
    return records


def parse_table_rows(spec: DocumentSpec) -> ParsedDocument:
    """Turn each non-empty table row into a ``header=value`` chunk.

    CSV uses the first row as the header by default; XLSX does the same per
    sheet.  Row numbers in chunk IDs and ``section_path`` are 1-based and refer
    to the original sheet, header included, so a human can locate the row.
    """

    _require_source(spec)
    suffix = spec.path.suffix.lower()
    if suffix == ".csv":
        records = _table_records(
            _csv_rows(spec.path),
            header_row=spec.header_row,
            section_prefix="",
            chunk_prefix="",
        )
        engine = "native"
    elif suffix in {".xlsx", ".xlsm"}:
        records = []
        for sheet_name, rows in _xlsx_rows(spec.path):
            records.extend(
                _table_records(
                    rows,
                    header_row=spec.header_row,
                    section_prefix=f"{sheet_name}!",
                    chunk_prefix=f"{sheet_name}:",
                )
            )
        engine = "native"
    else:
        raise DocumentParseError(
            "unsupported-table-format",
            f"{spec.path.name} is not a supported table format",
        )
    if not records:
        raise DocumentParseError(
            "table-empty", f"{spec.path.name} has no data rows"
        )

    parts = [text for _, _, text in records]
    transcript = "\n".join(parts)
    chunks: list[SourceChunk] = []
    cursor = 0
    for section_path, chunk_suffix, text in records:
        chunks.append(
            SourceChunk(
                asset_id=spec.asset_id,
                chunk_id=f"{spec.asset_id}:{chunk_suffix}",
                text=text,
                page=None,
                section_path=section_path,
                modality="table",
                char_base=cursor,
            )
        )
        cursor += len(text) + 1
    return ParsedDocument(
        asset_id=spec.asset_id,
        modality="table",
        text=transcript,
        chunks=chunks,
        page_count=1,
        parse_engine=engine,
    )


# ---------------------------------------------------------------------------
# Images and VLM
# ---------------------------------------------------------------------------


class ImageTranscriber(Protocol):
    def transcribe(self, spec: DocumentSpec) -> str:
        """Return the verbatim text visible in the image."""


def _is_local_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


def ensure_local_endpoint(
    endpoint: str, *, allow_external: bool = False
) -> None:
    """Refuse an endpoint that is not on the local machine or a private network.

    ``DOMAIN_ALLOW_EXTERNAL_VLM=1`` (or ``allow_external=True``) bypasses the
    check for deployments that have an approved external VLM.  A hostname is
    accepted only when every resolved address is local, so a DNS name cannot
    hide a public service behind a private-looking name.
    """

    if allow_external or os.environ.get(_ALLOW_EXTERNAL_ENV) == "1":
        return
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ExternalEndpointRefused(
            "vlm-endpoint-invalid", f"invalid VLM endpoint {endpoint!r}"
        )
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if _is_local_ip(address):
            return
        raise ExternalEndpointRefused(
            "vlm-endpoint-not-local",
            f"VLM endpoint {endpoint!r} is not local; set "
            f"{_ALLOW_EXTERNAL_ENV}=1 only for an approved external service",
        )

    try:
        infos = socket.getaddrinfo(
            host, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise ExternalEndpointRefused(
            "vlm-endpoint-unresolvable",
            f"cannot resolve VLM host {host!r}: {exc}",
        ) from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or any(
        not _is_local_ip(ipaddress.ip_address(item)) for item in addresses
    ):
        raise ExternalEndpointRefused(
            "vlm-endpoint-not-local",
            f"VLM host {host!r} resolves outside the local machine or a "
            "private network",
        )


def _chat_completions_url(endpoint: str) -> str:
    path = urllib.parse.urlparse(endpoint).path.rstrip("/")
    if path.endswith("/chat/completions"):
        return endpoint
    if path.endswith("/v1"):
        return endpoint.rstrip("/") + "/chat/completions"
    return endpoint.rstrip("/") + "/v1/chat/completions"


class FixtureVlm:
    """Deterministic transcript source for tests and offline acceptance."""

    def __init__(self, transcripts: dict[str, str]) -> None:
        self.transcripts = dict(transcripts)

    @classmethod
    def from_file(cls, path: str | Path) -> FixtureVlm:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VlmError(
                "vlm-fixture-invalid", f"cannot read VLM fixture {path}: {exc}"
            ) from exc
        transcripts = raw.get("transcripts") if isinstance(raw, dict) else None
        if not isinstance(transcripts, dict):
            raise VlmError(
                "vlm-fixture-invalid",
                f"{path} must contain a transcripts object",
            )
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in transcripts.items()
        ):
            raise VlmError(
                "vlm-fixture-invalid",
                f"{path} transcripts must map asset IDs to strings",
            )
        return cls(transcripts)

    def transcribe(self, spec: DocumentSpec) -> str:
        try:
            return self.transcripts[spec.asset_id]
        except KeyError as exc:
            raise VlmError(
                "vlm-fixture-missing",
                f"VLM fixture has no transcript for {spec.asset_id!r}",
            ) from exc


class OpenAICompatibleVlm:
    """Minimal OpenAI-compatible client for a local vision-language model."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
        allow_external: bool = False,
    ) -> None:
        ensure_local_endpoint(endpoint, allow_external=allow_external)
        self.endpoint = _chat_completions_url(endpoint)
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def transcribe(self, spec: DocumentSpec) -> str:
        if not spec.path.is_file():
            raise DocumentParseError(
                "source-missing", f"source image not found at {spec.path}"
            )
        data = spec.path.read_bytes()
        if len(data) > _MAX_IMAGE_BYTES:
            raise VlmError(
                "vlm-image-too-large",
                f"{spec.path.name} exceeds {_MAX_IMAGE_BYTES} bytes",
            )
        mime_type = (
            spec.mime_type
            or mimetypes.guess_type(spec.path.name)[0]
            or "image/png"
        )
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _IMAGE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{encoded}"
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise VlmError(
                "vlm-request-failed",
                f"VLM request to {self.endpoint} failed: {exc}",
            ) from exc
        try:
            parsed = json.loads(body)
            content = parsed["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise VlmError(
                "vlm-response-invalid",
                f"unexpected VLM response: {body[:300]}",
            ) from exc
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str):
            raise VlmError(
                "vlm-response-invalid", "VLM response content is not text"
            )
        return content


def parse_image(
    spec: DocumentSpec, *, vlm: ImageTranscriber | None
) -> ParsedDocument:
    """Transcribe an image locally and make the transcript one chunk."""

    _require_source(spec)
    if vlm is None:
        raise DocumentParseError(
            "vlm-not-configured",
            "image parsing needs a local VLM or a transcript fixture",
        )
    transcript = vlm.transcribe(spec)
    body = transcript.strip("\n")
    if not body.strip():
        raise VlmError(
            "vlm-empty-output",
            f"VLM returned no text for {spec.path.name}",
        )
    text = body + "\n"
    chunk = SourceChunk(
        asset_id=spec.asset_id,
        chunk_id=f"{spec.asset_id}:image",
        text=body,
        page=1,
        section_path="image",
        modality="image",
        char_base=0,
    )
    return ParsedDocument(
        asset_id=spec.asset_id,
        modality="image",
        text=text,
        chunks=[chunk],
        page_count=1,
        parse_engine="native",
    )


# ---------------------------------------------------------------------------
# Workspace assembly
# ---------------------------------------------------------------------------


def _require_source(spec: DocumentSpec) -> None:
    if not spec.path.is_file():
        raise DocumentParseError(
            "source-missing", f"source file not found at {spec.path}"
        )


def _parse_document(
    spec: DocumentSpec, *, vlm: ImageTranscriber | None
) -> ParsedDocument:
    if spec.modality == "pdf":
        return parse_pdf_pages(spec)
    if spec.modality == "table":
        return parse_table_rows(spec)
    if spec.modality == "image":
        return parse_image(spec, vlm=vlm)
    raise DocumentParseError(
        "unsupported-modality", f"unsupported modality {spec.modality!r}"
    )


def _reuse_image(
    manifest: MultimodalManifest,
    spec: DocumentSpec,
    previous: DocumentStatus,
) -> ParsedDocument:
    if not previous.text_uri:
        raise MultimodalError(
            "parsed-artifact-missing",
            f"{spec.asset_id} is marked parsed but has no text_uri",
        )
    path = (manifest.pack / previous.text_uri).resolve()
    try:
        path.relative_to(manifest.pack)
    except ValueError as exc:
        raise MultimodalError(
            "parsed-artifact-missing",
            f"text_uri {previous.text_uri!r} escapes the pack",
        ) from exc
    if not path.is_file():
        raise MultimodalError(
            "parsed-artifact-missing",
            f"parsed transcript not found at {path}",
        )
    text = path.read_text(encoding="utf-8")
    body = text.removesuffix("\n")
    chunk = SourceChunk(
        asset_id=spec.asset_id,
        chunk_id=f"{spec.asset_id}:image",
        text=body,
        page=1,
        section_path="image",
        modality="image",
        char_base=0,
    )
    return ParsedDocument(
        asset_id=spec.asset_id,
        modality="image",
        text=text,
        chunks=[chunk],
        page_count=1,
        parse_engine="native",
        text_uri=previous.text_uri,
    )


def _load_extractions(
    spec: DocumentSpec, chunks: list[SourceChunk]
) -> list[CandidateBatchInput]:
    chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    batches: list[CandidateBatchInput] = []
    seen: set[str] = set()
    for path in spec.extraction:
        if not path.is_file():
            raise ExtractionError(
                "extraction-missing", f"extraction file not found at {path}"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExtractionError(
                "extraction-invalid", f"cannot read {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ExtractionError(
                "extraction-invalid", f"{path} must be a JSON object"
            )
        chunk_id = str(raw.get("chunk_id") or "").strip()
        chunk = chunk_by_id.get(chunk_id)
        if chunk is None:
            raise ExtractionError(
                "extraction-chunk-mismatch",
                f"{path.name} references chunk {chunk_id!r}, which this "
                f"document did not produce",
            )
        if chunk_id in seen:
            raise ExtractionError(
                "extraction-duplicate",
                f"chunk {chunk_id!r} has more than one extraction file",
            )
        seen.add(chunk_id)
        try:
            batch = FaultCandidateBatch(
                chunk_id=chunk_id,
                chunk_text=chunk.text,
                asset_id=chunk.asset_id,
                entities=raw.get("entities") or [],
                relations=raw.get("relations") or [],
                warnings=list(raw.get("warnings") or []),
            )
        except Exception as exc:
            raise ExtractionError(
                "extraction-invalid",
                f"invalid extraction candidate in {path.name}: {exc}",
            ) from exc
        for payload in [*batch.entities, *batch.relations]:
            for candidate in payload.evidence:
                if not candidate.quote:
                    raise ExtractionError(
                        "quote-empty",
                        f"{path.name} has an empty evidence quote",
                    )
                if candidate.quote not in chunk.text:
                    raise ExtractionError(
                        "quote-not-verbatim",
                        f"{path.name}: quote {candidate.quote!r} is not in "
                        f"chunk {chunk_id!r}",
                    )
        batches.append(CandidateBatchInput(chunk=chunk, batch=batch))
    return batches


def build_multimodal_workspace(
    manifest: MultimodalManifest,
    *,
    vlm: ImageTranscriber | None = None,
    status_store: DocumentStatusStore | None = None,
    retry_failed: bool = False,
) -> MultimodalWorkspace:
    """Parse every manifest document and assemble a projection workspace.

    Per-document failures are recorded and skipped, never raised, so the
    caller can print every status before deciding to abort.  With
    ``retry_failed`` previously parsed images are reused from their transcript;
    PDF and table documents are re-parsed locally because that costs no network
    call and guarantees chunks match the transcript on disk.
    """

    store = status_store or DocumentStatusStore(
        manifest.pack / "storage" / "multimodal_status.json"
    )
    previous = store.load()
    assets: list[EntityRecord] = []
    chunks: list[SourceChunk] = []
    batches: list[CandidateBatchInput] = []
    statuses: list[DocumentStatus] = []

    for spec in manifest.documents:
        prior = previous.get(spec.asset_id)
        reuse = bool(
            retry_failed
            and prior is not None
            and prior.status == "parsed"
            and spec.modality == "image"
        )
        attempts = (prior.attempts if prior else 0) + (0 if reuse else 1)
        try:
            if reuse:
                assert prior is not None
                parsed = _reuse_image(manifest, spec, prior)
            else:
                parsed = _parse_document(spec, vlm=vlm)
            text_uri = parsed.text_uri or _write_text_artifact(
                manifest.pack,
                manifest.text_dir,
                spec.asset_id,
                parsed.text,
            )
            document_batches = _load_extractions(spec, parsed.chunks)
        except MultimodalError as exc:
            code, message = exc.code, exc.message
        except (SeedInputError, OSError) as exc:
            code, message = "document-io-failed", str(exc)
        else:
            code, message = "", ""
        if code:
            status = DocumentStatus(
                asset_id=spec.asset_id,
                status="failed",
                attempts=attempts,
                error_code=code,
                error_message=message,
                updated_at=_now_iso(),
            )
            store.record(status)
            statuses.append(status)
            continue

        asset = build_file_asset(
            pack=manifest.pack,
            spec={
                "asset_uid": spec.asset_id,
                "path": spec.relative_path,
                "canonical_name": spec.canonical_name,
                "description": spec.description,
                "modality": spec.modality,
                "mime_type": spec.mime_type,
                "language": spec.language,
                "text_uri": text_uri,
                "parse_engine": parsed.parse_engine,
                "parse_status": "parsed",
                "page_count": parsed.page_count,
                "chunk_count": len(parsed.chunks),
                "source_system": "multimodal",
                "last_parsed_at": _now_iso(),
            },
            tenant_id=manifest.tenant_id,
            ontology_version=manifest.ontology_version,
            schema_version=manifest.schema_version,
            created_by=manifest.created_by,
        )
        status = DocumentStatus(
            asset_id=spec.asset_id,
            status="parsed",
            attempts=attempts,
            page_count=parsed.page_count,
            chunk_count=len(parsed.chunks),
            text_uri=text_uri,
            updated_at=_now_iso(),
        )
        if reuse:
            status.updated_at = prior.updated_at if prior else status.updated_at
        else:
            store.record(status)
        statuses.append(status)
        assets.append(asset)
        chunks.extend(parsed.chunks)
        batches.extend(document_batches)

    context = {
        "tenant_id": manifest.tenant_id,
        "ontology_version": manifest.ontology_version,
        "schema_version": manifest.schema_version,
        "top_event_uid": manifest.top_event_uid,
        "draft_id": manifest.draft_id,
        "created_by": manifest.created_by,
        "created_at": manifest.created_at.isoformat(),
        "review_status": manifest.review_status,
        "bundle_version": manifest.bundle_version,
        "extraction_run_id": manifest.extraction_run_id,
        "tree_version": manifest.tree_version,
    }
    return MultimodalWorkspace(
        workspace=SeedWorkspace(
            context=context,
            assets=assets,
            chunks=chunks,
            batches=batches,
        ),
        statuses=statuses,
    )


__all__ = [
    "DocumentParseError",
    "DocumentSpec",
    "DocumentStatus",
    "DocumentStatusStore",
    "ExternalEndpointRefused",
    "ExtractionError",
    "FixtureVlm",
    "ImageTranscriber",
    "ManifestError",
    "MultimodalError",
    "MultimodalManifest",
    "MultimodalWorkspace",
    "OpenAICompatibleVlm",
    "ParsedDocument",
    "StatusStoreError",
    "VlmError",
    "build_multimodal_workspace",
    "ensure_local_endpoint",
    "load_manifest",
    "parse_image",
    "parse_pdf_pages",
    "parse_table_rows",
]
