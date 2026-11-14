"""CLI-level tests for the W7 dataset builder's split and mix options."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_distillation_dataset_under_test",
        ROOT / "scripts" / "build_distillation_dataset.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


builder = _load_builder()


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _corpus(tmp_path: Path, assets: list[str]) -> tuple[Path, Path]:
    chunks = []
    labels = []
    for index, asset in enumerate(assets):
        chunk_id = f"chunk-{index}"
        chunks.append(
            {
                "chunk_id": chunk_id,
                "asset_id": asset,
                "text": "过滤器堵塞导致压力低。",
                "source_id": "manual" if asset in {"A", "C"} else "cfdk",
            }
        )
        labels.append(
            {
                "chunk_id": chunk_id,
                "target": {
                    "entities": [
                        {
                            "local_id": "e",
                            "class": "FaultEvent",
                            "name": "过滤器堵塞",
                            "evidence": [{"quote": "过滤器堵塞"}],
                        }
                    ],
                    "relations": [],
                },
            }
        )
    return (
        _write_jsonl(tmp_path / "chunks.jsonl", chunks),
        _write_jsonl(tmp_path / "labels.jsonl", labels),
    )


def _manifest(path: Path) -> dict:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def test_pin_from_manifest_reuses_split_and_ignores_missing_assets(
    tmp_path: Path,
) -> None:
    first_chunks, first_labels = _corpus(tmp_path, ["A", "B", "C", "D"])
    first_out = tmp_path / "first"
    assert (
        builder.main(
            [
                "--chunks",
                str(first_chunks),
                "--labels",
                f"public={first_labels}",
                "--force-split",
                "A=val",
                "--force-split",
                "B=test",
                "--out-dir",
                str(first_out),
            ]
        )
        == 0
    )
    assert _manifest(first_out)["assets"]["train"] == ["C", "D"]

    # The second experiment has no asset D; the pin must ignore it rather
    # than fail, and C must stay in train while A/B keep their evaluation
    # roles.
    second_chunks, second_labels = _corpus(tmp_path / "second", ["A", "B", "C"])
    second_out = tmp_path / "second_out"
    assert (
        builder.main(
            [
                "--chunks",
                str(second_chunks),
                "--labels",
                f"public={second_labels}",
                "--pin-from-manifest",
                str(first_out / "manifest.json"),
                "--out-dir",
                str(second_out),
            ]
        )
        == 0
    )

    manifest = _manifest(second_out)
    assert manifest["assets"] == {
        "train": ["C"],
        "val": ["A"],
        "test": ["B"],
    }
    assert manifest["pinned_from"] == str(
        (first_out / "manifest.json").resolve()
    )


def test_force_split_conflicting_with_pin_is_rejected(tmp_path: Path) -> None:
    chunks, labels = _corpus(tmp_path, ["A", "B", "C"])
    out = tmp_path / "out"
    assert (
        builder.main(
            [
                "--chunks",
                str(chunks),
                "--labels",
                f"public={labels}",
                "--force-split",
                "A=val",
                "--out-dir",
                str(out),
            ]
        )
        == 0
    )

    assert (
        builder.main(
            [
                "--chunks",
                str(chunks),
                "--labels",
                f"public={labels}",
                "--pin-from-manifest",
                str(out / "manifest.json"),
                "--force-split",
                "A=test",
                "--out-dir",
                str(tmp_path / "conflict"),
            ]
        )
        == 2
    )


def test_repeat_source_weights_only_training_rows(tmp_path: Path) -> None:
    chunks, labels = _corpus(tmp_path, ["A", "B", "C", "D"])
    out = tmp_path / "out"

    assert (
        builder.main(
            [
                "--chunks",
                str(chunks),
                "--labels",
                f"public={labels}",
                "--force-split",
                "A=val",
                "--force-split",
                "B=test",
                "--repeat-source",
                "manual=3",
                "--out-dir",
                str(out),
            ]
        )
        == 0
    )

    manifest = _manifest(out)
    # A and B are pinned to evaluation, so only C (manual) is repeated.
    assert manifest["stats"]["train"] == 4
    assert manifest["stats"]["train_unique"] == 2
    assert manifest["train_mix"]["repeats"] == {"manual": 3}
    assert manifest["stats"]["val"] == 1
    assert manifest["stats"]["test"] == 1


@pytest.mark.parametrize("flag", ["--repeat-source", "--cap-source"])
def test_mix_option_rejects_a_duplicate_source(
    tmp_path: Path, flag: str
) -> None:
    chunks, labels = _corpus(tmp_path, ["A", "B", "C"])

    with pytest.raises(SystemExit) as excinfo:
        builder.main(
            [
                "--chunks",
                str(chunks),
                "--labels",
                f"public={labels}",
                flag,
                "manual=2",
                flag,
                "manual=3",
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
    assert excinfo.value.code == 2


def test_pin_from_missing_manifest_is_an_error(tmp_path: Path) -> None:
    chunks, labels = _corpus(tmp_path, ["A", "B", "C"])

    assert (
        builder.main(
            [
                "--chunks",
                str(chunks),
                "--labels",
                f"public={labels}",
                "--pin-from-manifest",
                str(tmp_path / "missing.json"),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
        == 2
    )
