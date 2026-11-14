#!/usr/bin/env bash
# One-shot W7 evaluation on the ModelArts Ascend notebook.
#
# Run it from the repository root, in the same conda environment used for
# training (the one with torch_npu):
#
#   cd /home/ma-user/work/LightRAG
#   bash training/run_w7_eval.sh
#
# It performs, in order:
#   1. environment / file / disk checks
#   2. stage-2 adapter-lineage check from the training logs
#   3. the metrics unit tests (skipped when pytest is absent)
#   4. a 2-sample smoke test on NPU
#   5. the full 652-sample evaluation of the selected arms (default: base + B1)
#   6. selection of one adapter by held-out-manual F1 with a CFDK guardrail
#   7. merge of the selected adapter (CPU), and GGUF conversion when llama.cpp
#      is available
#
# Every failure stops the script BEFORE the expensive phase, except the final
# merge/GGUF steps, which are best-effort.  Predictions are flushed per batch,
# so re-running the same command resumes an interrupted evaluation.
#
# Tunables (export before running to override):
#   ARMS, REPO, BASE_MODEL, DATA_DIR, A_ADAPTER, B1_ADAPTER, B2_ADAPTER,
#   BATCH_SIZE, MAX_NEW_TOKENS, MERGE_WINNER, RUN_GGUF, LLAMA_CPP_DIR
#
# ARMS picks the evaluation arms, space separated: base A B1 B2.
#   default "base B1"  -> how much did fine-tuning improve? (the W7 question)
#   "base A B1 B2"     -> full ablation: does the manual data help, and does
#                         the extra target-domain stage help or overfit?

set -uo pipefail

ARMS="${ARMS:-base B1}"
REPO="${REPO:-/home/ma-user/work/LightRAG}"
BASE_MODEL="${BASE_MODEL:-$REPO/training/models/Qwen2.5-1.5B-Instruct}"
DATA_DIR="${DATA_DIR:-$REPO/training/data/w7_b}"
A_ADAPTER="${A_ADAPTER:-$REPO/training/saves/qwen2.5-1.5b-cfdk-baseline-lora}"
B1_ADAPTER="${B1_ADAPTER:-$REPO/training/saves/qwen2.5-1.5b-evidence-lora}"
B2_ADAPTER="${B2_ADAPTER:-$REPO/training/saves/qwen2.5-1.5b-evidence-stage2-lora}"
BATCH_SIZE="${BATCH_SIZE:-8}"
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-1536}"
MERGE_WINNER="${MERGE_WINNER:-1}"
RUN_GGUF="${RUN_GGUF:-1}"
LLAMA_CPP_DIR="${LLAMA_CPP_DIR:-}"

STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$REPO/logs"
EVAL_DIR="$REPO/training/artifacts/eval/w7_b_test"
SMOKE_DIR="$REPO/training/artifacts/eval/smoke_$STAMP"
EVAL_LOG="$LOG_DIR/eval_w7_b_$STAMP.log"

echo "=== W7 evaluation $STAMP ==="
echo "repo:   $REPO"
echo "base:   $BASE_MODEL"
echo "data:   $DATA_DIR"
echo "arms:   $ARMS"
echo "batch:  $BATCH_SIZE"
echo "tokens: $MAX_NEW_TOKENS"
echo

cd "$REPO" || {
  echo "ERROR: repository not found at $REPO; set REPO=... and rerun" >&2
  exit 1
}
mkdir -p "$LOG_DIR" "$EVAL_DIR" "$SMOKE_DIR"
# The GGUF manifest is the only artifact that describes one specific model;
# clear it so a rerun that skips GGUF cannot bundle a stale one.
rm -f "$EVAL_DIR/manifest.json"

# ---------------------------------------------------------------- 1. checks
echo "=== 1. environment ==="
python - <<'PY' || exit 1
import sys

print("python:", sys.version.split()[0])

try:
    import torch
except Exception as exc:  # noqa: BLE001 - report the actual import error
    raise SystemExit(f"ERROR: cannot import torch: {exc}")

print("torch:", torch.__version__)

try:
    import torch_npu  # noqa: F401

    print("torch_npu:", getattr(torch_npu, "__version__", "unknown"))
    print("npu_available:", torch.npu.is_available())
    if not torch.npu.is_available():
        raise SystemExit(
            "ERROR: torch_npu imported but no NPU is visible; check npu-smi"
        )
