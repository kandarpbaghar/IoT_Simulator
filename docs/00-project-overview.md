# IoT Temperature Telemetry — Project Overview

A Raspberry Pi reads a temperature sensor and publishes the value **every 5 minutes**
over **MQTT** to a **Home Assistant** instance in the cloud that runs the
**Mosquitto** MQTT broker.

Before wiring a real sensor, a **simulator** publishes dummy temperature readings
over the same MQTT pipeline so the whole path can be validated end-to-end.

## Architecture

```
┌────────────────────┐         MQTT (publish)         ┌──────────────────────────┐
│  Raspberry Pi      │  temp/livingroom/temperature   │  Cloud VM (Home Assistant)│
│                    │ ─────────────────────────────► │                          │
│  sensor / simulator│                                │  Mosquitto MQTT broker    │
│  Python + paho-mqtt│                                │  Home Assistant Core      │
└────────────────────┘                                └──────────────────────────┘
        publishes every 5 min                          subscribes / auto-discovers
```

- **Transport:** MQTT (paho-mqtt on the Pi, Mosquitto broker on the cloud).
- **Interval:** one reading every 5 minutes.
- **Discovery:** Home Assistant MQTT Discovery so the sensor appears automatically.

## Phased plan

| Phase | Goal | Status |
|-------|------|--------|
| 1 | Bring up the Raspberry Pi (OS flash, network, SSH, usable with or without a monitor) | ⏸ blocked — SD card already has Home Assistant OS; awaiting decision |
| 2 | Stand up Home Assistant + Mosquitto broker (cloud VM **or** on the Pi) | pending |
| 3 | Connectivity: simulator → MQTT → Home Assistant, then swap in the real sensor | pending |
| — | MQTT temperature **simulator** | ✅ built & tested locally (`docs/simulator.md`) |

## Security roadmap (POC → production)

This is a **POC**, so we start with the minimum and layer security in later. Nothing
below is skipped permanently — each item has a target phase to turn it on.

| Concern | POC (now) | Production (target) |
|---------|-----------|---------------------|
| MQTT auth | anonymous **off**, single username/password | per-device credentials, ACLs per topic |
| MQTT transport | plaintext TCP `1883` on a trusted/VPN network | TLS `8883` with CA + server cert |
| Device identity | shared password | X.509 client certificates per device |
| Pi access | password SSH | SSH keys only, password login disabled |
| Broker exposure | bound to VPN/private network | firewall + TLS, never plain `1883` on public IP |
| Secrets | local config file | secrets manager / HA `secrets.yaml`, not in git |
| OS | default | unattended-upgrades, fail2ban, minimal packages |

> POC rule of thumb: even in the POC we still use a **username + password** on the
> broker (not anonymous) and keep `1883` off the public internet. That costs nothing
> and avoids building bad habits.

## Repo layout

```
IoT/
├── docs/                 # all documentation (this folder)
├── config/               # broker / HA config snippets (mosquitto.conf, etc.)
├── simulator/            # Python MQTT temperature simulator
├── test-script/          # throwaway test scripts
│   └── output/           # generated test outputs (gitignored)
└── venv/                 # Python 3.12 virtualenv (project root)
```
