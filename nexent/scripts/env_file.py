"""Load the optional Nexent shell-style environment file without dependencies."""

from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / "nexent" / ".env"


def load_nexent_env(
    path: str | Path | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Load ``KEY=value`` entries, preserving variables already in the process."""

    source = Path(path) if path is not None else DEFAULT_ENV_FILE
    target = environ if environ is not None else os.environ
    if not source.is_file():
        return []

    loaded: list[str] = []
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in target:
            continue
        target[key] = value
        loaded.append(key)
    return loaded


__all__ = ["DEFAULT_ENV_FILE", "REPO_ROOT", "load_nexent_env"]
