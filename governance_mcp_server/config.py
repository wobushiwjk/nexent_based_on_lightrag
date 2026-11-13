"""Configuration for the read-only ontology governance MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"
DEFAULT_ONTOLOGY = DEFAULT_PACK / "ontology.yml"
DEFAULT_ROOT = DEFAULT_PACK / "storage" / "governance"


@dataclass(frozen=True)
class GovernanceServerConfig:
    """Paths and transport settings for one governance server."""

    governance_root: Path
    ontology_path: Path
    server_name: str
    log_level: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "GovernanceServerConfig":
        root = _resolve(os.environ.get("ONTOLOGY_GOVERNANCE_ROOT", str(DEFAULT_ROOT)))
        ontology = _resolve(
            os.environ.get("ONTOLOGY_GOVERNANCE_SOURCE", str(DEFAULT_ONTOLOGY))
        )
        log_level = (
            os.environ.get("ONTOLOGY_GOVERNANCE_LOG_LEVEL", "INFO").strip().upper()
        )
        host = (
            os.environ.get("ONTOLOGY_GOVERNANCE_HOST", "127.0.0.1").strip()
            or "127.0.0.1"
        )
        port_raw = os.environ.get("ONTOLOGY_GOVERNANCE_PORT", "8766").strip() or "8766"
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ValueError(
                f"ONTOLOGY_GOVERNANCE_PORT must be an integer, got {port_raw!r}"
            ) from exc
        if port < 1 or port > 65535:
            raise ValueError("ONTOLOGY_GOVERNANCE_PORT must be between 1 and 65535")
        return cls(
            governance_root=root,
            ontology_path=ontology,
            server_name=os.environ.get(
                "ONTOLOGY_GOVERNANCE_SERVER_NAME",
                "ontology-governance",
            ),
            log_level=log_level,
            host=host,
            port=port,
        )


def _resolve(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


__all__ = [
    "DEFAULT_ONTOLOGY",
    "DEFAULT_PACK",
    "DEFAULT_ROOT",
    "GovernanceServerConfig",
    "REPO_ROOT",
]
