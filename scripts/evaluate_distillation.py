"""Evaluate base and distilled models on the W7 held-out test set.

The script runs one or more model variants over the same ``test.jsonl`` /
``test_meta.jsonl`` pair and writes per-sample predictions plus a JSON/Markdown
report.  It supports two backends:

* ``transformers`` (default): load the base model and any LoRA adapter locally.
  This is the path used on the ModelArts NPU notebook.
* ``openai``: call an OpenAI-compatible endpoint, so a merged model served by
  vLLM or a GGUF served by Ollama can be scored with the same metrics.

Example on the training machine::

    python scripts/evaluate_distillation.py

By default it evaluates the base model and the three adapters produced by
``training/train_all.sh``.  Run only some of them with ``--run``::

    python scripts/evaluate_distillation.py \
        --run B1=training/saves/qwen2.5-1.5b-evidence-lora \
        --run B2=training/saves/qwen2.5-1.5b-evidence-stage2-lora

The primary test set is ``training/data/w7_b/test.jsonl`` (652 samples: the
588 CFDK chunks shared with the baseline plus a 64-chunk held-out Grundfos
manual).  The report groups samples into ``cfdk`` and ``manual`` so a
target-domain gain is not hidden by the larger cross-domain corpus.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import types
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_lightweight_package(name: str, directory: Path) -> None:
    """Register a path-bearing package without executing its ``__init__``.

    The training machine installs the training stack but not LightRAG's
    runtime dependencies (``json_repair``, ``python-dotenv``, ...).  The three
    distillation modules used here are deliberately dependency-free, but a
    normal import runs ``lightrag/fault_tree/__init__.py`` and then
    ``lightrag.utils``.  Stubbing the two package objects keeps this scorer
    runnable in the training environment.
    """
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]  # type: ignore[attr-defined]
    package.__package__ = name
    sys.modules[name] = package


_install_lightweight_package(
    "lightrag.fault_tree", ROOT / "lightrag" / "fault_tree"
)
_install_lightweight_package(
    "lightrag.fault_tree.distillation",
    ROOT / "lightrag" / "fault_tree" / "distillation",
)

from lightrag.fault_tree.distillation.labels import (  # noqa: E402
    StudentLabelError,
    validate_student_target,
)
from lightrag.fault_tree.distillation.metrics import (  # noqa: E402
    SampleScore,
    aggregate_scores,
    check_split_leakage,
    group_scores,
    resolve_asset_id,
    score_sample,
)
from lightrag.fault_tree.distillation.teacher import (  # noqa: E402
    TeacherOutputError,
    extract_json_object,
)

DEFAULT_DATA = ROOT / "training" / "data" / "w7_b" / "test.jsonl"
DEFAULT_BASE_MODEL = ROOT / "training" / "models" / "Qwen2.5-1.5B-Instruct"
DEFAULT_RUNS = (
    ("base", DEFAULT_BASE_MODEL),
    ("A_cfdk", ROOT / "training" / "saves" / "qwen2.5-1.5b-cfdk-baseline-lora"),
    ("B1_evidence", ROOT / "training" / "saves" / "qwen2.5-1.5b-evidence-lora"),
    (
        "B2_evidence_stage2",
        ROOT / "training" / "saves" / "qwen2.5-1.5b-evidence-stage2-lora",
    ),
)


@dataclass(frozen=True)
class Sample:
    sample_id: str
    chunk_text: str
    gold: dict[str, Any]
    messages: list[dict[str, str]]


@dataclass(frozen=True)
class RunSpec:
    name: str
    path: Path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise SystemExit(f"{path}:{line_no}: row must be an object")
        rows.append(row)
    return rows


def _row_messages(row: Mapping[str, Any], *, sample_id: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    system = row.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    conversations = row.get("conversations")
    if not isinstance(conversations, list) or not conversations:
        raise SystemExit(f"{sample_id}: no conversations in the test row")
    first = conversations[0]
    if not isinstance(first, Mapping) or first.get("from") not in {"human", "user"}:
        raise SystemExit(f"{sample_id}: first conversation turn is not human")
    messages.append({"role": "user", "content": str(first.get("value") or "")})
    return messages


def load_samples(data_path: Path, meta_path: Path) -> list[Sample]:
    """Pair ShareGPT test rows with their meta rows by id, not by position."""
    rows = _read_jsonl(data_path)
    metas = _read_jsonl(meta_path)
    meta_by_id: dict[str, dict[str, Any]] = {}
    for meta in metas:
        sample_id = str(meta.get("id") or "").strip()
        if not sample_id:
            raise SystemExit(f"{meta_path}: a row has no id")
        if sample_id in meta_by_id:
            raise SystemExit(f"{meta_path}: duplicate id {sample_id}")
        meta_by_id[sample_id] = meta

    samples: list[Sample] = []
    seen: set[str] = set()
    for row in rows:
        sample_id = str(row.get("id") or "").strip()
        if not sample_id:
            raise SystemExit(f"{data_path}: a row has no id")
        if sample_id in seen:
            raise SystemExit(f"{data_path}: duplicate id {sample_id}")
        seen.add(sample_id)
        meta = meta_by_id.get(sample_id)
        if meta is None:
            raise SystemExit(f"{data_path}: {sample_id} has no meta row")
        chunk_text = str(meta.get("chunk_text") or "")
        if not chunk_text:
            raise SystemExit(f"{meta_path}: {sample_id} has empty chunk_text")
        gold = meta.get("gold")
        if not isinstance(gold, dict):
            raise SystemExit(f"{meta_path}: {sample_id} has no gold object")
        samples.append(
            Sample(
                sample_id=sample_id,
                chunk_text=chunk_text,
                gold=gold,
                messages=_row_messages(row, sample_id=sample_id),
            )
        )
    extra = sorted(set(meta_by_id) - seen)
    if extra:
        raise SystemExit(
            f"{meta_path}: {len(extra)} meta rows have no test row, e.g. {extra[:5]}"
        )
    return samples


def _parse_run_specs(raw: Sequence[str], *, backend: str) -> list[RunSpec]:
    if raw:
        specs: list[RunSpec] = []
        names: set[str] = set()
        for item in raw:
            if "=" in item:
                name, value = item.split("=", 1)
            else:
                name, value = item, item
            name = name.strip()
            value = value.strip()
            if not name:
                raise SystemExit(f"--run has an empty name: {item!r}")
            if name in names:
                raise SystemExit(f"--run name is duplicated: {name}")
            names.add(name)
            specs.append(RunSpec(name=name, path=Path(value).expanduser()))
        return specs
    if backend != "transformers":
        raise SystemExit(
            "--backend openai needs at least one --run NAME (the served model "
            "name is given by --served-model)"
        )
    specs = [
        RunSpec(name=name, path=path)
        for name, path in DEFAULT_RUNS
        if path.exists()
    ]
    if not specs:
        raise SystemExit(
            "no default model paths exist; pass --run NAME=PATH explicitly"
        )
    return specs


def _resolve_device(requested: str) -> str:
    import torch

    try:
        import torch_npu  # noqa: F401
    except ImportError:
        pass
    if requested != "auto":
        return requested
    if hasattr(torch, "npu") and torch.npu.is_available():
        return "npu:0"
    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def _resolve_dtype(requested: str, device: str) -> Any:
    import torch

    if requested == "bfloat16":
        return torch.bfloat16
    if requested == "float16":
        return torch.float16
    if requested == "float32":
        return torch.float32
    return torch.bfloat16 if device != "cpu" else torch.float32


def _load_transformers(
    spec: RunSpec, *, base_model: Path, device: str, dtype_name: str
) -> tuple[Any, Any, str]:
    """Load a base model or a LoRA adapter; return tokenizer, model, kind."""
    from transformers import AutoModelForCausalLM, AutoTokenizer

    path = spec.path.resolve()
    if not path.exists():
        raise SystemExit(f"{spec.name}: model path does not exist: {path}")
    is_adapter = (path / "adapter_config.json").exists()
    load_path = base_model.resolve() if is_adapter else path
    if not load_path.exists():
        raise SystemExit(
            f"{spec.name}: base model does not exist: {load_path}"
        )
    dtype = _resolve_dtype(dtype_name, device)
    tokenizer = AutoTokenizer.from_pretrained(
        str(load_path), trust_remote_code=False, use_fast=True
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"

    model = AutoModelForCausalLM.from_pretrained(
        str(load_path),
        torch_dtype=dtype,
        low_cpu_mem_usage=True,
        trust_remote_code=False,
    )
    kind = "base"
    if is_adapter:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, str(path))
        kind = "adapter"
    model.to(device)
    model.eval()
    model.config.use_cache = True
    return tokenizer, model, kind


def _render_prompt(tokenizer: Any, messages: Sequence[Mapping[str, str]]) -> str:
    template = getattr(tokenizer, "chat_template", None)
    if template:
        return tokenizer.apply_chat_template(
            list(messages), tokenize=False, add_generation_prompt=True
        )
    lines = [f"{message['role']}: {message['content']}" for message in messages]
    lines.append("assistant:")
    return "\n".join(lines)


def _order_by_prompt_length(
    tokenizer: Any, samples: Sequence[Sample]
) -> list[tuple[str, Sample]]:
    """Group prompts of similar length so each batch pads to its own longest.

    Decoding runs until the longest sequence in the batch hits EOS, so one
    long sample keeps the rest of its batch spinning after they finished.
    Chunk size drives output length, so sorting by prompt length clusters
    samples that finish together and removes most cross-sample padding.
    Rows stay keyed by sample id, so the order does not change the report.
    """
    rendered = [
        (_render_prompt(tokenizer, sample.messages), sample)
        for sample in samples
    ]
    if len(rendered) < 2:
        return rendered
    lengths = [
        len(ids)
        for ids in tokenizer(
            [prompt for prompt, _ in rendered], add_special_tokens=False
        )["input_ids"]
    ]
    ordered = sorted(zip(lengths, rendered), key=lambda pair: pair[0])
    return [item for _, item in ordered]


def _generate_transformers(
    model: Any,
    tokenizer: Any,
    prompts: Sequence[str],
    *,
    device: str,
    max_new_tokens: int,
) -> tuple[list[str], list[int]]:
    import torch

    inputs = tokenizer(
        list(prompts), return_tensors="pt", padding=True, truncation=False
    )
    inputs = {name: value.to(device) for name, value in inputs.items()}
    with torch.inference_mode():
        generated = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            num_beams=1,
            use_cache=True,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    new_tokens = generated[:, inputs["input_ids"].shape[1] :]
    texts = tokenizer.batch_decode(new_tokens, skip_special_tokens=True)
    counts = (new_tokens != tokenizer.pad_token_id).sum(dim=1).tolist()
    return [str(text) for text in texts], [int(count) for count in counts]


def _openai_url(endpoint: str) -> str:
    value = endpoint.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    return value + "/chat/completions"


def _openai_chat(
    client: Any,
    *,
    url: str,
    model: str,
    api_key: str,
    messages: Sequence[Mapping[str, str]],
    max_tokens: int,
    timeout: float,
) -> tuple[str, int]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = client.post(
        url,
        headers=headers,
        json={
            "model": model,
            "messages": list(messages),
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected response shape: {exc}") from exc
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping)
        )
    usage = payload.get("usage")
    tokens = 0
    if isinstance(usage, Mapping):
        tokens = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
    return str(content), tokens


def _generate_openai(
    samples: Sequence[Sample],
    *,
    endpoint: str,
    served_model: str,
    api_key: str,
    concurrency: int,
    max_tokens: int,
    timeout: float,
) -> list[tuple[str, int, str | None]]:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit("httpx is required for --backend openai") from exc

    url = _openai_url(endpoint)
    with httpx.Client() as client:
        def call(sample: Sample) -> tuple[str, int, str | None]:
            try:
                text, tokens = _openai_chat(
                    client,
                    url=url,
                    model=served_model,
                    api_key=api_key,
                    messages=sample.messages,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                return text, tokens, None
            except Exception as exc:  # keep one failure from killing the run
                return "", 0, str(exc)

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            return list(pool.map(call, samples))


def _build_record(
    sample_id: str,
    raw: str,
    output_tokens: int,
    generation_error: str | None,
) -> dict[str, Any]:
    prediction: dict[str, Any] | None = None
    json_valid = False
    json_error: str | None = None
    if generation_error is None:
        try:
            prediction = extract_json_object(raw)
            json_valid = True
        except TeacherOutputError as exc:
            json_error = str(exc)
    return {
        "id": sample_id,
        "raw": raw,
        "prediction": prediction,
        "json_valid": json_valid,
        "json_error": json_error,
        "generation_error": generation_error,
        "output_tokens": int(output_tokens),
    }


def _repair_partial_line(path: Path) -> None:
    """Drop a trailing partial JSONL row left by an interrupted run.

    A run killed mid-write can leave a row without its newline.  Appending
    after it would corrupt the next row, so ``--resume`` truncates back to
    the last complete line before reading it.
    """
    if not path.exists():
        return
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    cut = data.rfind(b"\n")
    path.write_bytes(data[: cut + 1] if cut >= 0 else b"")


def _load_done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done: set[str] = set()
    for row in _read_jsonl(path):
        sample_id = str(row.get("id") or "")
        if sample_id:
            done.add(sample_id)
    return done


def _score_records(
    samples: Sequence[Sample],
    records: Mapping[str, Mapping[str, Any]],
    asset_ids: Mapping[str, str] | None = None,
) -> list[SampleScore]:
    scores: list[SampleScore] = []
    for sample in samples:
        record = records[sample.sample_id]
        prediction = record.get("prediction")
        if prediction is not None and not isinstance(prediction, dict):
            prediction = None
        contract_valid = False
        contract_error: str | None = None
        if isinstance(prediction, dict):
            try:
                validate_student_target(
                    prediction,
                    sample.chunk_text,
                    sample_id=sample.sample_id,
                )
                contract_valid = True
            except StudentLabelError as exc:
                contract_error = str(exc)
        scores.append(
            score_sample(
                sample_id=sample.sample_id,
                chunk_text=sample.chunk_text,
                gold=sample.gold,
                prediction=prediction,
                json_valid=bool(record.get("json_valid")),
                contract_valid=contract_valid,
                contract_error=contract_error,
                generation_error=(
                    str(record["generation_error"])
                    if record.get("generation_error")
                    else None
                ),
                output_tokens=int(record.get("output_tokens") or 0),
                asset_id=(
                    asset_ids.get(sample.sample_id) if asset_ids else None
                ),
            )
        )
    return scores


def _markdown_table(report: Mapping[str, Any]) -> str:
    lines = [
        "| run | samples | JSON | contract | ent P | ent R | ent F1 | "
        "rel P | rel R | rel F1 | rule | quote | negative | empty | tokens |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, run in report["runs"].items():
        agg = run["aggregate"]
        entity = agg["entity"]
        relation = agg["relation"]
        lines.append(
            f"| {name} | {agg['samples']} | {agg['json_valid_rate']} | "
            f"{agg['contract_valid_rate']} | {entity['precision']} | "
            f"{entity['recall']} | {entity['f1']} | "
            f"{relation['precision']} | {relation['recall']} | "
            f"{relation['f1']} | {agg['cause_rule_accuracy']} | "
            f"{agg['quote_verbatim_rate']} | "
            f"{agg['negative_accuracy']} | {agg['empty_pred_rate']} | "
            f"{agg['avg_output_tokens']} |"
        )
    return "\n".join(lines)


def _group_table(report: Mapping[str, Any]) -> str:
    lines = [
        "| run | group | samples | ent F1 | rel F1 | quote | JSON |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, run in report["runs"].items():
        for group, agg in run["groups"].items():
            lines.append(
                f"| {name} | {group} | {agg['samples']} | "
                f"{agg['entity']['f1']} | {agg['relation']['f1']} | "
                f"{agg['quote_verbatim_rate']} | "
                f"{agg['json_valid_rate']} |"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "Model variant to evaluate; repeatable. PATH is a full model "
            "directory or a LoRA adapter directory. Defaults to base plus "
            "the three adapters under training/saves/ that exist."
        ),
    )
    parser.add_argument("--base-model", type=Path, default=DEFAULT_BASE_MODEL)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--meta", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "training" / "artifacts" / "eval" / "w7_b_test",
    )
    parser.add_argument(
        "--backend", choices=["transformers", "openai"], default="transformers"
    )
    parser.add_argument("--device", default="auto")
    parser.add_argument(
        "--dtype",
        choices=["auto", "bfloat16", "float16", "float32"],
        default="auto",
    )
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--endpoint",
        default="http://localhost:11434/v1/chat/completions",
    )
    parser.add_argument("--served-model", default="industrial-extractor")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    data_path = args.data.expanduser().resolve()
    meta_path = (
        args.meta.expanduser().resolve()
        if args.meta
        else data_path.with_name("test_meta.jsonl")
    )
    manifest_path = (
        args.manifest.expanduser().resolve()
        if args.manifest
        else data_path.with_name("manifest.json")
    )
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    samples = load_samples(data_path, meta_path)
    if args.offset:
        samples = samples[args.offset :]
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        raise SystemExit("no samples selected")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1")

    leakage: list[str] = []
    leakage_checked = False
    asset_ids: dict[str, str] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read {manifest_path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise SystemExit(f"{manifest_path}: manifest must be an object")
        leakage = check_split_leakage(
            manifest, [sample.sample_id for sample in samples]
        )
        assets = manifest.get("assets")
        known_assets: set[str] = set()
        if isinstance(assets, Mapping):
            for split_assets in assets.values():
                if isinstance(split_assets, list):
                    known_assets.update(str(asset) for asset in split_assets)
        for sample in samples:
            resolved = resolve_asset_id(sample.sample_id, known_assets)
            if resolved is not None:
                asset_ids[sample.sample_id] = resolved
        leakage_checked = True

    run_specs = _parse_run_specs(args.run, backend=args.backend)
    print(
        f"[eval] {len(samples)} samples from {data_path.name}; "
        f"runs: {', '.join(spec.name for spec in run_specs)}",
        file=sys.stderr,
    )
    if leakage:
        print(
            f"[eval] WARNING: {len(leakage)} split leakage problem(s); "
            "see report.json",
            file=sys.stderr,
        )

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "scripts/evaluate_distillation.py",
        "backend": args.backend,
        "data": str(data_path),
        "meta": str(meta_path),
        "manifest": str(manifest_path) if leakage_checked else None,
        "leakage_checked": leakage_checked,
        "leakage": leakage,
        "limit": args.limit,
        "offset": args.offset,
        "runs": {},
    }

    torch_module: Any = None
    if args.backend == "transformers":
        import torch

        torch_module = torch
        torch.manual_seed(0)
        if hasattr(torch, "npu") and torch.npu.is_available():
            torch.npu.manual_seed_all(0)

    for spec in run_specs:
        run_started = time.perf_counter()
        predictions_path = out_dir / f"{spec.name}.predictions.jsonl"
        if args.resume:
            _repair_partial_line(predictions_path)
            done_ids = _load_done_ids(predictions_path)
        else:
            done_ids = set()
        pending = [
            sample for sample in samples if sample.sample_id not in done_ids
        ]
        mode = "a" if args.resume and predictions_path.exists() else "w"
        handle = predictions_path.open(mode, encoding="utf-8", newline="\n")
        try:
            if args.backend == "transformers":
                device = _resolve_device(args.device)
                tokenizer, model, kind = _load_transformers(
                    spec,
                    base_model=args.base_model,
                    device=device,
                    dtype_name=args.dtype,
                )
                ordered = _order_by_prompt_length(tokenizer, pending)
                print(
                    f"[eval] {spec.name}: {kind} on {device}, "
                    f"{len(pending)} pending",
                    file=sys.stderr,
                )
                for start in range(0, len(ordered), args.batch_size):
                    chunk = ordered[start : start + args.batch_size]
                    batch = [sample for _, sample in chunk]
                    prompts = [prompt for prompt, _ in chunk]
                    texts, token_counts = _generate_transformers(
                        model,
                        tokenizer,
                        prompts,
                        device=device,
                        max_new_tokens=args.max_new_tokens,
                    )
                    for sample, text, token_count in zip(
                        batch, texts, token_counts
                    ):
                        handle.write(
                            json.dumps(
                                _build_record(
                                    sample.sample_id, text, token_count, None
                                ),
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    handle.flush()
                    done = min(start + len(batch), len(pending))
                    print(
                        f"[eval] {spec.name}: {done}/{len(pending)}",
                        file=sys.stderr,
                    )
                del model, tokenizer
                if torch_module is not None and device.startswith("npu"):
                    torch_module.npu.empty_cache()
                elif torch_module is not None and device.startswith("cuda"):
                    torch_module.cuda.empty_cache()
            else:
                print(
                    f"[eval] {spec.name}: served model {args.served_model}, "
                    f"{len(pending)} pending",
                    file=sys.stderr,
                )
                for start in range(0, len(pending), args.batch_size):
                    batch = pending[start : start + args.batch_size]
                    results = _generate_openai(
                        batch,
                        endpoint=args.endpoint,
                        served_model=args.served_model,
                        api_key=args.api_key,
                        concurrency=args.concurrency,
                        max_tokens=args.max_new_tokens,
                        timeout=args.timeout,
                    )
                    for sample, (text, token_count, error) in zip(
                        batch, results
                    ):
                        handle.write(
                            json.dumps(
                                _build_record(
                                    sample.sample_id, text, token_count, error
                                ),
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    handle.flush()
                    done = min(start + len(batch), len(pending))
                    print(
                        f"[eval] {spec.name}: {done}/{len(pending)}",
                        file=sys.stderr,
                    )
        finally:
            handle.close()

        records = {
            str(row["id"]): row
            for row in _read_jsonl(predictions_path)
            if row.get("id")
        }
        missing = [
            sample.sample_id
            for sample in samples
            if sample.sample_id not in records
        ]
        if missing:
            raise SystemExit(
                f"{spec.name}: {len(missing)} predictions missing, e.g. "
                f"{missing[:5]}"
            )
        scores = _score_records(samples, records, asset_ids)
        elapsed = time.perf_counter() - run_started
        run_report = {
            "path": str(spec.path),
            "samples": len(scores),
            "elapsed_s": round(elapsed, 2),
            "samples_per_s": round(len(scores) / elapsed, 3) if elapsed else None,
            "aggregate": aggregate_scores(scores),
            "groups": group_scores(scores),
            "assets": group_scores(scores, key="asset_id"),
        }
        report["runs"][spec.name] = run_report
        print(
            f"[eval] {spec.name}: ent_f1={run_report['aggregate']['entity']['f1']} "
            f"rel_f1={run_report['aggregate']['relation']['f1']} "
            f"json={run_report['aggregate']['json_valid_rate']} "
            f"quote={run_report['aggregate']['quote_verbatim_rate']}",
            file=sys.stderr,
        )

    table = _markdown_table(report)
    group_table = _group_table(report)
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    (out_dir / "report.json").write_text(report_text + "\n", encoding="utf-8")
    (out_dir / "summary.md").write_text(
        "# W7 distillation evaluation\n\n"
        f"- data: `{data_path}`\n"
        f"- samples: {len(samples)}\n"
        f"- leakage problems: {len(leakage)}\n\n"
        "## Overall (normalized name matching)\n\n"
        f"{table}\n\n"
        "## By source group\n\n"
        f"{group_table}\n",
        encoding="utf-8",
    )
    print(table)
    print()
    print(group_table)
    print(f"\n[eval] wrote {out_dir}")
    if leakage:
        print("\n[eval] leakage problems:")
        for problem in leakage[:20]:
            print(f"  - {problem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
