"""Signal primitives: random walks, diurnal curves, shift schedules, plant context."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class RandomWalk:
    """Bounded random walk — the workhorse for slowly varying process values."""

    def __init__(self, rng: random.Random, value: float, step: float,
                 lo: float, hi: float) -> None:
        self.rng = rng
        self.value = value
        self.step = step
        self.lo = lo
        self.hi = hi

    def next(self) -> float:
        self.value = clamp(self.value + self.rng.uniform(-self.step, self.step),
                           self.lo, self.hi)
        return self.value


def diurnal(local_hour: float, base: float, amplitude: float,
            peak_hour: float = 15.0) -> float:
    """Smooth daily cycle peaking at peak_hour (e.g. outdoor temperature)."""
    return base + amplitude * math.cos((local_hour - peak_hour) / 24.0 * 2 * math.pi)


# --------------------------------------------------------------------------- #
# Shift schedule — production intensity 0..1 by local time
# --------------------------------------------------------------------------- #
# hour -> base load factor
_SHIFT_3 = {0: 0.55, 6: 0.95, 14: 0.85, 22: 0.55}      # 24x7, lighter night shift
_SHIFT_2 = {0: 0.12, 6: 0.95, 14: 0.85, 22: 0.12}      # nights: base loads only


def _shift_base(hour: float, table: dict[int, float]) -> float:
    keys = sorted(table)
    cur = keys[-1]
    for k in keys:
        if hour >= k:
            cur = k
    return table[cur]


def shift_load_factor(local_dt: datetime, pattern: str) -> float:
    """Production intensity in [0..1]: shifts, lunch dips, shift-change dips, weekends."""
    table = _SHIFT_3 if pattern == "3shift" else _SHIFT_2
    h = local_dt.hour + local_dt.minute / 60.0
    f = _shift_base(h, table)

    # 30-min cosine ramp across shift boundaries so load doesn't step instantly
    for boundary in (0, 6, 14, 22):
        d = h - boundary
        if 0 <= d < 0.5:
            prev = _shift_base((boundary - 0.51) % 24, table)
            f = prev + (f - prev) * (1 - math.cos(math.pi * d / 0.5)) / 2

    # lunch (12:30-13:15) and dinner (20:30-21:00) canteen dips
    if 12.5 <= h < 13.25:
        f *= 0.72
    elif 20.5 <= h < 21.0:
        f *= 0.85
    # brief dip at shift handover
    for boundary in (6, 14, 22):
        if boundary <= h < boundary + 0.25:
            f *= 0.70

    wd = local_dt.weekday()
    if wd == 5:                       # Saturday — reduced single shift
        f *= 0.45 if 6 <= h < 15 else 0.35
    elif wd == 6:                     # Sunday — maintenance / base load only
        f *= 0.15
    return clamp(f, 0.0, 1.0)


# --------------------------------------------------------------------------- #
# Plant context — location, climate, electrical system, grid events
# --------------------------------------------------------------------------- #
@dataclass
class Plant:
    plant_id: str          # "pune"
    code: str              # "PUN" (device id prefix)
    name: str
    tz_offset_h: float
    country: str
    voltage_ll: float      # nominal line-to-line volts
    frequency: float       # 50 / 60 Hz
    climate_base_c: float  # July outdoor mean
    climate_amp_c: float
    shift_pattern: str     # "3shift" | "2shift"
    rng: random.Random = field(repr=False, default=None)

    # runtime state
    _day_walk: RandomWalk = field(init=False, default=None, repr=False)
    _grid_event: dict | None = field(init=False, default=None, repr=False)
    _grid_next_check: float = field(init=False, default=0.0, repr=False)

    def __post_init__(self) -> None:
        if self.rng is None:
            self.rng = random.Random(hash(self.plant_id) & 0xFFFF)
        # day-to-day production variation (order book, planned output)
        self._day_walk = RandomWalk(self.rng, 0.92, 0.005, 0.75, 1.0)

    def local_dt(self, now_utc: datetime) -> datetime:
        return now_utc + timedelta(hours=self.tz_offset_h)

    def load_factor(self, now_utc: datetime) -> float:
        return shift_load_factor(self.local_dt(now_utc), self.shift_pattern) \
            * self._day_walk.next()

    def ambient_c(self, now_utc: datetime) -> float:
        ld = self.local_dt(now_utc)
        h = ld.hour + ld.minute / 60.0
        return diurnal(h, self.climate_base_c, self.climate_amp_c) \
            + self.rng.gauss(0, 0.3)

    def indoor_c(self, now_utc: datetime) -> float:
        """Shop-floor temperature: HVAC pulls toward 24 °C, outdoor leaks in."""
        return 24.0 * 0.65 + self.ambient_c(now_utc) * 0.35

    # -- plant-wide grid disturbances (affect every energy meter in the plant) - #
    def grid_event(self, now_epoch: float) -> dict | None:
        """Return active {'kind': 'sag'|'swell', 'depth': float} or None."""
        if self._grid_event and now_epoch >= self._grid_event["until"]:
            self._grid_event = None
        if self._grid_event is None and now_epoch >= self._grid_next_check:
            self._grid_next_check = now_epoch + 60.0
            # ~1 disturbance / 6 h of runtime per plant
            if self.rng.random() < 60.0 / (6 * 3600):
                kind = "sag" if self.rng.random() < 0.8 else "swell"
                self._grid_event = {
                    "kind": kind,
                    "depth": self.rng.uniform(0.04, 0.12),
                    "until": now_epoch + self.rng.uniform(30, 420),
                }
        return self._grid_event


def build_plants(seed: int) -> list[Plant]:
    rng = random.Random(seed)
    specs = [
        ("pune",      "PUN", "Pune Works",          5.5,  "IN", 415.0, 50.0, 27.0, 4.0, "3shift"),
        ("chennai",   "CHN", "Chennai Plant",       5.5,  "IN", 415.0, 50.0, 31.0, 4.0, "3shift"),
        ("frankfurt", "FRA", "Frankfurt Fertigung", 2.0,  "DE", 400.0, 50.0, 21.0, 6.0, "2shift"),
        ("houston",   "HOU", "Houston Facility",   -5.0,  "US", 480.0, 60.0, 31.0, 5.0, "3shift"),
    ]
    return [Plant(*s, rng=random.Random(rng.randrange(1 << 30))) for s in specs]


UTC = timezone.utc
