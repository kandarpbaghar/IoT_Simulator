"""Device models — 12 device types typically found in a manufacturing plant.

Each device produces a JSON envelope with realistic, physically-correlated
metrics. Subclasses implement `_metrics()`; the base class handles the anomaly
engine, envelope, and one-shot data-quality glitches (nulls, missing fields,
sentinel outliers, seq gaps, duplicates).
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timezone

from .anomalies import AnomalyEngine
from .signals import Plant, RandomWalk, clamp

LOG = logging.getLogger("plant-sim.device")

SENTINELS = (32767, -999.9, 65535, -3276.8)  # classic raw-register error values


class Device:
    TYPE = "generic"
    CODE = "GEN"
    DEFAULT_INTERVAL = 30.0
    WIRELESS = False               # wireless sensors get rssi + battery
    MODES: dict[str, tuple[float, float, float, str]] = {}
    VENDORS: list[tuple[str, str]] = [("Generic", "G-1000")]
    AREAS: list[str] = ["utilities"]

    def __init__(self, plant: Plant, device_id: str, area: str,
                 vendor: str, model: str, rng: random.Random,
                 interval: float, anomaly_rate: float, chronic: bool,
                 flaky: bool, topic_prefix: str) -> None:
        self.plant = plant
        self.device_id = device_id
        self.area = area
        self.vendor = vendor
        self.model = model
        self.rng = rng
        self.interval = interval
        self.flaky = flaky
        self.chronic = chronic
        self.fw = f"{rng.randint(1, 4)}.{rng.randint(0, 9)}.{rng.randint(0, 20)}"

        base = f"{topic_prefix}/{plant.plant_id}/{self.TYPE}/{device_id}"
        self.topic_data = f"{base}/data"
        self.topic_status = f"{base}/status"
        self.topic_meta = f"{base}/meta"       # retained birth message

        self.seq = rng.randint(0, 40000)
        self.boot_epoch: float | None = None       # set on first tick
        self.clock_skew_s = rng.gauss(0, 2.0)      # small NTP scatter on everyone
        if rng.random() < 0.04:                    # a few devices have broken NTP
            self.clock_skew_s = rng.uniform(90, 480) * rng.choice((-1, 1))
        self._pending_reset = False
        self._last_now: float | None = None

        if self.WIRELESS:
            self.battery_pct = rng.uniform(35.0, 100.0)
            self._rssi = RandomWalk(rng, rng.uniform(-75, -50), 2.0, -92, -40)

        self.engine = AnomalyEngine(
            rng=random.Random(rng.randrange(1 << 30)),
            device_kinds=self.MODES,
            rate_per_day=anomaly_rate,
            chronic=chronic,
            on_event=None,          # wired by the runner
        )
        self._init_state()

    # subclasses override ----------------------------------------------------- #
    def _init_state(self) -> None: ...

    def _metrics(self, now: datetime, epoch: float,
                 load: float) -> tuple[dict, dict]:
        """Return (metrics, status). status = {'state': str, 'alarms': [str]}."""
        raise NotImplementedError

    # anomaly helpers for subclasses ------------------------------------------ #
    def mode(self, kind: str):
        return self.engine.active(kind)

    def sev(self, kind: str, epoch: float) -> float:
        """severity * progress for gradual degradations; 0 if inactive."""
        a = self.engine.active(kind)
        return a.severity * a.progress(epoch) if a else 0.0

    # tick --------------------------------------------------------------------- #
    def tick(self, now: datetime, epoch: float) -> list[tuple[str, dict, bool]]:
        """Return list of (topic, payload_dict, retain) to publish."""
        try:
            if self.boot_epoch is None:
                self.boot_epoch = epoch - self.rng.uniform(3600, 40 * 86400)
            self.engine.step(epoch, self.interval)

            if self.engine.active("dropout"):
                return []
            if self.engine.active("reboot"):
                self._pending_reset = True
                return []
            out: list[tuple[str, dict, bool]] = []
            if self._pending_reset:                # back from reboot
                self._pending_reset = False
                self.seq = 0
                self.boot_epoch = epoch
                # real devices republish their retained birth on every reconnect
                out.append((self.topic_meta, self.birth_payload(epoch), True))

            load = self.plant.load_factor(now)
            metrics, status = self._metrics(now, epoch, load)

            if self.WIRELESS:
                self.battery_pct = max(
                    0.0, self.battery_pct - self.rng.uniform(0.0002, 0.0015))
                metrics["battery_pct"] = round(self.battery_pct, 1)
                if self.battery_pct < 15:
                    status["alarms"].append("LOW_BATTERY")

            metrics, status = self._apply_stateful(metrics, status, epoch)
            metrics = self._apply_glitches(metrics)
            payload = self._envelope(now, epoch, metrics, status)

            out.append((self.topic_data, payload, False))
            # duplicate delivery (QoS/network retry) — same seq, same payload
            if self.rng.random() < (0.012 if self.flaky else 0.002):
                out.append((self.topic_data, payload, False))
            return out
        except Exception:  # noqa: BLE001 — one broken device must not kill the fleet
            LOG.exception("tick failed for %s", self.device_id)
            return []

    # stateful anomalies applied generically ----------------------------------- #
    def _apply_stateful(self, metrics: dict, status: dict,
                        epoch: float) -> tuple[dict, dict]:
        stuck = self.engine.active("stuck")
        if stuck:
            if "frozen" not in stuck.params:
                stuck.params["frozen"] = (dict(metrics), dict(status))
            fm, fs = stuck.params["frozen"]
            metrics, status = dict(fm), {"state": fs["state"],
                                         "alarms": list(fs["alarms"])}
        drift = self.engine.active("sensor_drift")
        if drift:
            keys = [k for k, v in metrics.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if keys and "key" not in drift.params:
                drift.params["key"] = self.rng.choice(keys)
                drift.params["sign"] = self.rng.choice((-1, 1))
            k = drift.params.get("key")
            if k in metrics and metrics[k] is not None:
                scale = max(abs(float(metrics[k])) * 0.25, 1.0)
                metrics[k] = round(
                    float(metrics[k])
                    + drift.params["sign"] * drift.severity
                    * drift.progress(epoch) * scale, 3)
        return metrics, status

    # one-shot data-quality glitches -------------------------------------------- #
    def _apply_glitches(self, metrics: dict) -> dict:
        boost = 6.0 if self.flaky else 1.0
        num_keys = [k for k, v in metrics.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if num_keys and self.rng.random() < 0.0015 * boost:
            metrics[self.rng.choice(num_keys)] = None            # null value
        if num_keys and self.rng.random() < 0.0015 * boost:
            metrics.pop(self.rng.choice(num_keys), None)         # field vanished
        if num_keys and self.rng.random() < 0.001 * boost:
            metrics[self.rng.choice(num_keys)] = self.rng.choice(SENTINELS)
        if self.rng.random() < 0.001 * boost:
            self.seq += self.rng.randint(3, 40)                  # lost messages
        return metrics

    # envelope -------------------------------------------------------------------- #
    @staticmethod
    def _iso(ts_epoch: float) -> str:
        return datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.") + f"{int(ts_epoch * 1000) % 1000:03d}Z"

    def _envelope(self, now: datetime, epoch: float,
                  metrics: dict, status: dict) -> dict:
        """Lean telemetry: identity lives in the topic, master data in the
        retained birth message (see birth_payload)."""
        skew = self.clock_skew_s
        cj = self.engine.active("clock_skew")
        if cj:
            if "offset" not in cj.params:
                cj.params["offset"] = cj.severity * self.rng.uniform(120, 600) \
                    * self.rng.choice((-1, 1))
            skew += cj.params["offset"]
        ts_epoch = epoch + skew
        self.seq += 1
        payload = {
            "ts": self._iso(ts_epoch),
            "ts_epoch_ms": int(ts_epoch * 1000),
            "seq": self.seq,
            "uptime_s": int(epoch - self.boot_epoch),
            "metrics": metrics,
            "status": status,
        }
        if self.WIRELESS:
            payload["rssi_dbm"] = int(self._rssi.next())
        return payload

    def birth_payload(self, epoch: float) -> dict:
        """Master data — published retained to <base>/meta on every (re)connect,
        so late subscribers (IOFlow) receive it immediately on subscription."""
        return {
            "ts": self._iso(epoch),
            "device_id": self.device_id,
            "plant": self.plant.plant_id,
            "area": self.area,
            "device_type": self.TYPE,
            "vendor": self.vendor,
            "model": self.model,
            "fw": self.fw,
            "wireless": self.WIRELESS,
            "interval_s": self.interval,
            "schema": "json-v1",
        }

    # helpers ---------------------------------------------------------------------- #
    def w(self, name: str, value: float, step: float, lo: float, hi: float) -> RandomWalk:
        walk = RandomWalk(self.rng, value, step, lo, hi)
        setattr(self, name, walk)
        return walk

    def g(self, sigma: float) -> float:
        return self.rng.gauss(0, sigma)


# =============================================================================== #
# 1. Three-phase energy / power-quality meter
# =============================================================================== #
class EnergyMeter(Device):
    TYPE = "energy_meter"
    CODE = "EM"
    DEFAULT_INTERVAL = 15.0
    VENDORS = [("Schneider", "PM5560"), ("Siemens", "PAC3200"),
               ("ABB", "M4M-30"), ("Secure", "Elite-446")]
    AREAS = ["main-incomer", "press-shop", "weld-shop", "paint-shop",
             "assembly", "compressor-room", "hvac-plant", "utilities"]
    MODES = {
        "pf_low":          (2.0, 1800, 14400, "power factor below 0.8 penalty band"),
        "idle_load_high":  (2.0, 3600, 28800, "high kW draw during idle/night hours"),
        "phase_imbalance": (2.0, 1800, 21600, "phase current imbalance > 10 %"),
        "ct_fault":        (1.0, 3600, 43200, "one phase current reads near zero"),
    }

    def _init_state(self) -> None:
        self.rated_kw = self.rng.choice([75, 110, 160, 250, 400, 630])
        self.pf_base = self.rng.uniform(0.86, 0.95)
        self.base_frac = self.rng.uniform(0.05, 0.12)
        self.kwh = self.rng.uniform(1e5, 5e6)
        self.w("util", self.rng.uniform(0.75, 0.95), 0.01, 0.6, 1.05)
        self.w("vdev", 1.0, 0.001, 0.975, 1.025)
        self.w("fdev", 0.0, 0.005, -0.06, 0.06)
        self.imb_base = self.rng.uniform(0.5, 2.5)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        p = self.plant

        v_ll = p.voltage_ll * self.vdev.next()
        grid = p.grid_event(epoch)
        if grid:
            v_ll *= (1 - grid["depth"]) if grid["kind"] == "sag" else (1 + grid["depth"])
            alarms.append("VOLT_SAG" if grid["kind"] == "sag" else "VOLT_SWELL")
        v_ln = v_ll / math.sqrt(3)

        kw = self.rated_kw * max(load * self.util.next(), self.base_frac) \
            * (1 + self.g(0.015))
        if self.mode("idle_load_high") and load < 0.5:
            kw = max(kw, self.rated_kw * 0.38)     # something left running at idle

        pf = self.pf_base - max(0.0, (0.45 - load)) * 0.35 - 0.12 * self.sev("pf_low", epoch)
        pf = clamp(pf + self.g(0.008), 0.55, 0.99)
        if pf < 0.8:
            alarms.append("LOW_PF")

        kva = kw / pf
        kvar = math.sqrt(max(kva * kva - kw * kw, 0.0))

        imb = self.imb_base + 12.0 * self.sev("phase_imbalance", epoch)
        if imb > 10:
            alarms.append("PHASE_IMBALANCE")
        i_avg = kva * 1000 / (math.sqrt(3) * v_ll)
        i1 = i_avg * (1 + imb / 100)
        i2 = i_avg * (1 - imb / 200)
        i3 = i_avg * (1 - imb / 200)
        if self.mode("ct_fault"):
            i2 *= 0.02
            alarms.append("CT_FAULT_SUSPECTED")

        self.kwh += kw * self.interval / 3600.0
        m = {
            "voltage_l1_v": round(v_ln * (1 + self.g(0.002)), 1),
            "voltage_l2_v": round(v_ln * (1 + self.g(0.002)), 1),
            "voltage_l3_v": round(v_ln * (1 + self.g(0.002)), 1),
            "current_l1_a": round(i1, 1),
            "current_l2_a": round(i2, 1),
            "current_l3_a": round(i3, 1),
            "active_power_kw": round(kw, 2),
            "reactive_power_kvar": round(kvar, 2),
            "apparent_power_kva": round(kva, 2),
            "power_factor": round(pf, 3),
            "frequency_hz": round(p.frequency + self.fdev.next(), 3),
            "thd_current_pct": round(6 + 18 * (1 - load) + self.g(0.8), 1),
            "thd_voltage_pct": round(1.8 + self.g(0.2), 2),
            "current_imbalance_pct": round(imb, 1),
            "energy_kwh": round(self.kwh, 1),
            "demand_kw": round(kw * (1 + self.g(0.02)), 2),
        }
        return m, {"state": "ok", "alarms": alarms}


# =============================================================================== #
# 2. Wireless vibration sensor on rotating assets
# =============================================================================== #
class VibrationSensor(Device):
    TYPE = "vibration"
    CODE = "VIB"
    DEFAULT_INTERVAL = 30.0
    WIRELESS = True
    VENDORS = [("SKF", "CMWA-8800"), ("Fluke", "3563"), ("IFM", "VVB001")]
    AREAS = ["press-shop", "compressor-room", "hvac-plant", "paint-shop", "utilities"]
    MODES = {
        "bearing_wear": (3.0, 4 * 3600, 24 * 3600, "vibration RMS ramps into ISO zone C/D"),
        "imbalance":    (2.0, 2 * 3600, 12 * 3600, "elevated vibration while running"),
        "looseness":    (1.5, 1800, 7200, "intermittent vibration spikes"),
    }

    def _init_state(self) -> None:
        self.asset = self.rng.choice(["motor", "pump", "fan", "gearbox"])
        self.run_threshold = self.rng.uniform(0.15, 0.35)
        self.w("vbase", self.rng.uniform(1.0, 2.8), 0.05, 0.6, 3.5)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        running = load > self.run_threshold
        if running:
            v = self.vbase.next() + 6.5 * self.sev("bearing_wear", epoch)
            if self.mode("imbalance"):
                v += 2.5 * self.mode("imbalance").severity
            if self.mode("looseness") and self.rng.random() < 0.3:
                v *= self.rng.uniform(1.6, 3.2)
            v += abs(self.g(0.15))
        else:
            v = self.rng.uniform(0.03, 0.15)

        temp = self.plant.indoor_c(now) + (18.0 * load if running else 0.0) \
            + 24.0 * self.sev("bearing_wear", epoch) + self.g(0.4)
        if v > 7.1:
            alarms.append("ISO10816_ZONE_D")
        elif v > 4.5:
            alarms.append("ISO10816_ZONE_C")
        if temp > 95:
            alarms.append("BEARING_OVERTEMP")

        m = {
            "velocity_rms_mm_s": round(v, 2),
            "accel_peak_g": round(0.4 + v * 0.35 + abs(self.g(0.05)), 2),
            "bearing_temp_c": round(temp, 1),
            "asset_running": running,
            "asset_type": self.asset,
        }
        return m, {"state": "running" if running else "stopped", "alarms": alarms}


# =============================================================================== #
# 3. Ambient temperature / humidity sensor
# =============================================================================== #
class EnvSensor(Device):
    TYPE = "env_sensor"
    CODE = "ENV"
    DEFAULT_INTERVAL = 60.0
    WIRELESS = True
    VENDORS = [("Milesight", "EM320-TH"), ("Dragino", "LHT65N"), ("Efento", "NB-TH")]
    AREAS = ["assembly", "paint-shop", "warehouse", "quality-lab", "server-room", "weld-shop"]
    MODES = {
        "hvac_fail": (2.5, 3600, 21600, "zone temperature drifts toward outdoor"),
    }

    def _init_state(self) -> None:
        self.zone_offset = self.rng.uniform(-1.5, 3.0)
        rh_base = {"IN": 62.0, "DE": 46.0, "US": 52.0}.get(self.plant.country, 50.0)
        self.w("rh", rh_base + self.rng.uniform(-5, 5), 0.4, 25, 92)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        indoor = self.plant.indoor_c(now) + self.zone_offset
        hv = self.sev("hvac_fail", epoch)
        if hv:
            target = self.plant.ambient_c(now) + 4.0
            indoor = indoor + (target - indoor) * min(hv, 1.0)
        t = indoor + self.g(0.15)
        rh = self.rh.next() + self.g(0.5)
        # Magnus dew point approximation
        gamma = math.log(max(rh, 1) / 100.0) + 17.62 * t / (243.12 + t)
        dew = 243.12 * gamma / (17.62 - gamma)
        if t > 32 if self.area != "server-room" else t > 27:
            alarms.append("HIGH_TEMP")
        if rh > 75:
            alarms.append("HIGH_HUMIDITY")
        m = {
            "temperature_c": round(t, 2),
            "humidity_pct": round(clamp(rh, 0, 100), 1),
            "dew_point_c": round(dew, 2),
        }
        return m, {"state": "ok", "alarms": alarms}


# =============================================================================== #
# 4. Rotary screw air compressor
# =============================================================================== #
class AirCompressor(Device):
    TYPE = "air_compressor"
    CODE = "CMP"
    DEFAULT_INTERVAL = 20.0
    VENDORS = [("AtlasCopco", "GA75-VSD"), ("Ingersoll-Rand", "RS55ie"), ("Kaeser", "CSD-105")]
    AREAS = ["compressor-room"]
    MODES = {
        "air_leak":     (3.0, 2 * 3600, 24 * 3600, "duty cycle up, pressure sags — network leak"),
        "oil_overheat": (2.0, 1800, 10800, "oil temperature ramps past 90 degC"),
        "short_cycle":  (1.5, 1800, 7200, "rapid load/unload cycling"),
    }

    def _init_state(self) -> None:
        self.capacity_m3min = self.rng.uniform(8.0, 14.0)
        self.rated_a = self.rng.uniform(95, 140)
        self.pressure = self.rng.uniform(6.4, 6.9)
        self.loaded = True
        self.run_hours = self.rng.uniform(4000, 38000)
        self.duty = 0.6

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        leak = self.sev("air_leak", epoch)
        band_lo, band_hi = 6.2, 7.0
        if self.mode("short_cycle"):
            band_lo, band_hi = 6.55, 6.75
        demand = (0.25 + 0.65 * load) * (1 + 0.35 * leak) + self.g(0.03)

        # crude receiver model: loaded fills, demand drains
        dt_min = self.interval / 60.0
        if self.loaded:
            self.pressure += (1.0 - demand) * 0.25 * dt_min
            if self.pressure >= band_hi:
                self.loaded = False
        else:
            self.pressure -= demand * 0.30 * dt_min
            if self.pressure <= band_lo - 0.3 * leak:
                self.loaded = True
        self.pressure = clamp(self.pressure, 4.5, 7.8)
        self.duty = clamp(self.duty * 0.9 + (1.0 if self.loaded else 0.0) * 0.1, 0, 1)
        self.run_hours += self.interval / 3600.0

        oil = 62 + 20 * self.duty + 26 * self.sev("oil_overheat", epoch) + self.g(0.5)
        if oil > 90:
            alarms.append("HIGH_OIL_TEMP")
        if self.pressure < 5.8:
            alarms.append("LOW_DISCHARGE_PRESSURE")

        m = {
            "discharge_pressure_bar": round(self.pressure, 2),
            "flow_m3_min": round(self.capacity_m3min * (1.0 if self.loaded else 0.05)
                                 * (0.9 + self.g(0.02)), 2),
            "motor_current_a": round(self.rated_a * (0.95 if self.loaded else 0.35)
                                     * (1 + self.g(0.01)), 1),
            "oil_temp_c": round(oil, 1),
            "outlet_temp_c": round(oil - 25 + self.g(0.8), 1),
            "load_duty_pct": round(self.duty * 100, 1),
            "run_hours": round(self.run_hours, 1),
        }
        return m, {"state": "loaded" if self.loaded else "unloaded", "alarms": alarms}


# =============================================================================== #
# 5. Water-cooled chiller
# =============================================================================== #
class Chiller(Device):
    TYPE = "chiller"
    CODE = "CHL"
    DEFAULT_INTERVAL = 30.0
    VENDORS = [("Trane", "RTAF-155"), ("Carrier", "30XV-500"), ("Daikin", "EWAD-C")]
    AREAS = ["hvac-plant"]
    MODES = {
        "refrigerant_low":   (2.5, 4 * 3600, 24 * 3600, "supply temp rises, deltaT collapses, COP drops"),
        "condenser_fouling": (2.0, 6 * 3600, 48 * 3600, "power creeps up ~15 %, COP degrades"),
    }

    def _init_state(self) -> None:
        self.design_kw_th = self.rng.uniform(350, 800)
        self.design_flow = self.design_kw_th / (4.18 * 5.0) * 3.6   # m3/h at 5K deltaT
        self.w("setpoint", 6.5, 0.02, 6.2, 6.9)
        self.cop_base = self.rng.uniform(4.2, 5.0)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        amb = self.plant.ambient_c(now)
        th_load = self.design_kw_th * (0.25 + 0.75 * load) \
            * clamp(1 + (amb - 24) * 0.02, 0.8, 1.4)
        loadfrac = clamp(th_load / self.design_kw_th, 0.0, 1.1)

        rlow = self.sev("refrigerant_low", epoch)
        foul = self.sev("condenser_fouling", epoch)
        supply = self.setpoint.next() + 3.2 * rlow + self.g(0.08)
        delta_t = 5.0 * loadfrac * (1 - 0.5 * rlow) + self.g(0.05)
        cop = self.cop_base * (1 - 0.008 * max(amb - 25, 0)) \
            * (1 - 0.30 * rlow) * (1 - 0.15 * foul)
        kw = th_load / max(cop, 1.5) * (1 + self.g(0.015))

        if supply > 9.0:
            alarms.append("HIGH_CHW_SUPPLY_TEMP")
        if cop < 3.0:
            alarms.append("LOW_EFFICIENCY")

        m = {
            "chw_supply_temp_c": round(supply, 2),
            "chw_return_temp_c": round(supply + max(delta_t, 0.2), 2),
            "chw_flow_m3_h": round(self.design_flow * (0.6 + 0.4 * loadfrac)
                                   * (1 + self.g(0.01)), 1),
            "cooling_load_kw": round(th_load, 1),
            "compressor_power_kw": round(kw, 1),
            "cop": round(cop, 2),
            "condenser_pressure_bar": round(12.5 + (amb - 25) * 0.25 + 3.0 * foul
                                            + self.g(0.1), 2),
            "evaporator_pressure_bar": round(3.4 - 0.8 * rlow + self.g(0.05), 2),
        }
        state = "running" if loadfrac > 0.28 else "standby"
        return m, {"state": state, "alarms": alarms}


# =============================================================================== #
# 6. Utility flow meter (water / compressed air / natural gas)
# =============================================================================== #
class FlowMeter(Device):
    TYPE = "flow_meter"
    CODE = "FLM"
    DEFAULT_INTERVAL = 30.0
    VENDORS = [("Endress+Hauser", "Promag-W400"), ("Krohne", "Optiflux-2300"),
               ("Siemens", "MAG-8000")]
    AREAS = ["utilities", "paint-shop", "boiler-house", "hvac-plant"]
    MODES = {
        "leak":            (3.0, 4 * 3600, 48 * 3600, "elevated night-time base flow"),
        "stuck_totalizer": (1.5, 3600, 24 * 3600, "totalizer freezes while flow continues"),
        "reverse_flow":    (1.0, 300, 1800, "brief negative flow readings"),
    }

    def _init_state(self) -> None:
        self.medium = self.rng.choice(["water", "compressed_air", "natural_gas"])
        self.design_m3h = {"water": self.rng.uniform(20, 90),
                           "compressed_air": self.rng.uniform(300, 700),
                           "natural_gas": self.rng.uniform(40, 160)}[self.medium]
        self.total_m3 = self.rng.uniform(1e4, 9e5)
        self.base_frac = self.rng.uniform(0.01, 0.04)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        base = self.base_frac + 0.12 * self.sev("leak", epoch)
        flow = self.design_m3h * (base + (0.85 - base) * load) * (1 + self.g(0.04))
        if self.mode("reverse_flow") and self.rng.random() < 0.4:
            flow = -abs(flow) * self.rng.uniform(0.1, 0.5)
            alarms.append("REVERSE_FLOW")
        if not self.mode("stuck_totalizer"):
            self.total_m3 += max(flow, 0) * self.interval / 3600.0
        m = {
            "flow_m3_h": round(flow, 2),
            "totalizer_m3": round(self.total_m3, 2),
            "pressure_bar": round({"water": 3.5, "compressed_air": 6.6,
                                   "natural_gas": 1.1}[self.medium]
                                  * (1 + self.g(0.02)), 2),
            "fluid_temp_c": round(self.plant.indoor_c(now)
                                  + (25 if self.medium == "compressed_air" else 2)
                                  + self.g(0.5), 1),
            "medium": self.medium,
        }
        return m, {"state": "ok", "alarms": alarms}


# =============================================================================== #
# 7. Storage tank level (diesel / water / coolant)
# =============================================================================== #
class TankLevel(Device):
    TYPE = "tank_level"
    CODE = "TNK"
    DEFAULT_INTERVAL = 60.0
    WIRELESS = True
    VENDORS = [("VEGA", "VEGAPULS-C21"), ("Siemens", "LR-110"), ("Ifm", "LW2720")]
    AREAS = ["utilities", "boiler-house", "genset-yard", "paint-shop"]
    MODES = {
        "tank_leak":      (2.5, 4 * 3600, 24 * 3600, "level drains 3x faster than usage"),
        "overfill_glitch": (1.0, 300, 1200, "level reads > 100 %"),
    }

    def _init_state(self) -> None:
        self.medium = self.rng.choice(["diesel", "water", "coolant", "hydraulic_oil"])
        self.capacity_l = self.rng.choice([5000, 10000, 20000, 50000])
        self.level = self.rng.uniform(35, 90)
        self.filling = False
        # % consumed per hour at full production
        self.use_rate = self.rng.uniform(0.8, 3.0)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        dt_h = self.interval / 3600.0
        if self.filling:
            self.level += 25.0 * dt_h * 60 / 8       # refill in ~8 min
            if self.level >= 95:
                self.filling = False
        else:
            drain = self.use_rate * (0.15 + 0.85 * load)
            drain *= 1 + 3.0 * self.sev("tank_leak", epoch)
            self.level -= drain * dt_h
            if self.level <= 18:
                self.filling = True
        self.level = clamp(self.level, 0, 100)

        reported = self.level + self.g(0.15)
        if self.mode("overfill_glitch"):
            reported = self.rng.uniform(101, 108)
            alarms.append("SENSOR_RANGE")
        if self.level < 15:
            alarms.append("LOW_LEVEL")
        m = {
            "level_pct": round(reported, 2),
            "volume_l": round(self.capacity_l * clamp(reported, 0, 100) / 100, 0),
            "fluid_temp_c": round(self.plant.ambient_c(now) * 0.6 + 10 + self.g(0.3), 1),
            "medium": self.medium,
        }
        return m, {"state": "filling" if self.filling else "draining", "alarms": alarms}


# =============================================================================== #
# 8. Steam boiler
# =============================================================================== #
class Boiler(Device):
    TYPE = "boiler"
    CODE = "BLR"
    DEFAULT_INTERVAL = 20.0
    VENDORS = [("Thermax", "CPRD-60"), ("Bosch", "UL-S-2000"), ("Cleaver-Brooks", "CBEX-800")]
    AREAS = ["boiler-house"]
    MODES = {
        "efficiency_loss": (2.5, 6 * 3600, 48 * 3600, "stack temp & O2 creep up, efficiency drops"),
        "low_water":       (1.5, 600, 3600, "drum level low + alarm"),
        "pressure_hunt":   (1.5, 1800, 7200, "steam pressure oscillates"),
    }

    def _init_state(self) -> None:
        self.w("pressure", 8.0, 0.04, 7.2, 8.8)
        self.w("drum", 52.0, 0.8, 40, 62)
        self.max_steam_kg_h = self.rng.uniform(1500, 4000)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        eff_loss = self.sev("efficiency_loss", epoch)
        steam = self.max_steam_kg_h * (0.2 + 0.7 * load) * (1 + self.g(0.02))
        pressure = self.pressure.next()
        if self.mode("pressure_hunt"):
            pressure += 0.8 * math.sin(epoch / 45.0)
        drum = self.drum.next()
        if self.mode("low_water"):
            drum = self.rng.uniform(24, 34)
            alarms.append("LOW_WATER")
        efficiency = 88.0 - 6.0 * eff_loss + self.g(0.2)
        stack = 172 + 55 * eff_loss + 12 * load + self.g(1.0)
        fuel = steam * 0.075 / (efficiency / 88.0)
        if stack > 220:
            alarms.append("HIGH_STACK_TEMP")
        m = {
            "steam_pressure_bar": round(pressure, 2),
            "steam_flow_kg_h": round(steam, 0),
            "steam_temp_c": round(175 + pressure * 1.5 + self.g(0.5), 1),
            "feedwater_temp_c": round(102 + self.g(0.8), 1),
            "drum_level_pct": round(drum, 1),
            "fuel_flow_kg_h": round(fuel, 1),
            "o2_pct": round(3.0 + 3.2 * eff_loss + self.g(0.15), 2),
            "stack_temp_c": round(stack, 1),
            "efficiency_pct": round(efficiency, 1),
        }
        return m, {"state": "firing" if load > 0.15 else "standby", "alarms": alarms}


# =============================================================================== #
# 9. Variable-frequency drive on process motors
# =============================================================================== #
class VFD(Device):
    TYPE = "vfd"
    CODE = "VFD"
    DEFAULT_INTERVAL = 15.0
    VENDORS = [("Danfoss", "FC-302"), ("ABB", "ACS880"), ("Siemens", "G120X"),
               ("Yaskawa", "GA700")]
    AREAS = ["press-shop", "paint-shop", "assembly", "hvac-plant", "weld-shop"]
    MODES = {
        "overtemp_derate":  (2.5, 1800, 10800, "heatsink hot -> speed derated, warn code"),
        "overcurrent_trip": (2.0, 300, 1800, "drive faults F07, motor stops"),
        "fan_fail":         (1.5, 3600, 24 * 3600, "cooling fan dead, temp creeps"),
    }

    def _init_state(self) -> None:
        self.rated_kw = self.rng.choice([7.5, 15, 22, 37, 55, 90])
        self.rated_a = self.rated_kw * 1.9
        self.speed_steps = [0.35, 0.5, 0.65, 0.8, 0.9, 1.0]
        self.step_idx = self.rng.randrange(2, len(self.speed_steps))
        self.w("torque", self.rng.uniform(0.55, 0.85), 0.02, 0.3, 0.98)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        fault_code = 0
        running = load > 0.2
        if self.rng.random() < 0.02:                       # recipe change
            self.step_idx = self.rng.randrange(len(self.speed_steps))
        speed_frac = self.speed_steps[self.step_idx] if running else 0.0

        heatsink = self.plant.indoor_c(now) + 28 * speed_frac * self.torque.value \
            + 22 * self.sev("overtemp_derate", epoch) \
            + 15 * self.sev("fan_fail", epoch) + self.g(0.4)
        if heatsink > 85 and running:
            speed_frac *= 0.8                              # thermal derate
            alarms.append("W29_HEATSINK_OVERTEMP")
        if self.mode("overcurrent_trip"):
            running, speed_frac, fault_code = False, 0.0, 7
            alarms.append("F07_OVERCURRENT")

        torque = self.torque.next() if running else 0.0
        freq = self.plant.frequency * speed_frac
        m = {
            "output_freq_hz": round(freq, 2),
            "motor_speed_rpm": round(freq * (1450 / 50 if self.plant.frequency == 50
                                             else 1750 / 60) * (1 + self.g(0.002)), 0),
            "output_current_a": round(self.rated_a * torque * (1 + self.g(0.01)), 1),
            "output_power_kw": round(self.rated_kw * torque * speed_frac
                                     * (1 + self.g(0.01)), 2),
            "torque_pct": round(torque * 100, 1),
            "dc_bus_v": round(self.plant.voltage_ll * 1.35 * (1 + self.g(0.004)), 0),
            "heatsink_temp_c": round(heatsink, 1),
            "fault_code": fault_code,
        }
        state = "fault" if fault_code else ("running" if running else "stopped")
        return m, {"state": state, "alarms": alarms}


# =============================================================================== #
# 10. UPS protecting controls / IT
# =============================================================================== #
class UPS(Device):
    TYPE = "ups"
    CODE = "UPS"
    DEFAULT_INTERVAL = 60.0
    VENDORS = [("APC", "SRT-10K"), ("Eaton", "93PM"), ("Vertiv", "GXT5")]
    AREAS = ["server-room", "plc-panel-room", "quality-lab"]
    MODES = {
        "mains_fail":       (2.0, 120, 2400, "on battery, charge draining"),
        "battery_degraded": (2.0, 6 * 3600, 72 * 3600, "battery health/runtime low"),
    }

    def _init_state(self) -> None:
        self.battery = 100.0
        self.health = self.rng.uniform(88, 100)
        self.nominal_v = 230.0 if self.plant.frequency == 50 else 277.0
        self.w("load_pct", self.rng.uniform(30, 65), 0.5, 15, 85)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        on_battery = bool(self.mode("mains_fail"))
        health = self.health - 30 * self.sev("battery_degraded", epoch)
        lp = self.load_pct.next()
        if on_battery:
            self.battery = max(0.0, self.battery - 0.45 * (lp / 50) * self.interval / 60)
            alarms.append("ON_BATTERY")
            input_v = self.rng.uniform(0, 40)
        else:
            self.battery = min(100.0, self.battery + 0.25 * self.interval / 60)
            input_v = self.nominal_v * (1 + self.g(0.01))
        if self.battery < 30:
            alarms.append("LOW_BATTERY")
        if health < 65:
            alarms.append("REPLACE_BATTERY")
        runtime = (self.battery / 100) * (health / 100) * (55 / max(lp / 50, 0.2))
        m = {
            "input_voltage_v": round(input_v, 1),
            "output_voltage_v": round(self.nominal_v * (1 + self.g(0.003)), 1),
            "load_pct": round(lp, 1),
            "battery_charge_pct": round(self.battery, 1),
            "battery_health_pct": round(health, 1),
            "battery_temp_c": round(24 + lp * 0.08 + self.g(0.3), 1),
            "runtime_min": round(runtime, 1),
            "on_battery": on_battery,
        }
        return m, {"state": "on_battery" if on_battery else "online", "alarms": alarms}


# =============================================================================== #
# 11. Production line counter (PLC edge gateway)
# =============================================================================== #
class ProductionLine(Device):
    TYPE = "production_counter"
    CODE = "LIN"
    DEFAULT_INTERVAL = 10.0
    VENDORS = [("Siemens", "S7-1214C+IoT2050"), ("Rockwell", "5069-L320ER"),
               ("Mitsubishi", "FX5U")]
    AREAS = ["press-shop", "weld-shop", "assembly", "paint-shop"]
    MODES = {
        "high_reject":     (2.5, 1800, 14400, "reject ratio jumps from <2 % to 6-12 %"),
        "microstoppages":  (2.5, 1800, 10800, "frequent brief idle flickers"),
        "jam":             (2.0, 600, 2400, "line in FAULT, counters stall"),
    }

    STATES = ("RUNNING", "IDLE", "STARVED", "BLOCKED", "CHANGEOVER", "FAULT")

    def _init_state(self) -> None:
        self.ideal_ppm = self.rng.choice([12, 20, 30, 45, 60, 90])
        self.good = self.rng.randint(1_000_000, 9_000_000)
        self.reject = int(self.good * self.rng.uniform(0.004, 0.015))
        self.state = "RUNNING"
        self.state_until = 0.0
        self.w("speed_eff", self.rng.uniform(0.88, 0.97), 0.005, 0.7, 1.0)

    def _next_state(self, epoch: float, load: float) -> None:
        if epoch < self.state_until:
            return
        r = self.rng.random()
        if self.mode("jam"):
            self.state, dur = "FAULT", self.rng.uniform(120, 600)
        elif load < 0.25:
            self.state, dur = "IDLE", self.rng.uniform(300, 1200)
        elif self.mode("microstoppages") and r < 0.35:
            self.state, dur = self.rng.choice(("IDLE", "STARVED")), self.rng.uniform(15, 45)
        elif r < 0.06:
            self.state, dur = "STARVED", self.rng.uniform(60, 300)
        elif r < 0.10:
            self.state, dur = "BLOCKED", self.rng.uniform(60, 240)
        elif r < 0.115:
            self.state, dur = "CHANGEOVER", self.rng.uniform(900, 2400)
        else:
            self.state, dur = "RUNNING", self.rng.uniform(300, 1800)
        self.state_until = epoch + dur

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        self._next_state(epoch, load)
        rate = 0.0
        if self.state == "RUNNING":
            rate = self.ideal_ppm * self.speed_eff.next() * (1 + self.g(0.02))
            made = rate * self.interval / 60.0
            rej_ratio = 0.008 + 0.09 * self.sev("high_reject", epoch)
            rejects = sum(1 for _ in range(int(made) + 1)
                          if self.rng.random() < rej_ratio)
            self.good += max(int(made) - rejects, 0)
            self.reject += rejects
        if self.state == "FAULT":
            alarms.append("LINE_JAM")
        if self.mode("high_reject"):
            alarms.append("HIGH_REJECT_RATE")
        m = {
            "line_state": self.state,
            "good_count": self.good,
            "reject_count": self.reject,
            "rate_ppm": round(rate, 1),
            "target_rate_ppm": self.ideal_ppm,
            "cycle_time_s": round(60.0 / rate, 2) if rate > 0 else None,
        }
        return m, {"state": self.state.lower(), "alarms": alarms}


# =============================================================================== #
# 12. Indoor air quality (welding fume / forklift zones)
# =============================================================================== #
class AirQuality(Device):
    TYPE = "air_quality"
    CODE = "AQM"
    DEFAULT_INTERVAL = 60.0
    WIRELESS = True
    VENDORS = [("Airthings", "Space-Pro"), ("Milesight", "AM319"), ("Sensirion", "SEN66-EVAL")]
    AREAS = ["weld-shop", "paint-shop", "warehouse", "battery-charging-bay"]
    MODES = {
        "dust_event": (2.5, 900, 5400, "PM2.5 spike from grinding/welding"),
        "co_spike":   (2.0, 900, 3600, "CO elevated near charging/combustion"),
    }

    def _init_state(self) -> None:
        self.w("co2b", self.rng.uniform(430, 480), 3.0, 400, 550)
        self.w("vocb", self.rng.uniform(80, 200), 5.0, 40, 400)

    def _metrics(self, now: datetime, epoch: float, load: float) -> tuple[dict, dict]:
        alarms: list[str] = []
        dust = self.mode("dust_event")
        co_ev = self.mode("co_spike")
        co2 = self.co2b.next() + 520 * load + self.g(15)
        pm25 = 6 + 28 * load + abs(self.g(2))
        if dust:
            pm25 += 140 * dust.severity * (1 - dust.progress(epoch) * 0.7)
        co = 0.4 + 1.2 * load + abs(self.g(0.1))
        if co_ev:
            co += 24 * co_ev.severity
        if pm25 > 150:
            alarms.append("PM25_HIGH")
        if co > 25:
            alarms.append("CO_HIGH")
        if co2 > 1200:
            alarms.append("CO2_HIGH")
        m = {
            "co2_ppm": round(co2, 0),
            "pm25_ug_m3": round(pm25, 1),
            "pm10_ug_m3": round(pm25 * self.rng.uniform(1.6, 2.2), 1),
            "co_ppm": round(co, 2),
            "tvoc_ppb": round(self.vocb.next() + 150 * load + self.g(10), 0),
            "temperature_c": round(self.plant.indoor_c(now) + self.g(0.2), 1),
            "humidity_pct": round(clamp(50 + self.g(3), 20, 95), 1),
        }
        return m, {"state": "ok", "alarms": alarms}


DEVICE_CLASSES: list[type[Device]] = [
    EnergyMeter, VibrationSensor, EnvSensor, AirCompressor, Chiller,
    FlowMeter, TankLevel, Boiler, VFD, UPS, ProductionLine, AirQuality,
]
