#!/usr/bin/env python3.12
"""CLI entry point:  python -m plant_sim [options]

Config precedence: command-line args > environment variables > defaults.
Load the env file first:  set -a; . config/plant_simulator.env; set +a
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from dataclasses import dataclass

from .fleet import build_fleet
from .runner import Runner

LOG = logging.getLogger("plant-sim")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    host: str
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    qos: int
    topic_prefix: str
    devices: int | None
    interval_scale: float
    anomaly_rate: float
    seed: int
    duration: float | None
    dry_run: bool
    output_dir: str | None


def parse_config(argv: list[str]) -> Config:
    p = argparse.ArgumentParser(
        prog="plant_sim",
        description="Multi-plant energy & plant-health MQTT device fleet simulator")
    p.add_argument("--host", default=_env("MQTT_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(_env("MQTT_PORT", "1883")))
    p.add_argument("--username", default=_env("MQTT_USERNAME", "") or None)
    p.add_argument("--password", default=_env("MQTT_PASSWORD", "") or None)
    p.add_argument("--tls", action="store_true",
                   default=_env("USE_TLS", "false").lower() == "true")
    p.add_argument("--qos", type=int, choices=(0, 1), default=int(_env("MQTT_QOS", "1")))
    p.add_argument("--prefix", default=_env("TOPIC_PREFIX", "home/plants"),
                   help="Topic prefix. NOTE: the 'iot' broker user may only write "
                        "under home/# — keep the prefix inside that namespace.")
    p.add_argument("--devices", type=int,
                   default=int(_env("FLEET_DEVICES", "0")) or None,
                   help="Cap total devices (balanced subset). Default: full 200.")
    p.add_argument("--interval-scale", type=float,
                   default=float(_env("INTERVAL_SCALE", "1.0")),
                   help="Multiply all publish intervals (2.0 = half the msg rate)")
    p.add_argument("--anomaly-rate", type=float,
                   default=float(_env("ANOMALY_RATE", "1.5")),
                   help="Anomaly events per device per day (chronic devices get 4x)")
    p.add_argument("--seed", type=int, default=int(_env("SIM_SEED", "42")))
    p.add_argument("--duration", type=float,
                   default=float(_env("SIM_DURATION", "0")) or None,
                   help="Auto-stop after N seconds (default: run until Ctrl-C)")
    p.add_argument("--dry-run", action="store_true",
                   help="No broker — print payloads to the log instead")
    p.add_argument("--output-dir", default=_env(
        "SIM_OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "..", "..",
                                       "test-script", "output")),
        help="Where fleet_manifest.json and anomaly_events.jsonl are written")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    if not a.prefix.startswith("home/"):
        LOG.warning("Topic prefix %r is outside home/# — the 'iot' broker user's "
                    "ACL is write-only under home/#; messages may be silently "
                    "dropped by the broker.", a.prefix)
    return Config(
        host=a.host, port=a.port, username=a.username, password=a.password,
        use_tls=a.tls, qos=a.qos, topic_prefix=a.prefix.rstrip("/"),
        devices=a.devices, interval_scale=a.interval_scale,
        anomaly_rate=a.anomaly_rate, seed=a.seed, duration=a.duration,
        dry_run=a.dry_run, output_dir=os.path.abspath(a.output_dir))


def main(argv: list[str] | None = None) -> int:
    cfg = parse_config(sys.argv[1:] if argv is None else argv)
    LOG.info("plant_sim: broker=%s:%s prefix=%s qos=%d devices=%s "
             "interval_scale=%.2f anomaly_rate=%.2f seed=%d dry_run=%s",
             cfg.host, cfg.port, cfg.topic_prefix, cfg.qos,
             cfg.devices or "all(200)", cfg.interval_scale, cfg.anomaly_rate,
             cfg.seed, cfg.dry_run)
    try:
        plants, devices = build_fleet(
            seed=cfg.seed, anomaly_rate=cfg.anomaly_rate,
            interval_scale=cfg.interval_scale, topic_prefix=cfg.topic_prefix,
            max_devices=cfg.devices)
    except Exception:  # noqa: BLE001
        LOG.exception("Fleet build failed")
        return 1

    runner = Runner(cfg, plants, devices)
    signal.signal(signal.SIGINT, lambda *_: runner.stop())
    signal.signal(signal.SIGTERM, lambda *_: runner.stop())
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
