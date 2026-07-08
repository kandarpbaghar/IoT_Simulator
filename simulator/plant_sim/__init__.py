"""
plant_sim — multi-plant energy & plant-health MQTT device fleet simulator.

Simulates ~200 industrial devices (energy meters, vibration sensors, compressors,
chillers, boilers, VFDs, UPS, flow/level/env/air-quality sensors, production line
counters) across 4 plants, publishing realistic JSON telemetry to an MQTT broker.

A built-in anomaly engine injects real-life failure patterns (bearing wear,
voltage sags, air leaks, sensor flatlines, dropouts, clock skew, reboots, ...)
so a downstream monitoring system (IOFlow, phase 2) has genuine exceptions to
detect. Ground-truth anomaly events are published to a meta topic and appended
to a JSONL file for later validation.

Run:  python -m plant_sim --help   (from the simulator/ directory)
"""

__version__ = "1.0.0"
