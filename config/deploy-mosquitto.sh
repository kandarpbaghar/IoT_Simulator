#!/usr/bin/env bash
# Deploy Mosquitto as a Docker container next to an existing Home Assistant
# Container install. Run this ON THE CLOUD VM (OCI ARM). Idempotent: re-running
# recreates the broker with the same config.
#
#   Usage:   bash deploy-mosquitto.sh
#   Override creds:  HASS_MQTT_PASSWORD=... IOT_MQTT_PASSWORD=... bash deploy-mosquitto.sh
set -euo pipefail

# --- POC credentials (override via env) ---------------------------------------
HASS_MQTT_USER="${HASS_MQTT_USER:-hass}"
HASS_MQTT_PASSWORD="${HASS_MQTT_PASSWORD:-HassBroker@26}"
IOT_MQTT_USER="${IOT_MQTT_USER:-iot}"
IOT_MQTT_PASSWORD="${IOT_MQTT_PASSWORD:-IoTsensor@26}"
IMAGE="eclipse-mosquitto:2"
BASE="$HOME/mosquitto"
CFG="$BASE/config"

log(){ printf '\n=== %s ===\n' "$*"; }

# --- 0. prerequisites ---------------------------------------------------------
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found on this VM."; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: cannot talk to Docker (need sudo? add your user to the docker group)."; exit 1; }

# --- 1. config + acl ----------------------------------------------------------
log "Writing config to $CFG"
mkdir -p "$CFG" "$BASE/data" "$BASE/log"

cat > "$CFG/mosquitto.conf" <<'EOF'
persistence true
persistence_location /mosquitto/data/
log_dest stdout
log_type warning
log_type notice
log_type information
listener 1883
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl
EOF

cat > "$CFG/acl" <<'EOF'
# Least privilege
user hass
topic readwrite #

user iot
topic write home/#
topic write homeassistant/#
EOF

# --- 2. password file (recreated each run for a known state) ------------------
log "Creating logins ($HASS_MQTT_USER, $IOT_MQTT_USER)"
rm -f "$CFG/passwd"
docker run --rm -v "$CFG:/mosquitto/config" "$IMAGE" \
  mosquitto_passwd -c -b /mosquitto/config/passwd "$HASS_MQTT_USER" "$HASS_MQTT_PASSWORD"
docker run --rm -v "$CFG:/mosquitto/config" "$IMAGE" \
  mosquitto_passwd -b /mosquitto/config/passwd "$IOT_MQTT_USER" "$IOT_MQTT_PASSWORD"

# --- 3. (re)start the broker --------------------------------------------------
log "Starting Mosquitto container"
docker rm -f mosquitto >/dev/null 2>&1 || true
docker run -d --name mosquitto --restart unless-stopped \
  -p 1883:1883 \
  -v "$CFG:/mosquitto/config" \
  -v "$BASE/data:/mosquitto/data" \
  -v "$BASE/log:/mosquitto/log" \
  "$IMAGE" >/dev/null
sleep 2
docker ps --filter name=mosquitto --format '  {{.Names}}  {{.Status}}  {{.Ports}}'
echo "--- recent logs ---"; docker logs --tail 8 mosquitto

# --- 4. self-test: iot publishes, hass receives (through the ACL) -------------
log "Self-test (publish as $IOT_MQTT_USER, subscribe as $HASS_MQTT_USER)"
RESULT=$(docker exec mosquitto sh -c "
  mosquitto_sub -h localhost -u '$HASS_MQTT_USER' -P '$HASS_MQTT_PASSWORD' -t 'home/#' -C 1 -W 5 -v > /tmp/sub.out 2>&1 &
  sleep 1
  mosquitto_pub -h localhost -u '$IOT_MQTT_USER' -P '$IOT_MQTT_PASSWORD' -t 'home/selftest' -m 'ok' 2>&1
  sleep 2
  cat /tmp/sub.out
" || true)
echo "  $RESULT"
if echo "$RESULT" | grep -q "home/selftest ok"; then
  echo "  PASS: broker auth + ACL working."
else
  echo "  WARN: self-test did not confirm delivery — check logs above."
fi

# --- 5. next steps ------------------------------------------------------------
cat <<EOF

=== DONE — Mosquitto is running on this VM (port 1883) ===

Connect Home Assistant (web UI):
  Settings -> Devices & services -> Add Integration -> MQTT
    Broker:   172.17.0.1     (Docker bridge gateway; try 'mosquitto' if same compose net,
                              or 127.0.0.1 if HA runs with host networking)
    Port:     1883
    Username: $HASS_MQTT_USER
    Password: $HASS_MQTT_PASSWORD

Connect the Pi / simulator (from outside):
    Host: <this VM public IP>   Port: 1883
    Username: $IOT_MQTT_USER    Password: $IOT_MQTT_PASSWORD
  -> Make sure inbound TCP 1883 is allowed in the OCI Security List / NSG
     AND in the instance firewall (Oracle images ship iptables closed):
       sudo iptables -I INPUT 6 -p tcp --dport 1883 -j ACCEPT      # quick POC
       # persist per your distro (netfilter-persistent / firewalld)

Security (POC): 1883 is plaintext + password only. Harden later with TLS on 8883
and per-device client certs (see docs/00-project-overview.md).
EOF
