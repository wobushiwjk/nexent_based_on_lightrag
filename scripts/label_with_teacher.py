"""Label parsed chunks with the external teacher model.

Only chunks whose ``sources.yml`` entry has ``public: true`` are ever sent.
Everything else is written to ``teacher_blocked.jsonl`` and never leaves the
machine.  Teacher output is parsed and narrowed locally; invalid output goes
to ``teacher_failures.jsonl`` for review, not into the training set.

Run from the repository root::

    cp training/.env.example training/.env   # fill endpoint/key
    python scripts/label_with_teacher.py --dry-run
    python scripts/label_with_teacher.py

``--chunks`` is repeatable and merges the manifests, which is how the CFDK
chunks and the manual chunks are labelled in one pass.  ``--skip-label-file``
is also repeatable: every chunk ID already covered by one of those label files
is left alone, so the bootstrap labels are not paid for twice.  ``--overwrite``
re-labels everything and ignores both the existing teacher output and the
skip files.

Outputs (git-ignored):

``training/data/teacher_labels.jsonl``
    Validated ``FaultEvent + CAUSES`` targets, one row per chunk.
``training/data/teacher_failures.jsonl``
    Raw teacher output that could not be normalized.
``training/data/teacher_blocked.jsonl``
    Chunks refused by the public-source gate.
``training/data/teacher_export_log.jsonl``
    One audit row per request: model, hashes, usage, timestamp.

The run is resumable: existing ``teacher_labels.jsonl`` chunk IDs are skipped
unless ``--overwrite`` is given.  Do not run two labelers against the same
data directory at once.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.distillation.dataset import (  # noqa: E402
    ChunkRecord,
    DatasetBuildError,
    load_chunks,
)
from lightrag.fault_tree.distillation.prompts import (  # noqa: E402
    build_system_prompt,
    build_user_prompt,
    load_prompt_profile,
)
from lightrag.fault_tree.distillation.teacher import (  # noqa: E402
    TeacherCall,
    TeacherClient,
    TeacherConfig,
    TeacherError,
    extract_json_object,
    load_env_file,
    normalize_teacher_target,
)

DEFAULT_CHUNKS = ROOT / "training" / "corpus" / "parsed" / "chunks.jsonl"
DEFAULT_SOURCES = ROOT / "training" / "corpus" / "sources.yml"
DEFAULT_PROMPT_PROFILE = ROOT / "prompts" / "entity_type" / "industrial_fault.yml"
DEFAULT_ENV_FILE = ROOT / "training" / ".env"
DEFAULT_DATA_DIR = ROOT / "training" / "data"


class LabelError(RuntimeError):
    """The labelling run cannot start or continue."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_sources(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LabelError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LabelError(f"{path} must contain a YAML mapping")
    sources: dict[str, dict[str, Any]] = {}
    for entry in payload.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        source_id = str(entry.get("id") or "").strip()
        if source_id:
            sources[source_id] = entry
    if not sources:
        raise LabelError(f"{path} defines no sources")
    return sources


def source_is_public(entry: dict[str, Any] | None) -> bool:
    """The gate is strict: only a YAML boolean ``true`` allows export."""
    return bool(entry) and entry.get("public") is True


def load_chunk_files(paths: list[Path]) -> dict[str, ChunkRecord]:
    """Merge chunk manifests; a chunk ID may appear in only one of them."""
    merged: dict[str, ChunkRecord] = {}
    origin: dict[str, Path] = {}
    for path in paths:
        for chunk_id, chunk in load_chunks(path).items():
            if chunk_id in merged:
                raise LabelError(
                    f"chunk ID {chunk_id!r} appears in both "
                    f"{origin[chunk_id]} and {path}"
                )
            merged[chunk_id] = chunk
            origin[chunk_id] = path
    return merged


def _read_existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LabelError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        chunk_id = str(row.get("chunk_id") or "").strip()
        if chunk_id:
            ids.add(chunk_id)
    return ids


def _select_chunks(
    chunks: dict[str, ChunkRecord],
    *,
    chunk_ids: list[str],
    max_chunks: int,
    skip_ids: set[str],
) -> list[ChunkRecord]:
    selected = list(chunks.values())
    if chunk_ids:
        missing = [item for item in chunk_ids if item not in chunks]
        if missing:
            raise LabelError(f"unknown chunk IDs: {missing}")
        wanted = set(chunk_ids)
        selected = [chunk for chunk in selected if chunk.chunk_id in wanted]
    if skip_ids:
        selected = [
            chunk for chunk in selected if chunk.chunk_id not in skip_ids
        ]
    if max_chunks > 0:
        selected = selected[:max_chunks]
    return selected


