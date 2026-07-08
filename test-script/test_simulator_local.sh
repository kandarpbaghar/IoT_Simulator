#!/usr/bin/env bash
# Local end-to-end test of the MQTT temperature simulator.
# Stands up an authenticated Mosquitto broker on localhost, subscribes, runs the
# simulator once, and shows the captured messages. Non-destructive; tears down after.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/test-script/output"
MOSQ="/opt/homebrew/opt/mosquitto/sbin/mosquitto"
USER="iot"; PASS="changeme"; PORT=1883
mkdir -p "$OUT"

CONF="$OUT/local_broker.conf"
PWFILE="$OUT/local_passwd"
RECV="$OUT/received.log"
BROKER_LOG="$OUT/broker.log"
rm -f "$RECV"

cleanup() {
  [[ -n "${SUB_PID:-}" ]] && kill "$SUB_PID" 2>/dev/null || true
  [[ -n "${BROKER_PID:-}" ]] && kill "$BROKER_PID" 2>/dev/null || true
}
trap cleanup EXIT

# 1. password file + config (auth required, localhost only)
mosquitto_passwd -c -b "$PWFILE" "$USER" "$PASS"
cat > "$CONF" <<EOF
listener $PORT 127.0.0.1
allow_anonymous false
password_file $PWFILE
EOF

# 2. start broker
"$MOSQ" -c "$CONF" >"$BROKER_LOG" 2>&1 &
BROKER_PID=$!
sleep 1
kill -0 "$BROKER_PID" 2>/dev/null || { echo "Broker failed to start:"; cat "$BROKER_LOG"; exit 1; }
echo "Broker up (pid $BROKER_PID) on 127.0.0.1:$PORT"

# 3. subscribe to everything in the background
mosquitto_sub -h 127.0.0.1 -p "$PORT" -u "$USER" -P "$PASS" \
  -t '#' -v >"$RECV" 2>&1 &
SUB_PID=$!
sleep 0.5

# 4. run the simulator once
echo "--- running simulator (--once) ---"
"$ROOT/venv/bin/python" "$ROOT/simulator/temp_simulator.py" \
  --host 127.0.0.1 --port "$PORT" --username "$USER" --password "$PASS" \
  --device-id sim_temp_01 --once -v

sleep 1
echo
echo "=== Messages received by subscriber ==="
cat "$RECV"
echo "======================================="

# 5. sanity assertions
grep -q "home/sim_temp_01/temperature" "$RECV" \
  && echo "PASS: temperature reading received" \
  || { echo "FAIL: no temperature reading"; exit 1; }
grep -q "homeassistant/sensor/sim_temp_01/temperature/config" "$RECV" \
  && echo "PASS: HA discovery config received" \
  || { echo "FAIL: no discovery config"; exit 1; }
grep -q "home/sim_temp_01/status online" "$RECV" \
  && echo "PASS: availability 'online' received" \
  || { echo "FAIL: no availability message"; exit 1; }
echo "ALL CHECKS PASSED"
