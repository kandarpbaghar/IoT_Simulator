# IoT Temperature Telemetry (POC)

Raspberry Pi → MQTT → Home Assistant (cloud, Mosquitto broker). Publishes a
temperature reading every 5 minutes. A simulator validates the pipeline before the
real sensor is wired.

## Docs
- [Project overview & architecture](docs/00-project-overview.md)
- [Phase 1 — Raspberry Pi setup](docs/01-raspberry-pi-setup.md)
- [MQTT temperature simulator](docs/simulator.md) — ✅ built & tested
- Phase 2 — Home Assistant + Mosquitto (coming next)
- Phase 3 — Connectivity: simulator → broker → Home Assistant (coming next)

## Status
- ✅ **Simulator** built and verified end-to-end against a local Mosquitto broker.
- ⏸ **Phase 1 blocked on a decision:** the inserted SD card already contains
  **Home Assistant OS**. Choose whether to run HA on this Pi (keep the card) or
  reflash it as a sensor node (erases HAOS) before we continue.

## Quick test (no hardware needed)
```bash
./test-script/test_simulator_local.sh
```