def _write_jsonl_row(handle: Any, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    handle.flush()


class _RunStats:
    def __init__(self) -> None:
        self.labeled = 0
        self.failed = 0
        self.blocked = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "labeled": self.labeled,
            "failed": self.failed,
            "blocked": self.blocked,
        }


async def _label_one(
    chunk: ChunkRecord,
    *,
    config: TeacherConfig,
    client: TeacherClient,
    sources: dict[str, dict[str, Any]],
    system_prompt: str,
    locks: asyncio.Lock,
    files: dict[str, Any],
    stats: _RunStats,
    max_raw_chars: int,
) -> None:
    source_id = chunk.source_id
    if not source_is_public(sources.get(source_id)):
        async with locks:
            _write_jsonl_row(
                files["blocked"],
                {
                    "chunk_id": chunk.chunk_id,
                    "asset_id": chunk.asset_id,
                    "source_id": source_id,
                    "reason": "source-not-public",
                    "timestamp": _now(),
                },
            )
            stats.blocked += 1
        return

    user_prompt = build_user_prompt(
        {
            "chunk_id": chunk.chunk_id,
            "asset_id": chunk.asset_id,
            "text": chunk.text,
            "page": chunk.page,
            "section_path": chunk.section_path,
            "modality": chunk.modality,
        }
    )
    call: TeacherCall | None = None
    try:
        call = await client.label(system_prompt, user_prompt)
        raw = extract_json_object(call.content)
        target, warnings = normalize_teacher_target(raw, chunk.text)
    except TeacherError as exc:
        async with locks:
            _write_jsonl_row(
                files["failures"],
                {
                    "chunk_id": chunk.chunk_id,
                    "asset_id": chunk.asset_id,
                    "source_id": source_id,
                    "error": str(exc),
                    "raw_content": (
                        call.content[:max_raw_chars]
                        if call is not None
                        else ""
                    ),
                    "model": config.model,
                    "timestamp": _now(),
                },
            )
            stats.failed += 1
        return

    assert call is not None
    async with locks:
        _write_jsonl_row(
            files["labels"],
            {
                "chunk_id": chunk.chunk_id,
                "asset_id": chunk.asset_id,
                "source_id": source_id,
                "origin": "teacher",
                "target": target,
                "warnings": warnings,
                "model": config.model,
                "prompt_sha256": call.prompt_sha256,
                "labeled_at": _now(),
            },
        )
        _write_jsonl_row(
            files["export_log"],
            {
                "chunk_id": chunk.chunk_id,
                "asset_id": chunk.asset_id,
                "source_id": source_id,
                "source_license": sources[source_id].get("license"),
                "chunk_text_sha256": hashlib.sha256(
                    chunk.text.encode("utf-8")
                ).hexdigest(),
                "model": config.model,
                "json_mode": config.json_mode,
                "prompt_sha256": call.prompt_sha256,
                "response_sha256": call.response_sha256,
                "usage": call.usage,
                "timestamp": _now(),
            },
        )
        stats.labeled += 1


