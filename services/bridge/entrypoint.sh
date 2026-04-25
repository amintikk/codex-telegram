#!/bin/bash.container
set -euo pipefail

if [[ ! -d "${RUNS_DIR:-/data/runs}" ]]; then
  mkdir -p "${RUNS_DIR:-/data/runs}"
fi

exec python3 /app/app.py
