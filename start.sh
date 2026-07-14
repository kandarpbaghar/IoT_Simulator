#!/usr/bin/env bash
# Start / stop the plant fleet MQTT simulator.
#
# Usage:
#   ./start.sh start                 # launch in background (default if no command)
#   ./start.sh stop                  # graceful shutdown (SIGTERM -> devices go offline)
#   ./start.sh restart
#   ./start.sh status
#   ./start.sh logs                  # follow the log (Ctrl-C to stop following)
#
# Broker host/credentials and publish rate come from config/plant_simulator.env
# (gitignored). Extra args after the command pass straight through to plant_sim:
#   ./start.sh start --interval-scale 5 --devices 20
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT/config/plant_simulator.env"
PY="$ROOT/venv/bin/python"
OUT_DIR="$ROOT/test-script/output"
PID_FILE="$OUT_DIR/plant_sim.pid"
LOG_FILE="$OUT_DIR/plant_sim.log"

mkdir -p "$OUT_DIR"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid; pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start() {
  if is_running; then
    echo "Already running (pid $(cat "$PID_FILE"))."; return 0
  fi
  [[ -x "$PY" ]] || { echo "ERROR: venv not found at $PY" >&2
    echo "  Create it: python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2; exit 1; }
  [[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE not found (copy from config/plant_simulator.env.example)" >&2; exit 1; }

  # export every var in the env file so plant_sim (reads os.environ) sees them
  set -a; # shellcheck disable=SC1090
  . "$ENV_FILE"; set +a

  echo "Starting plant simulator -> ${MQTT_HOST:-localhost}:${MQTT_PORT:-1883}" \
       "(scale=${INTERVAL_SCALE:-1.0}, devices=${FLEET_DEVICES:-all})"
  # exec inside the subshell so $! is the python PID and SIGTERM reaches it directly
  ( cd "$ROOT/simulator" && exec "$PY" -m plant_sim -v "$@" ) >"$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1
  if is_running; then
    echo "Started (pid $(cat "$PID_FILE")). Logs: $LOG_FILE"
  else
    echo "Failed to start. Last log lines:" >&2; tail -n 20 "$LOG_FILE" >&2
    rm -f "$PID_FILE"; exit 1
  fi
}

stop() {
  if ! is_running; then echo "Not running."; rm -f "$PID_FILE"; return 0; fi
  local pid; pid="$(cat "$PID_FILE")"
  echo "Stopping (pid $pid) — graceful shutdown (devices -> offline)..."
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 40); do is_running || break; sleep 0.5; done
  if is_running; then
    echo "Did not exit in time; sending SIGKILL."; kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "Stopped."
}

CMD="${1:-start}"; shift 2>/dev/null || true
case "$CMD" in
  start)   start "$@" ;;
  stop)    stop ;;
  restart) stop; start "$@" ;;
  status)  if is_running; then echo "Running (pid $(cat "$PID_FILE"))."; else echo "Not running."; fi ;;
  logs)    tail -f "$LOG_FILE" ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs} [extra plant_sim args]" >&2; exit 1 ;;
esac