except ImportError as exc:
    raise SystemExit(
        f"ERROR: torch_npu is missing ({exc}); activate the PyTorch-2.6.0 "
        "conda environment used for training before running this script"
    )

try:
    import peft
    import transformers

    print("transformers:", transformers.__version__)
    print("peft:", peft.__version__)
except Exception as exc:  # noqa: BLE001
    raise SystemExit(f"ERROR: transformers/peft unavailable: {exc}")
PY

echo
echo "=== 1b. required files ==="
required=(
  "scripts/evaluate_distillation.py"
  "lightrag/fault_tree/distillation/metrics.py"
  "lightrag/fault_tree/distillation/labels.py"
  "lightrag/fault_tree/distillation/teacher.py"
  "$BASE_MODEL/config.json"
  "$DATA_DIR/test.jsonl"
  "$DATA_DIR/test_meta.jsonl"
  "$DATA_DIR/manifest.json"
)
case " $ARMS " in
  *" A "*) required+=("$A_ADAPTER/adapter_config.json") ;;
esac
case " $ARMS " in
  *" B1 "*) required+=("$B1_ADAPTER/adapter_config.json") ;;
esac
case " $ARMS " in
  *" B2 "*) required+=("$B2_ADAPTER/adapter_config.json") ;;
esac
for arm in $ARMS; do
  case "$arm" in
    base|A|B1|B2) ;;
    *)
      echo "ERROR: unknown ARMS entry '$arm' (use base, A, B1, B2)" >&2
      exit 2
      ;;
  esac
done
missing=0
for path in "${required[@]}"; do
  if [ -e "$path" ]; then
    echo "OK   $path"
  else
    echo "MISS $path"
    missing=$((missing + 1))
  fi
done
if [ "$missing" -ne 0 ]; then
  echo
  echo "ERROR: $missing required path(s) missing; fix them before spending NPU time." >&2
  exit 2
fi

echo
echo "=== 1c. disk / NPU ==="
df -h "$REPO" | tail -1
if command -v npu-smi >/dev/null 2>&1; then
  npu-smi info | head -20
fi

# ------------------------------------------------------- 2. stage-2 lineage
echo
echo "=== 2. stage-2 adapter lineage ==="
wants_b2=0
case " $ARMS " in
  *" B2 "*) wants_b2=1 ;;
esac
if [ "$wants_b2" -eq 1 ]; then
  lineage_found=0
  for log in "$LOG_DIR/B_stage2.log" "$LOG_DIR/train_all.log"; do
    if [ -f "$log" ]; then
      lineage_found=1
      echo "-- $log"
      grep -n "Loaded adapter" "$log" | tail -5 || echo "no 'Loaded adapter' line"
    fi
  done
  if [ "$lineage_found" -eq 0 ]; then
    echo "no stage-2 log found under $LOG_DIR"
  fi
  echo "NOTE: 'Loaded adapter(s): ...evidence-lora' means B2 continues B1."
  echo "      Without that line B2 is a stage-2-only adapter; the report will"
  echo "      still be valid, but describe it that way."
else
  echo "skipped: B2 is not in ARMS ($ARMS)"
fi

# ------------------------------------------------------------ 3. unit tests
echo
echo "=== 3. metrics unit tests ==="
if [ -f tests/domain/test_distillation_metrics.py ] \
  && python -c "import pytest" >/dev/null 2>&1; then
  # The test imports lightrag.fault_tree.distillation.metrics the normal way,
  # which executes lightrag/__init__.py and lightrag/fault_tree/__init__.py,
  # so it needs LightRAG's runtime deps (json_repair, tiktoken, ...).  The
  # training environment usually lacks them; probe the import first, because
  # a collection ImportError and an assertion failure can both exit 1.
  if python -c "import lightrag.fault_tree.distillation.metrics" \
    >/dev/null 2>&1; then
    python -m pytest tests/domain/test_distillation_metrics.py -q
    test_status=$?
    if [ "$test_status" -eq 0 ]; then
      echo "unit tests passed"
    elif [ "$test_status" -eq 1 ]; then
      echo "ERROR: metrics tests failed; the evaluation would be untrustworthy." >&2
      exit 3
    else
      echo "SKIP: pytest could not run here (exit $test_status)."
      echo "      The smoke test below still exercises the metrics end to end."
    fi
  else
    echo "SKIP: importing lightrag.fault_tree needs LightRAG's runtime"
    echo "      dependencies, which the training environment lacks."
    echo "      The smoke test below still exercises the metrics end to end."
  fi
