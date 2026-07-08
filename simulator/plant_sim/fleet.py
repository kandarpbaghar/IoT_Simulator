"""Fleet builder — deterministic layout of 200 devices across 4 plants."""
from __future__ import annotations

import logging
import random

from .devices import (DEVICE_CLASSES, AirCompressor, AirQuality, Boiler, Chiller,
                      Device, EnergyMeter, EnvSensor, FlowMeter, ProductionLine,
                      TankLevel, UPS, VFD, VibrationSensor)
from .signals import Plant, build_plants

LOG = logging.getLogger("plant-sim.fleet")

# device mix per plant — 50 devices/plant x 4 plants = 200
PLANT_MIX: list[tuple[type[Device], int]] = [
    (EnergyMeter, 12),
    (VibrationSensor, 8),
    (EnvSensor, 6),
    (VFD, 5),
    (FlowMeter, 4),
    (AirCompressor, 3),
    (TankLevel, 3),
    (ProductionLine, 3),
    (Chiller, 2),
    (AirQuality, 2),
    (Boiler, 1),
    (UPS, 1),
]

CHRONIC_FRACTION = 0.05    # devices with 4x anomaly rate (the plant's "problem children")
FLAKY_FRACTION = 0.08      # devices with noisy comms / bad data quality


def build_fleet(seed: int, anomaly_rate: float, interval_scale: float,
                topic_prefix: str, max_devices: int | None = None
                ) -> tuple[list[Plant], list[Device]]:
    rng = random.Random(seed)
    plants = build_plants(seed)
    devices: list[Device] = []

    for plant in plants:
        for cls, count in PLANT_MIX:
            for n in range(1, count + 1):
                dev_rng = random.Random(rng.randrange(1 << 30))
                vendor, model = dev_rng.choice(cls.VENDORS)
                devices.append(cls(
                    plant=plant,
                    device_id=f"{plant.code}-{cls.CODE}-{n:02d}",
                    area=dev_rng.choice(cls.AREAS),
                    vendor=vendor,
                    model=model,
                    rng=dev_rng,
                    interval=cls.DEFAULT_INTERVAL * interval_scale,
                    anomaly_rate=anomaly_rate,
                    chronic=dev_rng.random() < CHRONIC_FRACTION,
                    flaky=dev_rng.random() < FLAKY_FRACTION,
                    topic_prefix=topic_prefix,
                ))

    if max_devices is not None and max_devices < len(devices):
        # balanced subset: keep every k-th device so all types/plants stay represented
        step = len(devices) / max_devices
        devices = [devices[int(i * step)] for i in range(max_devices)]

    LOG.info("Fleet built: %d devices across %d plants (%d chronic, %d flaky)",
             len(devices), len(plants),
             sum(d.chronic for d in devices), sum(d.flaky for d in devices))
    return plants, devices


def fleet_manifest(plants: list[Plant], devices: list[Device]) -> dict:
    """JSON-serializable inventory — useful for phase-2 asset mapping in IOFlow."""
    return {
        "plants": [{
            "plant_id": p.plant_id, "code": p.code, "name": p.name,
            "country": p.country, "tz_offset_h": p.tz_offset_h,
            "voltage_ll": p.voltage_ll, "frequency_hz": p.frequency,
            "shift_pattern": p.shift_pattern,
        } for p in plants],
        "device_count": len(devices),
        "devices": [{
            "device_id": d.device_id, "plant": d.plant.plant_id,
            "device_type": d.TYPE, "area": d.area, "vendor": d.vendor,
            "model": d.model, "fw": d.fw, "interval_s": d.interval,
            "wireless": d.WIRELESS, "chronic": d.chronic, "flaky": d.flaky,
            "topic_data": d.topic_data, "topic_status": d.topic_status,
            "topic_meta": d.topic_meta,
        } for d in devices],
    }
