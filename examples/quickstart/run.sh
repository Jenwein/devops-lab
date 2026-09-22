#!/usr/bin/env bash
# Runs the end-to-end quickstart against a running platform. Idempotent.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
root="${DEVOPS_LAB_ROOT:-$HOME/devops-lab}"
if [[ ! -f "${root}/config/runtime.env" ]]; then
  echo "run scripts/lab setup and scripts/lab up first" >&2
  exit 1
fi
exec python3 "${repo}/examples/quickstart/quickstart.py" --repo "${repo}" --root "${root}" "$@"