else
  echo "SKIP: pytest or tests/domain/test_distillation_metrics.py is missing."
  echo "      The smoke test below still exercises the metrics end to end."
fi

# -------------------------------------------------------------- 4. smoke run
echo
echo "=== 4. smoke test (2 samples, base + first adapter in ARMS) ==="
smoke_adapter=""
for arm in $ARMS; do
  case "$arm" in
    A) smoke_adapter="A_cfdk=$A_ADAPTER" ;;
    B1) smoke_adapter="B1_evidence=$B1_ADAPTER" ;;
    B2) smoke_adapter="B2_evidence_stage2=$B2_ADAPTER" ;;
    base) ;;
  esac
  [ -n "$smoke_adapter" ] && break
done
smoke_args=(--run "base=$BASE_MODEL")
if [ -n "$smoke_adapter" ]; then
  smoke_args+=(--run "$smoke_adapter")
fi
if ! python scripts/evaluate_distillation.py \
  --base-model "$BASE_MODEL" \
  --data "$DATA_DIR/test.jsonl" \
  --meta "$DATA_DIR/test_meta.jsonl" \
  --manifest "$DATA_DIR/manifest.json" \
  "${smoke_args[@]}" \
  --limit 2 --batch-size 2 \
  --out-dir "$SMOKE_DIR"; then
  echo "ERROR: smoke test failed; stopping before the full run." >&2
  exit 4
fi
echo
cat "$SMOKE_DIR/summary.md"

# --------------------------------------------------------- 5. full evaluation
echo
echo "=== 5. full evaluation (652 samples; arms: $ARMS) ==="
echo "log: $EVAL_LOG"
echo "This is the expensive phase. Predictions flush per batch; if it is"
echo "interrupted, run the same command again and --resume continues."
echo
run_args=()
for arm in $ARMS; do
  case "$arm" in
    base) run_args+=(--run "base=$BASE_MODEL") ;;
    A) run_args+=(--run "A_cfdk=$A_ADAPTER") ;;
    B1) run_args+=(--run "B1_evidence=$B1_ADAPTER") ;;
    B2) run_args+=(--run "B2_evidence_stage2=$B2_ADAPTER") ;;
  esac
done
if [ "${#run_args[@]}" -eq 0 ]; then
  echo "ERROR: ARMS did not select any model; set ARMS='base B1'" >&2
  exit 2
fi
python scripts/evaluate_distillation.py \
  --base-model "$BASE_MODEL" \
  --data "$DATA_DIR/test.jsonl" \
  --meta "$DATA_DIR/test_meta.jsonl" \
  --manifest "$DATA_DIR/manifest.json" \
  "${run_args[@]}" \
  --batch-size "$BATCH_SIZE" \
  --max-new-tokens "$MAX_NEW_TOKENS" \
  --resume 2>&1 | tee "$EVAL_LOG"
eval_status=${PIPESTATUS[0]}
if [ "$eval_status" -ne 0 ]; then
  echo "ERROR: evaluation exited with status $eval_status." >&2
  echo "Per-sample predictions are preserved; rerun this script to resume." >&2
  exit 5
fi

# ------------------------------------------------------------- 6. selection
echo
echo "=== 6. selection ==="
python - "$EVAL_DIR/report.json" "$EVAL_DIR/selection.json" <<'PY' || exit 6
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

report_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
report = json.loads(report_path.read_text(encoding="utf-8"))


def number(value, default=-1.0):
    return float(value) if isinstance(value, (int, float)) else default


def metric(container, *keys):
    value = container
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


rows = []
for name in ("A_cfdk", "B1_evidence", "B2_evidence_stage2"):
    run = (report.get("runs") or {}).get(name)
    if not run:
        continue
    aggregate = run.get("aggregate") or {}
    groups = run.get("groups") or {}
    rows.append(
        {
            "name": name,
            "adapter": run.get("path"),
            "manual_entity_f1": metric(groups, "manual", "entity", "f1"),
            "cfdk_entity_f1": metric(groups, "cfdk", "entity", "f1"),
            "manual_relation_f1": metric(groups, "manual", "relation", "f1"),
            "cfdk_relation_f1": metric(groups, "cfdk", "relation", "f1"),
            "json_valid_rate": aggregate.get("json_valid_rate"),
            "contract_valid_rate": aggregate.get("contract_valid_rate"),
            "quote_verbatim_rate": aggregate.get("quote_verbatim_rate"),
            "avg_output_tokens": aggregate.get("avg_output_tokens"),
        }
    )

