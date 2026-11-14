"""Build the W7 ShareGPT training/validation/test files.

Inputs are one or more parsed chunk manifests and one or more label files.
Chunk manifests are concatenated (ids must be globally unique), labels are
merged in the order given (later wins), validated against the chunk text, and
split by document (``asset_id``) with a fixed seed.  The output is the
LLaMA-Factory ShareGPT format plus ``val_meta.jsonl`` / ``test_meta.jsonl``
for evaluation and a ``manifest.json`` recording the exact split.

Run from the repository root::

    python scripts/build_distillation_dataset.py \
        --chunks training/corpus/parsed/cfdk_chunks.jsonl \
        --chunks training/corpus/parsed/manual_chunks.jsonl \
        --labels cfdk=training/data/cfdk_bootstrap_labels.jsonl \
        --labels manual=training/data/manual_bootstrap_labels.jsonl \
        --force-split tianjin-water-standard=val \
        --force-split grundfos-6859100=test \
        --out-dir training/data/w7_b

The stage-2 mix continues from the stage-1 adapter with the target domain
weighted up and the cross-domain corpus capped to a replay budget::

    python scripts/build_distillation_dataset.py ... \
        --repeat-source grundfos-nk-6859942=5 \
        --repeat-source grundfos-s-5564721=5 \
        --cap-source cfdk-compressor-maintenance=1200 \
        --out-dir training/data/w7_b_stage2

``--repeat-source`` copies train rows; ``--cap-source`` samples train rows
with ``--seed``.  Val and test are never rebalanced.
``--pin-from-manifest`` reuses an earlier manifest's asset assignment, so a
CFDK-only baseline evaluates on the same held-out documents as the main
experiment; assets absent from the current chunk files are ignored.

Default label order (lowest to highest priority):

    training/data/public_labels.jsonl
    training/data/teacher_labels.jsonl
    training/data/reviewed_labels.jsonl

A missing default file is skipped; at least one label file must exist.  Use
``--labels origin=path`` to override the list.  ``--strict`` aborts on the
first invalid label instead of recording it and continuing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.distillation.dataset import (  # noqa: E402
    DatasetBuildError,
    merge_chunk_files,
    merge_labels,
    rebalance_train,
    split_by_asset,
    write_dataset,
)
from lightrag.fault_tree.distillation.prompts import (  # noqa: E402
    build_system_prompt,
    load_prompt_profile,
)

DEFAULT_CHUNKS = ROOT / "training" / "corpus" / "parsed" / "chunks.jsonl"
DEFAULT_DATA_DIR = ROOT / "training" / "data"
DEFAULT_PROMPT_PROFILE = ROOT / "prompts" / "entity_type" / "industrial_fault.yml"

_DEFAULT_LABEL_NAMES = (
    ("public", "public_labels.jsonl"),
    ("teacher", "teacher_labels.jsonl"),
    ("reviewed", "reviewed_labels.jsonl"),
)


def _origin_from_path(path: Path) -> str:
    stem = path.stem
    for name in ("public", "teacher", "reviewed"):
        if stem.startswith(f"{name}_"):
            return name
    return stem


def _parse_label_args(
    raw: list[str], data_dir: Path
) -> list[tuple[Path, str]]:
    if not raw:
        result: list[tuple[Path, str]] = []
        for origin, name in _DEFAULT_LABEL_NAMES:
            path = data_dir / name
            if path.exists():
                result.append((path, origin))
        return result
    result = []
    for item in raw:
        if "=" in item:
            origin, raw_path = item.split("=", 1)
            origin = origin.strip()
            path = Path(raw_path).expanduser()
        else:
            path = Path(item).expanduser()
            origin = _origin_from_path(path)
        if not origin:
            raise DatasetBuildError(f"empty label origin for {item}")
        result.append((path.resolve(), origin))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunks",
        type=Path,
        action="append",
        default=[],
        help="Chunk manifest JSONL. Repeatable; defaults to chunks.jsonl.",
    )
    parser.add_argument(
        "--labels",
        action="append",
        default=[],
        help="Label JSONL, optionally as origin=path. Repeatable.",
    )
    parser.add_argument(
        "--prompt-profile", type=Path, default=DEFAULT_PROMPT_PROFILE
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--test-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--force-split",
        action="append",
        default=[],
        metavar="ASSET=SPLIT",
        help=(
            "Pin an asset to train/val/test before the random fill; "
            "repeatable. Use it to hold out a target-domain manual."
        ),
    )
    parser.add_argument(
        "--repeat-source",
        action="append",
        default=[],
        metavar="SOURCE=N",
        help=(
            "Repeat that source's train rows N times; repeatable. Use it to "
            "weight a small target-domain corpus. Val/test are untouched."
        ),
    )
    parser.add_argument(
        "--cap-source",
        action="append",
        default=[],
        metavar="SOURCE=N",
        help=(
            "Keep at most N train rows from that source, sampled with --seed; "
            "repeatable. Use it to budget cross-domain replay."
        ),
    )
    parser.add_argument(
        "--pin-from-manifest",
        type=Path,
        default=None,
        help=(
            "Reuse the asset->split assignment recorded in an earlier "
            "manifest. Assets absent from the current chunk files are "
            "ignored, so a CFDK-only baseline can share the main "
            "experiment's evaluation documents."
        ),
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    forced_splits: dict[str, str] = {}
    for item in args.force_split:
        if "=" not in item:
            parser.error(f"--force-split expects ASSET=SPLIT, got {item!r}")
        asset_id, split_name = item.rsplit("=", 1)
        asset_id = asset_id.strip()
        split_name = split_name.strip().lower()
        if not asset_id or split_name not in {"train", "val", "test"}:
            parser.error(f"invalid --force-split {item!r}")
        if asset_id in forced_splits:
            parser.error(f"duplicate --force-split for {asset_id}")
        forced_splits[asset_id] = split_name

    def _parse_mix_args(raw: list[str], flag: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in raw:
            if "=" not in item:
                parser.error(f"{flag} expects SOURCE=N, got {item!r}")
            source, raw_count = item.rsplit("=", 1)
            source = source.strip()
            try:
                count = int(raw_count)
            except ValueError:
                parser.error(f"{flag} expects an integer N, got {item!r}")
            if not source or count < 1:
                parser.error(f"invalid {flag} {item!r}")
            if source in counts:
                parser.error(f"duplicate {flag} for {source}")
            counts[source] = count
        return counts

    pinned: dict[str, str] = {}
    if args.pin_from_manifest is not None:
        try:
            previous = json.loads(
                args.pin_from_manifest.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"[build] cannot read --pin-from-manifest: {exc}",
                file=sys.stderr,
            )
            return 2
        assets = previous.get("assets")
        if not isinstance(assets, dict):
            print(
                "[build] --pin-from-manifest has no 'assets' object",
                file=sys.stderr,
            )
            return 2
        for split_name, names in assets.items():
            if split_name not in {"train", "val", "test"} or not isinstance(
                names, list
            ):
                continue
            for name in names:
                pinned[str(name)] = split_name

    repeats = _parse_mix_args(args.repeat_source, "--repeat-source")
    caps = _parse_mix_args(args.cap_source, "--cap-source")
    overlap = sorted(set(repeats) & set(caps))
    if overlap:
        parser.error(f"sources cannot be repeated and capped: {overlap}")
    train_mix = (
        {"repeats": repeats, "caps": caps, "seed": args.seed}
        if repeats or caps
        else {}
    )

    chunk_paths = [
        path.resolve() for path in (args.chunks or [DEFAULT_CHUNKS])
    ]
    data_dir = args.out_dir.resolve()
    try:
        label_files = _parse_label_args(args.labels, data_dir)
    except DatasetBuildError as exc:
        print(f"[build] {exc}", file=sys.stderr)
        return 2
    missing = [str(path) for path, _ in label_files if not path.exists()]
    if missing:
        print(f"[build] missing label files: {missing}", file=sys.stderr)
        return 2
    missing_chunks = [str(path) for path in chunk_paths if not path.exists()]
    if missing_chunks:
        print(f"[build] missing chunk files: {missing_chunks}", file=sys.stderr)
        return 2
    if not label_files:
        print(
            "[build] no label files found; run "
            "scripts/import_public_fault_dataset.py first",
            file=sys.stderr,
        )
        return 2

    try:
        chunks = merge_chunk_files(chunk_paths)
        present_assets = {chunk.asset_id for chunk in chunks.values()}
        pinned_present = {
            asset: split_name
            for asset, split_name in pinned.items()
            if asset in present_assets
        }
        conflicts = sorted(
            asset
            for asset in pinned_present
            if asset in forced_splits
            and forced_splits[asset] != pinned_present[asset]
        )
        if conflicts:
            raise DatasetBuildError(
                f"--force-split conflicts with --pin-from-manifest: {conflicts}"
            )
        effective_splits = {**pinned_present, **forced_splits}
        try:
            system_prompt = build_system_prompt(
                load_prompt_profile(args.prompt_profile.resolve())
            )
        except ValueError as exc:
            raise DatasetBuildError(str(exc)) from exc
        records, errors = merge_labels(
            label_files, chunks, strict=args.strict
        )
        if not records:
            raise DatasetBuildError("no valid labels after validation")
        split = split_by_asset(
            records,
            chunks,
            val_ratio=args.val_ratio,
            test_ratio=args.test_ratio,
            seed=args.seed,
            forced_splits=effective_splits,
        )
        if train_mix:
            split = rebalance_train(
                split,
                chunks,
                repeats=repeats,
                caps=caps,
                seed=args.seed,
            )
        manifest = write_dataset(
            split,
            chunks,
            system_prompt,
            data_dir,
            seed=args.seed,
            val_ratio=args.val_ratio,
            test_ratio=args.test_ratio,
            label_files=label_files,
            errors=errors,
            chunk_files=chunk_paths,
            forced_splits=forced_splits,
            train_mix=train_mix,
            pinned_from=(
                str(args.pin_from_manifest.resolve())
                if args.pin_from_manifest
                else None
            ),
        )
    except DatasetBuildError as exc:
        print(f"[build] {exc}", file=sys.stderr)
        return 2

    stats = manifest["stats"]
    print(
        f"[build] {stats['train']} train / {stats['val']} val / "
        f"{stats['test']} test, {stats['entities']} entities, "
        f"{stats['relations']} relations, {stats['negatives']} negatives"
    )
    print(
        f"[build] assets: {len(split.assets['train'])} train / "
        f"{len(split.assets['val'])} val / {len(split.assets['test'])} test"
    )
    by_source: dict[str, int] = {}
    for record in split.train:
        chunk = chunks[record.chunk_id]
        source = chunk.source_id or chunk.asset_id
        by_source[source] = by_source.get(source, 0) + 1
    if by_source:
        mix = ", ".join(
            f"{source}={count}"
            for source, count in sorted(
                by_source.items(), key=lambda item: (-item[1], item[0])
            )
        )
        print(f"[build] train rows by source: {mix}")
    print(f"[build] wrote {data_dir}")
    print(
        f"[build] system prompt sha256 "
        f"{manifest['system_prompt_sha256'][:12]}"
    )
    if errors:
        print(
            f"[build] {len(errors)} invalid labels skipped; see manifest.json",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
