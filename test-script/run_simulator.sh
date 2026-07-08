#!/usr/bin/env bash
# Run the MQTT temperature simulator against the configured broker.
#
# Loads config/simulator.env (broker host/creds, publish interval, temp model)
# and launches simulator/temp_simulator.py from the project venv.
#
# Usage:
#   test-script/run_simulator.sh                 # publish forever (interval from env)
#   test-script/run_simulator.sh --once -v       # single reading, verbose, then exit
#   test-script/run_simulator.sh --interval 5    # override any simulator flag
#
# Any extra args are passed straight through to the simulator, overriding the env.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/config/simulator.env"
PY="$ROOT/venv/bin/python"
SIM="$ROOT/simulator/temp_simulator.py"

# --- Preconditions ---------------------------------------------------------- #
if [[ ! -x "$PY" ]]; then
  echo "ERROR: project venv not found at $PY" >&2
  echo "Create it with: python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
if [[ ! -f "$SIM" ]]; then
  echo "ERROR: simulator not found at $SIM" >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: config not found at $ENV_FILE" >&2
  echo "Copy the template: cp config/simulator.env.example config/simulator.env  (then edit)" >&2
  exit 1
fi

# --- Load config (exports every var defined in the env file) ---------------- #
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

echo "Broker : ${MQTT_HOST:-localhost}:${MQTT_PORT:-1883}  (user=${MQTT_USERNAME:-none}, tls=${USE_TLS:-false})"
echo "Device : ${DEVICE_ID:-sim_temp_01}  interval=${PUBLISH_INTERVAL:-300}s"
echo "Ctrl-C to stop."
echo

# exec so signals (Ctrl-C / SIGTERM) reach the Python process directly.
exec "$PY" "$SIM" "$@"
