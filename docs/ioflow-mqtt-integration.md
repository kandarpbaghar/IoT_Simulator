# IOFlow MQTT integration

IOFlow (`~/ai-projects/IOFlow`) now consumes this project's telemetry natively:
a message published to the Mosquitto broker fires an IOFlow flow, which can
write the reading to a database (e.g. TimescaleDB) via IOFlow's existing
`database_query` node — configured visually, no per-device glue code.

**Recommended setup — the MQTT application**: install the `mqtt` seed
application in IOFlow, create a connection with broker URL
`mqtt://80.225.229.166:1883` + the `hass` credentials (the *Test Connection*
button performs a real authenticated CONNECT), then use the application's
**New Message** event as the flow trigger and set a topic filter (e.g.
`home/#`). All flows on the same application share one broker connection —
MQTT pushes messages over it in real time; there is no polling. A standalone
`mqtt` trigger type (broker configured directly on the flow) also exists for
one-off flows.

Full documentation: `~/ai-projects/IOFlow/docs/mqtt-trigger.md`.

## Connecting IOFlow to this project's broker

- Broker: `80.225.229.166:1883` (cloud VM, see `config/simulator.env`).
- **Use the `hass` user for the IOFlow trigger, not `iot`.** The ACL
  (`config/mosquitto/config/acl`) makes `iot` publish-only — it cannot
  subscribe, so an IOFlow trigger configured with `iot` credentials connects
  fine but silently receives nothing. `hass` has `readwrite #`.
  (Hardening phase: add a dedicated `ioflow` user with
  `topic read home/#` / `topic read energy/#` instead of reusing `hass`.)
- Typical trigger config: topic filter `home/#` (temperature POC) or
  `energy/+/reading` (energy monitoring, later), QoS 1, payload format `json`.

## Flow variables

`input.topic`, `input.payload` (parsed JSON), `input.payload_raw`,
`input.qos`, `input.retain`, `input.timestamp`.

## Verified

2026-07-03: integration test
(`~/ai-projects/IOFlow/test-script/test_mqtt_trigger_e2e.py`) ran against the
live broker — publish as `iot`, subscribe as `hass` — 19/19 checks passed,
including malformed-payload fallback and auto-reconnect after a dropped
connection (<1s, requirement ≤30s).
