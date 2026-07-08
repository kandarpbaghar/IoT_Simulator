# Plant Fleet Simulator (`simulator/plant_sim/`)

Simulates **200 industrial IoT devices** across **4 plants** of a manufacturing
company, publishing realistic energy + plant-health telemetry to the cloud
Mosquitto broker. Built for phase 2: IOFlow subscribes, detects exceptions, and
writes to a time-series database.

```
./test-script/run_plant_simulator.sh              # full 200-device fleet
./test-script/test_plant_simulator.sh             # offline smoke test (dry-run)
./venv/bin/python test-script/test_plant_sim_e2e.py   # live broker round-trip test
```

## Plants

| plant | code | country | electrical | shifts | climate (July) |
|---|---|---|---|---|---|
| `pune` | PUN | IN | 415 V / 50 Hz | 3-shift 24×7 | 27 °C ± 4 |
| `chennai` | CHN | IN | 415 V / 50 Hz | 3-shift 24×7 | 31 °C ± 4 |
| `frankfurt` | FRA | DE | 400 V / 50 Hz | 2-shift (nights ~idle) | 21 °C ± 6 |
| `houston` | HOU | US | 480 V / 60 Hz | 3-shift 24×7 | 31 °C ± 5 |

Every plant has its own timezone, shift schedule (lunch dips, shift-change dips,
Saturday reduced, Sunday maintenance-only), day-to-day production variation, and
occasional **plant-wide grid sags/swells** that hit all its energy meters at once.

## Device mix (50 per plant × 4 = 200)

| type | per plant | id example | interval | key metrics |
|---|---|---|---|---|
| `energy_meter` | 12 | PUN-EM-01 | 15 s | V/I per phase, kW, kVAR, PF, Hz, THD, imbalance, kWh totalizer |
| `vibration` | 8 | PUN-VIB-01 | 30 s | RMS velocity (ISO 10816), peak g, bearing temp, battery, RSSI |
| `env_sensor` | 6 | PUN-ENV-01 | 60 s | temp, RH, dew point, battery |
| `vfd` | 5 | PUN-VFD-01 | 15 s | output Hz/rpm/A/kW, torque, DC bus, heatsink temp, fault code |
| `flow_meter` | 4 | PUN-FLM-01 | 30 s | flow, totalizer, pressure (water / compressed air / gas) |
| `air_compressor` | 3 | PUN-CMP-01 | 20 s | discharge bar, flow, current, oil temp, load duty, run hours |
| `tank_level` | 3 | PUN-TNK-01 | 60 s | level %, volume, medium (diesel/water/coolant/oil) |
| `production_counter` | 3 | PUN-LIN-01 | 10 s | line state, good/reject counters, rate vs target, cycle time |
| `chiller` | 2 | PUN-CHL-01 | 30 s | CHW supply/return, flow, kW, COP, condenser/evap pressure |
| `air_quality` | 2 | PUN-AQM-01 | 60 s | CO₂, PM2.5/PM10, CO, TVOC |
| `boiler` | 1 | PUN-BLR-01 | 20 s | steam bar/flow, drum level, O₂, stack temp, efficiency |
| `ups` | 1 | PUN-UPS-01 | 60 s | in/out volts, load, battery charge/health, runtime, on-battery |

~5 % of devices are **chronic** (4× anomaly rate) and ~8 % are **flaky**
(6× data-quality glitch rate) — every real plant has those.

## Topics (all under `home/#` — the `iot` broker user is write-only there)

```
home/plants/{plant}/{device_type}/{device_id}/data     lean telemetry JSON (QoS 1)
home/plants/{plant}/{device_type}/{device_id}/meta     retained birth msg (master data)
home/plants/{plant}/{device_type}/{device_id}/status   retained online/offline + LWT
home/plants/_meta/fleet                                retained fleet manifest JSON
home/plants/_meta/anomaly                              anomaly ground-truth events
```

> Subscribers (IOFlow, mosquitto_sub) must use the `hass` user — `iot`
> authenticates but silently receives nothing (publish-only ACL).

## Payload contract (lean telemetry + retained birth)

Device **identity lives in the topic**; telemetry is lean:

```json
{
  "ts": "2026-07-04T05:00:00.946Z",
  "ts_epoch_ms": 1783141200946,
  "seq": 15815, "uptime_s": 2987059,
  "metrics": { "temperature_c": 24.99, "humidity_pct": 54.7,
               "dew_point_c": 15.25, "battery_pct": 77.9 },
  "status": { "state": "ok", "alarms": [] },
  "rssi_dbm": -54
}
```

