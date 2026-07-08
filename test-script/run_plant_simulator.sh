#!/usr/bin/env bash
# Run the full 200-device plant fleet simulator against the cloud broker.
# Usage: ./test-script/run_plant_simulator.sh [extra plant_sim args...]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -f "$ROOT/config/plant_simulator.env" ]]; then
  echo "ERROR: config/plant_simulator.env not found (copy from .env.example)" >&2
  exit 1
fi
set -a; . "$ROOT/config/plant_simulator.env"; set +a

cd "$ROOT/simulator"
exec "$ROOT/venv/bin/python" -m plant_sim "$@"