async def _run(
    selected: list[ChunkRecord],
    *,
    config: TeacherConfig,
    sources: dict[str, dict[str, Any]],
    system_prompt: str,
    data_dir: Path,
    max_raw_chars: int,
) -> _RunStats:
    data_dir.mkdir(parents=True, exist_ok=True)
    stats = _RunStats()
    lock = asyncio.Lock()
    paths = {
        "labels": data_dir / "teacher_labels.jsonl",
        "failures": data_dir / "teacher_failures.jsonl",
        "blocked": data_dir / "teacher_blocked.jsonl",
        "export_log": data_dir / "teacher_export_log.jsonl",
    }
    client = TeacherClient(config)
    handles = {
        name: path.open("a", encoding="utf-8", newline="\n")
        for name, path in paths.items()
    }
    try:
        results = await asyncio.gather(
            *(
                _label_one(
                    chunk,
                    config=config,
                    client=client,
                    sources=sources,
                    system_prompt=system_prompt,
                    locks=lock,
                    files=handles,
                    stats=stats,
                    max_raw_chars=max_raw_chars,
                )
                for chunk in selected
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                stats.failed += 1
                print(
                    f"[teacher] unexpected task failure: {result}",
                    file=sys.stderr,
                )
    finally:
        for handle in handles.values():
            handle.close()
        await client.close()
    return stats


def _dry_run_plan(
    *,
    chunks: dict[str, ChunkRecord],
    selected: list[ChunkRecord],
    existing: set[str],
    bootstrap: set[str],
    sources: dict[str, dict[str, Any]],
    data_dir: Path,
    model: str,
    json_mode: bool,
) -> dict[str, Any]:
    public = [
        chunk.chunk_id
        for chunk in selected
        if source_is_public(sources.get(chunk.source_id))
    ]
    blocked = [
        chunk.chunk_id
        for chunk in selected
        if not source_is_public(sources.get(chunk.source_id))
    ]
    plan = {
        "generated_at": _now(),
        "model": model,
        "json_mode": json_mode,
        "chunks_total": len(chunks),
        "selected": len(selected),
        "existing_labels": len(existing),
        "skipped_by_label_file": len(bootstrap),
        "to_label": len(public),
        "blocked": len(blocked),
        "public_chunk_ids": public,
        "blocked_chunk_ids": blocked,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "teacher_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunks",
        action="append",
        type=Path,
        default=None,
        help="Chunk manifest to label; repeatable, manifests are merged.",
    )
    parser.add_argument(
        "--skip-label-file",
        action="append",
        type=Path,
        default=[],
        help="Skip chunk IDs already present in this label file; repeatable.",
    )
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument(
        "--prompt-profile", type=Path, default=DEFAULT_PROMPT_PROFILE
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--chunk-id",
        action="append",
        default=[],
        help="Label only this chunk ID; repeatable.",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=0,
        help="Stop after N selected chunks; 0 means all.",
    )
    parser.add_argument("--max-raw-chars", type=int, default=20000)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-json-mode", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        env = {}
        env_path = args.env_file.resolve()
        if env_path.exists():
            env.update(load_env_file(env_path))
        env.update(os.environ)
        chunk_paths = [
            path.resolve() for path in (args.chunks or [DEFAULT_CHUNKS])
        ]
        chunks = load_chunk_files(chunk_paths)
        sources = load_sources(args.sources.resolve())
        system_prompt = build_system_prompt(
            load_prompt_profile(args.prompt_profile.resolve())
        )
        data_dir = args.out_dir.resolve()
        existing = _read_existing_ids(
            data_dir / "teacher_labels.jsonl"
        )
        bootstrap: set[str] = set()
        for skip_path in args.skip_label_file:
            bootstrap |= _read_existing_ids(skip_path.resolve())
        env_max_chunks = int(env.get("W7_TEACHER_MAX_CHUNKS") or 0)
        max_chunks = (
            args.max_chunks if args.max_chunks > 0 else max(0, env_max_chunks)
        )
        selected = _select_chunks(
            chunks,
            chunk_ids=args.chunk_id,
            max_chunks=max_chunks,
            skip_ids=set() if args.overwrite else existing | bootstrap,
        )
        model = str(env.get("W7_TEACHER_MODEL") or "deepseek-flash").strip()
        json_mode = not args.no_json_mode and str(
            env.get("W7_TEACHER_JSON_MODE") or "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if args.dry_run:
            plan = _dry_run_plan(
                chunks=chunks,
                selected=selected,
                existing=existing,
                bootstrap=bootstrap,
                sources=sources,
                data_dir=data_dir,
                model=model,
                json_mode=json_mode,
            )
            print(
                f"[teacher] dry run: {plan['to_label']} public / "
                f"{plan['blocked']} blocked / {plan['existing_labels']} "
                f"already labelled"
            )
            print(f"[teacher] plan -> {data_dir / 'teacher_plan.json'}")
            return 0
        config = TeacherConfig.from_env(env)
        if args.no_json_mode:
            config = replace(config, json_mode=False)
    except (LabelError, DatasetBuildError, TeacherError, ValueError) as exc:
        print(f"[teacher] {exc}", file=sys.stderr)
        return 2

    if not selected:
        print("[teacher] nothing to label")
        return 0

    stats = asyncio.run(
        _run(
            selected,
            config=config,
            sources=sources,
            system_prompt=system_prompt,
            data_dir=data_dir,
            max_raw_chars=max(0, args.max_raw_chars),
        )
    )
    counts = stats.as_dict()
    print(
        f"[teacher] labeled {counts['labeled']}, failed {counts['failed']}, "
        f"blocked {counts['blocked']}"
    )
    print(f"[teacher] outputs -> {data_dir}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
