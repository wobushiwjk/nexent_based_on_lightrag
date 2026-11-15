#!/usr/bin/env bash
# Reproduce the independently sourced W9 corpus and its deterministic baseline.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python}"

"$PYTHON" scripts/fetch_w9_sources.py
"$PYTHON" scripts/build_w9_web_manifest.py
"$PYTHON" scripts/evaluate_w9_unseen_sources.py