base = (report.get("runs") or {}).get("base") or {}
base_manual = metric(base.get("groups") or {}, "manual", "entity", "f1")

warnings = []
eligible = [
    row
    for row in rows
    if number(row["quote_verbatim_rate"], 0) >= 0.98
    and number(row["json_valid_rate"], 0) >= 0.95
    and number(row["contract_valid_rate"], 0) >= 0.90
]
pool = eligible or rows
if rows and not eligible:
    warnings.append(
        "no candidate met quote>=0.98, JSON>=0.95 and contract>=0.90; "
        "ranked all candidates anyway"
    )

selected = None
reason = ""
if pool:
    best_cfdk = max(number(row["cfdk_entity_f1"], 0) for row in pool)
    near = [
        row
        for row in pool
        if number(row["cfdk_entity_f1"], 0) >= best_cfdk - 0.03
    ]
    selected = max(
        near,
        key=lambda row: (
            number(row["manual_entity_f1"], 0),
            number(row["cfdk_entity_f1"], 0),
        ),
    )
    reason = (
        f"highest held-out-manual entity F1 "
        f"({selected['manual_entity_f1']}) among adapters whose CFDK entity "
        f"F1 is within 0.03 of the best CFDK F1 ({best_cfdk})"
    )
    if base_manual is not None and number(selected["manual_entity_f1"], 0) < number(
        base_manual, 0
    ):
        warnings.append(
            f"selected adapter manual F1 {selected['manual_entity_f1']} is below "
            f"base {base_manual}; distillation did not help the target domain"
        )
else:
    warnings.append("no adapter run found in report.json")

payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "selection_rule": (
        "manual entity F1, restricted to candidates within 0.03 of the best "
        "CFDK entity F1; quote_verbatim_rate>=0.98, json_valid_rate>=0.95, "
        "contract_valid_rate>=0.90"
    ),
    "base_manual_entity_f1": base_manual,
    "candidates": rows,
    "selected": selected,
    "reason": reason,
    "warnings": warnings,
}
out_path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

print(
    f"{'run':<20} {'manual_f1':>9} {'cfdk_f1':>8} "
    f"{'json':>6} {'contract':>8} {'quote':>6} {'tokens':>7}"
)
for row in rows:
    print(
        f"{row['name']:<20} {str(row['manual_entity_f1']):>9} "
        f"{str(row['cfdk_entity_f1']):>8} {str(row['json_valid_rate']):>6} "
        f"{str(row['contract_valid_rate']):>8} "
        f"{str(row['quote_verbatim_rate']):>6} "
        f"{str(row['avg_output_tokens']):>7}"
    )
print()
if selected:
    print(f"selected: {selected['name']}")
    print(f"adapter:  {selected['adapter']}")
    print(f"reason:   {reason}")
else:
    print("selected: none")
for warning in warnings:
    print(f"WARNING: {warning}")
print(f"wrote {out_path}")
PY

winner_name="$(python - "$EVAL_DIR/selection.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
selected = payload.get("selected") or {}
print(selected.get("name") or "")
PY
)"
winner_adapter="$(python - "$EVAL_DIR/selection.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
selected = payload.get("selected") or {}
print(selected.get("adapter") or "")
PY
)"

# --------------------------------------------------------------- 7. merge
echo
echo "=== 7. merge / GGUF ==="
merged_dir=""
if [ "$MERGE_WINNER" = "1" ] && [ -n "$winner_name" ]; then
  if command -v llamafactory-cli >/dev/null 2>&1; then
    merged_dir="$REPO/training/saves/w7-${winner_name}-merged"
    merge_yaml="$REPO/training/artifacts/eval/merge_selected.yaml"
    cat > "$merge_yaml" <<EOF
