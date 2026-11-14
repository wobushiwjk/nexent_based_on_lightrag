#!/usr/bin/env bash
# Serve the merged W7 student through an OpenAI-compatible local endpoint.
#
# Preferred: vLLM (fast, GPU). Fallback: LLaMA-Factory API (HF backend).
# This script deliberately has no external-model fallback.  User uploads must
# stay on the trained local student.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${W7_STUDENT_MODEL_DIR:-$REPO_ROOT/training/qwen2.5-1.5b-evidence-merged}"
MODEL_NAME="${W7_STUDENT_MODEL:-industrial-extractor}"
HOST="${W7_STUDENT_HOST:-0.0.0.0}"
PORT="${W7_STUDENT_PORT:-8000}"

if [[ ! -f "$MODEL_DIR/config.json" || ! -f "$MODEL_DIR/model.safetensors" ]]; then
  cat >&2 <<EOF
W7 student model is missing at:
  $MODEL_DIR

Extract the delivered archive first:
  tar -xf training/qwen2.5-1.5b-evidence-merged.tar -C training/
EOF
  exit 2
fi

if [[ -n "${W7_STUDENT_SERVE_CMD:-}" ]]; then
  exec bash -lc "$W7_STUDENT_SERVE_CMD"
fi

if command -v vllm >/dev/null 2>&1; then
  exec vllm serve "$MODEL_DIR" \
    --served-model-name "$MODEL_NAME" \
    --host "$HOST" \
    --port "$PORT" \
    --generation-config vllm
fi

if command -v llamafactory-cli >/dev/null 2>&1; then
  export API_MODEL_NAME="$MODEL_NAME"
  exec llamafactory-cli api \
    --model_name_or_path "$MODEL_DIR" \
    --template qwen \
    --infer_backend hf \
    --host "$HOST" \
    --port "$PORT"
fi

cat >&2 <<'EOF'
No local OpenAI-compatible model server was found.

Install one of:
  - vLLM:            pip install vllm
  - LLaMA-Factory:   pip install llamafactory

Then run this script again, or set W7_STUDENT_SERVE_CMD to the exact local
serving command.  Do not point W7_STUDENT_ENDPOINT at an external API.
EOF
exit 2
