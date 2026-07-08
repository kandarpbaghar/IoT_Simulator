#!/usr/bin/env bash
# Quick offline smoke test: 8 devices, no broker, payloads printed to stdout.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/simulator"
exec "$ROOT/venv/bin/python" -m plant_sim --dry-run --devices 8 \
  --interval-scale 0.1 --duration 12 --anomaly-rate 500 "$@"