model_name_or_path: $BASE_MODEL
adapter_name_or_path: $winner_adapter
template: qwen
finetuning_type: lora
export_dir: $merged_dir
export_size: 2
export_device: cpu
export_legacy_format: false
EOF
    if [ -f "$merged_dir/config.json" ]; then
      echo "merge already present: $merged_dir"
    else
      echo "merging $winner_name -> $merged_dir"
      if llamafactory-cli export "$merge_yaml" 2>&1 \
        | tee "$LOG_DIR/merge_${winner_name}_$STAMP.log"; then
        echo "merged: $merged_dir"
      else
        echo "WARNING: merge failed; see $LOG_DIR/merge_${winner_name}_$STAMP.log" >&2
        merged_dir=""
      fi
    fi
  else
    echo "WARNING: llamafactory-cli not found; skipping merge" >&2
  fi
else
  echo "merge skipped (MERGE_WINNER=$MERGE_WINNER, winner='$winner_name')"
fi

if [ "$RUN_GGUF" = "1" ] && [ -n "$merged_dir" ] && [ -d "$merged_dir" ]; then
  if [ -z "$LLAMA_CPP_DIR" ]; then
    for candidate in "$REPO/llama.cpp" "$HOME/llama.cpp" \
      "/home/ma-user/work/llama.cpp"; do
      if [ -f "$candidate/convert_hf_to_gguf.py" ]; then
        LLAMA_CPP_DIR="$candidate"
        break
      fi
    done
  fi
  if [ -n "$LLAMA_CPP_DIR" ] \
    && [ -f "$LLAMA_CPP_DIR/convert_hf_to_gguf.py" ]; then
    mkdir -p "$REPO/training/artifacts"
    f16="$REPO/training/artifacts/w7-${winner_name}-f16.gguf"
    q4="$REPO/training/artifacts/w7-${winner_name}-q4_k_m.gguf"
    echo "converting to GGUF: $f16"
    if python "$LLAMA_CPP_DIR/convert_hf_to_gguf.py" "$merged_dir" \
      --outfile "$f16" --outtype f16; then
      if [ -x "$LLAMA_CPP_DIR/build/bin/llama-quantize" ]; then
        echo "quantizing: $q4"
        if "$LLAMA_CPP_DIR/build/bin/llama-quantize" "$f16" "$q4" Q4_K_M; then
          python - "$q4" "$EVAL_DIR/report.json" "$EVAL_DIR/selection.json" \
            "$REPO/training/artifacts/manifest.json" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

gguf = Path(sys.argv[1])
report = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
selection = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
out = Path(sys.argv[4])
selected = selection.get("selected") or {}
run = (report.get("runs") or {}).get(selected.get("name")) or {}
digest = hashlib.sha256()
with gguf.open("rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
payload = {
    "model_name": "industrial-extractor",
    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "base_model": "Qwen2.5-1.5B-Instruct",
    "adapter": selected.get("adapter"),
    "selection_reason": selection.get("reason"),
    "quantization": "Q4_K_M",
    "gguf": str(gguf),
    "gguf_sha256": digest.hexdigest(),
    "gguf_size_bytes": gguf.stat().st_size,
    "test_metrics": {
        "aggregate": run.get("aggregate"),
        "groups": run.get("groups"),
    },
}
out.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"wrote {out}")
PY
          cp -f "$REPO/training/artifacts/manifest.json" \
            "$EVAL_DIR/manifest.json"
        else
          echo "WARNING: llama-quantize failed" >&2
        fi
      else
        echo "WARNING: llama-quantize not built; f16 GGUF left at $f16" >&2
      fi
    else
      echo "WARNING: convert_hf_to_gguf failed; no GGUF produced" >&2
    fi
  else
    echo "llama.cpp not found; skipped GGUF."
    echo "Set LLAMA_CPP_DIR=/path/to/llama.cpp and rerun if you want it here."
  fi
else
  echo "GGUF skipped (RUN_GGUF=$RUN_GGUF, merged_dir='$merged_dir')"
fi

# ---------------------------------------------------------------- 8. bundle
echo
echo "=== 8. results ==="
cat "$EVAL_DIR/summary.md"
echo
ls -lh "$EVAL_DIR"
tarball="$REPO/training/artifacts/eval/w7_eval_$STAMP.tar.gz"
tar -czf "$tarball" -C "$REPO/training/artifacts/eval" \
  --ignore-failed-read w7_b_test "smoke_$STAMP" merge_selected.yaml 2>/dev/null \
  || true
echo
if [ -f "$tarball" ]; then
  echo "download bundle: $tarball"
fi
echo "report:    $EVAL_DIR/report.json"
echo "selection: $EVAL_DIR/selection.json"
echo "eval log:  $EVAL_LOG"
echo "=== done $STAMP ==="
