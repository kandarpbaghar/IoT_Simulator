# MQTT Temperature Simulator

Publishes dummy temperature readings over MQTT to validate the whole pipeline
(device → Mosquitto broker → Home Assistant) before a real sensor exists.

Location: `simulator/temp_simulator.py`. Verified working end-to-end against a local
authenticated Mosquitto broker (see `test-script/test_simulator_local.sh`).

## What it does
- Connects to an MQTT broker (username/password; TLS hooks included but off for POC).
- Publishes a **Home Assistant MQTT Discovery** config once on connect, so the sensor
  auto-appears in HA — no manual YAML.
- Publishes a temperature reading every `--interval` seconds (default **300 = 5 min**).
- Publishes `online`/`offline` availability, with a **Last Will** so the broker marks
  it offline if it crashes.
- Auto-reconnects with backoff; logs everything; exits cleanly on Ctrl-C.

## Topics
| Purpose | Topic |
|---------|-------|
| Discovery | `homeassistant/sensor/<device_id>/temperature/config` |
| State (the value) | `home/<device_id>/temperature` |
| Availability | `home/<device_id>/status` |

## Run it

```bash
# 1. config
cp config/simulator.env.example config/simulator.env
# edit MQTT_HOST / MQTT_USERNAME / MQTT_PASSWORD to match your broker

# 2. load config + run
set -a; . config/simulator.env; set +a
./venv/bin/python simulator/temp_simulator.py -v

# fast test loop (every 5 s instead of 5 min)
./venv/bin/python simulator/temp_simulator.py --host <broker> \
  --username iot --password <pw> --interval 5 -v

# single reading and exit (handy for CI/smoke tests)
./venv/bin/python simulator/temp_simulator.py --host <broker> \
  --username iot --password <pw> --once -v
```

All flags also read from env vars (see `config/simulator.env.example`);
precedence is **CLI > env > default**.

## Local self-test (no Pi, no cloud needed)
```bash
./test-script/test_simulator_local.sh
```
Spins up a local authenticated Mosquitto broker on `127.0.0.1:1883`, runs the
simulator once, and asserts the discovery/state/availability messages all arrive.
Outputs land in `test-script/output/` (gitignored).

## Production TODO (wired, currently off)
- `--tls --port 8883 --ca-cert ... --client-cert ... --client-key ...` for encrypted
  transport + per-device client certificates. Turn on in the security-hardening phase.
