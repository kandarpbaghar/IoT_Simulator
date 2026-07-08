"""Anomaly engine — schedules and tracks real-life fault patterns per device.

Two layers of "wrongness" exist in the simulator:
  1. Stateful anomalies (this module): scheduled events with a start, duration
     and severity — bearing wear, air leaks, dropouts, flatlines, drift, ...
     Ground truth for every start/end is reported via a callback so the
     monitoring system's detections can be validated later.
  2. One-shot data-quality glitches (in devices.py): per-message nulls, missing
     fields, sentinel outliers, duplicates, seq gaps. Cheap, memoryless.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Callable

LOG = logging.getLogger("plant-sim.anomaly")

# kind -> (weight, min_duration_s, max_duration_s, expected_symptom)
GENERIC_ANOMALIES: dict[str, tuple[float, float, float, str]] = {
    "dropout":      (3.0,  120,  1500, "device silent — stale/no data"),
    "reboot":       (2.0,   30,    90, "gap + seq/uptime reset"),
    "stuck":        (2.0,  600,  7200, "all metrics flatlined at last value"),
    "sensor_drift": (2.0, 1800, 21600, "one metric slowly drifts out of band"),
    "clock_skew":   (1.0,  600,  3600, "timestamps jump minutes off wall clock"),
}


@dataclass
class ActiveAnomaly:
    kind: str
    started: float
    until: float
    severity: float                 # 0.5 .. 1.5 scale factor
    params: dict = field(default_factory=dict)

    def progress(self, now: float) -> float:
        """0..1 ramp over the anomaly's lifetime (for gradual degradations)."""
        span = max(self.until - self.started, 1.0)
        return min(1.0, (now - self.started) / span)


class AnomalyEngine:
    """Per-device scheduler. Devices query `active(kind)` when building metrics."""

    def __init__(self, rng: random.Random, device_kinds: dict, rate_per_day: float,
                 chronic: bool,
                 on_event: Callable[[str, ActiveAnomaly], None] | None = None) -> None:
        self.rng = rng
        self.catalog = {**GENERIC_ANOMALIES, **device_kinds}
        self.rate_per_day = rate_per_day * (4.0 if chronic else 1.0)
        self.chronic = chronic
        self.on_event = on_event
        self.active_map: dict[str, ActiveAnomaly] = {}

    # -- lifecycle ----------------------------------------------------------- #
    def step(self, now: float, interval: float) -> None:
        """Advance state: expire finished anomalies, maybe start a new one."""
        try:
            for kind in [k for k, a in self.active_map.items() if now >= a.until]:
                anomaly = self.active_map.pop(kind)
                self._emit("end", anomaly)

            p_start = self.rate_per_day * interval / 86400.0
            if self.rng.random() < p_start:
                self._start(now)
        except Exception:  # noqa: BLE001 — an engine bug must never kill the fleet
            LOG.exception("anomaly engine step failed")

    def _start(self, now: float) -> None:
        candidates = [k for k in self.catalog if k not in self.active_map]
        if not candidates:
            return
        weights = [self.catalog[k][0] for k in candidates]
        kind = self.rng.choices(candidates, weights=weights, k=1)[0]
        _, dlo, dhi, _sym = self.catalog[kind]
        anomaly = ActiveAnomaly(
            kind=kind,
            started=now,
            until=now + self.rng.uniform(dlo, dhi),
            severity=self.rng.uniform(0.5, 1.5),
        )
        self.active_map[kind] = anomaly
        self._emit("start", anomaly)

    def _emit(self, phase: str, anomaly: ActiveAnomaly) -> None:
        if self.on_event:
            try:
                self.on_event(phase, anomaly)
            except Exception:  # noqa: BLE001
                LOG.exception("anomaly event callback failed")

    # -- queries used by device models ---------------------------------------- #
    def active(self, kind: str) -> ActiveAnomaly | None:
        return self.active_map.get(kind)

    def symptom(self, kind: str) -> str:
        return self.catalog.get(kind, (0, 0, 0, "?"))[3]
