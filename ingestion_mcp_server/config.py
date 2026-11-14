"""Configuration for the upload ingestion MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"
DEFAULT_STORAGE = DEFAULT_PACK / "storage"
DEFAULT_UPLOAD_DIRS = (
    REPO_ROOT / "uploads" / "inbox",
    Path("/mnt/nexent/workdir"),
    Path("/var/lib/docker/volumes/nexent-agent-workspace/_data"),
)


@dataclass(frozen=True)
class IngestionServerConfig:
    """Paths, model endpoint and transport settings."""

    pack_dir: Path
    storage_dir: Path
    upload_dirs: tuple[Path, ...]
    student_endpoint: str
    student_model: str
    student_api_key: str
    student_timeout: float
    student_max_output_tokens: int
    student_max_attempts: int
    vlm_endpoint: str
    vlm_model: str
    vlm_api_key: str
    server_name: str
    log_level: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "IngestionServerConfig":
        uploads_raw = os.environ.get("INGESTION_UPLOAD_DIRS", "").strip()
        upload_dirs = (
            tuple(_resolve(item) for item in uploads_raw.split(os.pathsep) if item)
            if uploads_raw
            else tuple(path.resolve() for path in DEFAULT_UPLOAD_DIRS)
        )
        try:
            student_timeout = float(os.environ.get("W7_STUDENT_TIMEOUT") or 120)
            student_max_output_tokens = int(
                os.environ.get("W7_STUDENT_MAX_OUTPUT_TOKENS") or 1536
            )
            student_max_attempts = int(os.environ.get("W7_STUDENT_MAX_ATTEMPTS") or 2)
        except ValueError as exc:
            raise ValueError(f"invalid W7 student numeric setting: {exc}") from exc
        port = int(os.environ.get("INGESTION_MCP_PORT") or 8767)
        if port < 1 or port > 65535:
            raise ValueError("INGESTION_MCP_PORT must be between 1 and 65535")
        return cls(
            pack_dir=_resolve(
                os.environ.get("INGESTION_DOMAIN_PACK", str(DEFAULT_PACK))
            ),
            storage_dir=_resolve(
                os.environ.get("INGESTION_DOMAIN_STORAGE", str(DEFAULT_STORAGE))
            ),
            upload_dirs=upload_dirs,
            student_endpoint=os.environ.get("W7_STUDENT_ENDPOINT", "").strip(),
            student_model=os.environ.get("W7_STUDENT_MODEL", "").strip(),
            student_api_key=os.environ.get("W7_STUDENT_API_KEY", "").strip(),
            student_timeout=student_timeout,
            student_max_output_tokens=student_max_output_tokens,
            student_max_attempts=max(1, student_max_attempts),
            vlm_endpoint=os.environ.get("DOMAIN_VLM_ENDPOINT", "").strip(),
            vlm_model=os.environ.get("DOMAIN_VLM_MODEL", "").strip(),
            vlm_api_key=os.environ.get("DOMAIN_VLM_API_KEY", "").strip(),
            server_name=os.environ.get(
                "INGESTION_MCP_SERVER_NAME", "industrial-fault-ingestion"
            ),
            log_level=os.environ.get("INGESTION_MCP_LOG_LEVEL", "INFO").upper(),
            host=os.environ.get("INGESTION_MCP_HOST", "127.0.0.1"),
            port=port,
        )


def _resolve(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


__all__ = [
    "DEFAULT_PACK",
    "DEFAULT_STORAGE",
    "DEFAULT_UPLOAD_DIRS",
    "IngestionServerConfig",
    "REPO_ROOT",
]