**Master data** is a *retained* birth message on `.../{device_id}/meta`,
republished on **every** (re)connect and after simulated reboots — so a late
subscriber (IOFlow) receives all births immediately upon subscribing, and the
broker's retained store self-heals if it is ever lost:

```json
{
  "ts": "2026-07-04T07:31:02.114Z",
  "device_id": "HOU-ENV-06", "plant": "houston", "area": "weld-shop",
  "device_type": "env_sensor", "vendor": "Efento", "model": "NB-TH",
  "fw": "1.0.1", "wireless": true, "interval_s": 60.0, "schema": "json-v1"
}
```

The broker (`persistence true` + data volume) keeps retained messages across
restarts. To decommission a device, publish an empty retained payload to its
`meta` and `status` topics.

`rssi_dbm`/`battery_pct` appear only on wireless sensors. `status.alarms` carries
device-side alarm codes (`LOW_PF`, `ISO10816_ZONE_D`, `F07_OVERCURRENT`,
`LOW_WATER`, `ON_BATTERY`, ...). Counters (`energy_kwh`, `good_count`,
`totalizer_m3`, `run_hours`) are monotonic except during injected faults.

## Injected anomalies (what monitoring should catch)

**Generic (any device):** `dropout` (silent 2–25 min → stale data), `reboot`
(gap + seq/uptime reset), `stuck` (all metrics flatlined), `sensor_drift` (one
metric drifts out of band), `clock_skew` (timestamps minutes off).

**Physical, per type:** energy meter — `pf_low`, `idle_load_high` (night waste),
`phase_imbalance`, `ct_fault`; vibration — `bearing_wear` (hours-long ISO C→D
ramp), `imbalance`, `looseness`; env — `hvac_fail`; compressor — `air_leak`
(duty↑ pressure↓), `oil_overheat`, `short_cycle`; chiller — `refrigerant_low`
(ΔT collapse, COP↓), `condenser_fouling`; flow — `leak` (night baseflow),
`stuck_totalizer`, `reverse_flow`; tank — `tank_leak`, `overfill_glitch`;
boiler — `efficiency_loss` (stack temp↑ O₂↑), `low_water`, `pressure_hunt`;
VFD — `overtemp_derate`, `overcurrent_trip` (F07), `fan_fail`; UPS —
`mains_fail` (battery draining), `battery_degraded`; line — `high_reject`,
`microstoppages`, `jam`; air quality — `dust_event`, `co_spike`.

**One-shot data-quality glitches:** null metric values, missing fields, sentinel
outliers (`32767`, `-999.9`, `65535`, `-3276.8`), seq gaps, duplicate messages,
a few devices with permanently broken NTP (±minutes clock skew).

**Ground truth** for every stateful anomaly (start/end, device, kind, severity,
duration, expected symptom) is published to `home/plants/_meta/anomaly` and
appended to `test-script/output/anomaly_events.jsonl` — use it in phase 2 to
score what the monitoring system actually detected. The fleet inventory is in
`test-script/output/fleet_manifest.json` and retained on `_meta/fleet`.

## Configuration

`config/plant_simulator.env` (gitignored; example alongside). Key knobs:

| env / flag | default | meaning |
|---|---|---|
| `FLEET_DEVICES` / `--devices` | 0 = all 200 | balanced subset for testing |
| `INTERVAL_SCALE` / `--interval-scale` | 1.0 | 2.0 halves message rate (~9 msg/s at 1.0) |
| `ANOMALY_RATE` / `--anomaly-rate` | 1.5 | anomaly events per device per day |
| `SIM_SEED` / `--seed` | 42 | deterministic fleet layout |
| `SIM_DURATION` / `--duration` | 0 | auto-stop after N seconds |
| `--dry-run` | off | print payloads, no broker |

Each device is its own MQTT client (unique client-id, LWT, retained
availability), connections staggered 25/s. Runtime stats + every anomaly
injection are logged each minute.

## Phase 2 notes (IOFlow)

- Subscribe as `hass` with a single covering filter `home/plants/#`
  (nested filters cause duplicate deliveries — see `docs/ioflow-mqtt-integration.md`).
- The `_meta` topics let a validation flow compare detections vs injected truth.
- Resolve device context from the **topic** (`plant`/`device_type`/`device_id`)
  or the retained `/meta` birth; store `metrics.*` as columns/fields keyed on
  (`device_id`, `ts_epoch_ms`).
- Telemetry for a `device_id` with no known birth should raise an
  "unregistered device" data-quality exception (park, don't drop) — births
  arriving later reconcile it. `_meta/fleet` is the bulk backstop inventory.
- Late/duplicate/out-of-order data is *intentional* — the ingest pipeline should
  dedupe on (`device_id`, `seq`) and tolerate `ts` ≠ arrival time.
