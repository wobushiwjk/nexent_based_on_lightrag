#!/usr/bin/env bash
# Snapshot the ModelArts environment so a before/after restart diff shows
# exactly what a stop/start changes.  Run it in the PyTorch-2.6.0 env:
#
#   bash /home/ma-user/work/env_snapshot.sh before
#   # stop and start the notebook instance
#   bash /home/ma-user/work/env_snapshot.sh after
#   diff -u /home/ma-user/work/env_before.txt /home/ma-user/work/env_after.txt
#   diff -u /home/ma-user/work/env_before_freeze.txt \
#           /home/ma-user/work/env_after_freeze.txt
#
# The script only reads state; it never installs or patches anything.  Save it
# under /home/ma-user/work (the persistent volume), not in the repo checkout.
set -u

LABEL="${1:-snapshot}"
REPO=/home/ma-user/work/LightRAG
OUT="/home/ma-user/work/env_${LABEL}.txt"
SITE="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"

{
  echo "== label =="; echo "$LABEL"
  echo; echo "== when =="; date -Is; ps -o lstart= -p 1; uname -m
  echo; echo "== python =="; command -v python; python -V
  echo "conda_env=${CONDA_DEFAULT_ENV:-none}"

  echo; echo "== versions =="
  python -m pip show torch torch_npu triton transformers peft trl \
    llamafactory accelerate datasets 2>/dev/null \
    | grep -E '^(Name|Version):'

  echo; echo "== entry points =="
  command -v llamafactory-cli || echo "llamafactory-cli MISSING"

  echo; echo "== site-packages =="; echo "SITE=$SITE"
  stat -c '%y  %n' "$SITE/triton" "$SITE/torch" "$SITE/torch_npu" \
    "$SITE/transformers" "$SITE/peft" 2>/dev/null

  echo; echo "== triton AttrsDescriptor =="
  python - <<'PY'
try:
    import triton.backends.compiler as backends
    print("backends.compiler:", hasattr(backends, "AttrsDescriptor"))
except Exception as exc:  # noqa: BLE001 - report whatever the import does
    print("backends.compiler import failed:", exc)
try:
    import triton.compiler.compiler as compiler
    print("compiler.compiler:", hasattr(compiler, "AttrsDescriptor"))
except Exception as exc:  # noqa: BLE001
    print("compiler.compiler import failed:", exc)
PY

  echo; echo "== torch_npu import (autoload on, no env override) =="
  unset TORCH_DEVICE_BACKEND_AUTOLOAD
  python -c "import torch, torch_npu; print(torch.__version__, torch_npu.__version__, torch.npu.is_available())" 2>&1 | tail -12

  echo; echo "== repo =="
  git -C "$REPO" rev-parse --short HEAD 2>/dev/null
  git -C "$REPO" status --short 2>/dev/null | head -10

  echo; echo "== data rows =="
  wc -l "$REPO"/training/data/w7_b/train.jsonl \
        "$REPO"/training/data/w7_b/val.jsonl \
        "$REPO"/training/data/w7_b/test.jsonl 2>/dev/null

  echo; echo "== sizes =="
  du -sh "$REPO"/training/models/Qwen2.5-1.5B-Instruct \
        "$REPO"/training/saves/qwen2.5-1.5b-evidence-lora 2>/dev/null

  echo; echo "== required files =="
  cd "$REPO" 2>/dev/null || { echo "REPO MISSING: $REPO"; exit 0; }
  for path in \
    scripts/evaluate_distillation.py \
    lightrag/fault_tree/distillation/__init__.py \
    lightrag/fault_tree/distillation/labels.py \
    lightrag/fault_tree/distillation/metrics.py \
    lightrag/fault_tree/distillation/teacher.py \
    training/run_w7_eval.sh \
    tests/domain/test_distillation_metrics.py \
    training/models/Qwen2.5-1.5B-Instruct/config.json \
    training/models/Qwen2.5-1.5B-Instruct/model.safetensors \
    training/data/w7_b/test.jsonl \
    training/data/w7_b/test_meta.jsonl \
    training/data/w7_b/manifest.json \
    training/saves/qwen2.5-1.5b-evidence-lora/adapter_config.json \
    training/saves/qwen2.5-1.5b-evidence-lora/adapter_model.safetensors; do
    if [ -e "$path" ]; then
      echo "OK   $path"
    else
      echo "MISS $path"
    fi
  done

  echo; echo "== disk =="; df -h "$REPO" | tail -1
  echo; echo "== npu =="
  if command -v npu-smi >/dev/null 2>&1; then
    npu-smi info | head -12
  else
    echo "npu-smi not found"
  fi
} 2>&1 | tee "$OUT"

python -m pip freeze > "/home/ma-user/work/env_${LABEL}_freeze.txt" 2>/dev/null
echo
echo "wrote $OUT"
echo "wrote /home/ma-user/work/env_${LABEL}_freeze.txt"
