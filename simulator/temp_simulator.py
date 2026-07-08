#!/usr/bin/env python3.12
"""
Temperature simulator — publishes dummy temperature readings over MQTT.

Purpose: validate the full pipeline (device -> MQTT broker -> Home Assistant)
before wiring a real sensor. Publishes to a Mosquitto broker and announces itself
via Home Assistant MQTT Discovery so the sensor appears automatically in HA.

POC defaults: plaintext MQTT on 1883 with username/password auth.
Production hooks (TLS on 8883, CA/client certs) are wired but off by default —
see the --tls flag and MQTT_* / TLS_* environment variables.

Config precedence: command-line args > environment variables > built-in defaults.
Load a config/simulator.env file first with:  set -a; . config/simulator.env; set +a
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import ssl
import sys
import time
from dataclasses import dataclass

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    sys.exit("paho-mqtt is not installed. Run: ./venv/bin/pip install 'paho-mqtt>=2.0'")

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
LOG = logging.getLogger("temp-simulator")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    host: str
    port: int
    username: str | None
    password: str | None
    interval: float
    device_id: str
    device_name: str
    discovery_prefix: str
    # temperature model
    base_temp: float
    drift: float
    tmin: float
    tmax: float
    # TLS (production)
    use_tls: bool
    ca_cert: str | None
    client_cert: str | None
    client_key: str | None
    insecure_tls: bool
    # run control
    once: bool

    @property
    def base_topic(self) -> str:
        return f"home/{self.device_id}"

    @property
    def state_topic(self) -> str:
        return f"{self.base_topic}/temperature"

    @property
    def availability_topic(self) -> str:
        return f"{self.base_topic}/status"

    @property
    def discovery_topic(self) -> str:
        # HA listens here for sensor auto-config
        return f"{self.discovery_prefix}/sensor/{self.device_id}/temperature/config"


def parse_config(argv: list[str]) -> Config:
    p = argparse.ArgumentParser(description="MQTT temperature simulator")
    p.add_argument("--host", default=_env("MQTT_HOST", "localhost"),
                   help="MQTT broker host (env MQTT_HOST)")
    p.add_argument("--port", type=int, default=int(_env("MQTT_PORT", "1883")),
                   help="MQTT broker port (env MQTT_PORT)")
    p.add_argument("--username", default=_env("MQTT_USERNAME", "") or None,
                   help="MQTT username (env MQTT_USERNAME)")
    p.add_argument("--password", default=_env("MQTT_PASSWORD", "") or None,
                   help="MQTT password (env MQTT_PASSWORD)")
    p.add_argument("--interval", type=float, default=float(_env("PUBLISH_INTERVAL", "300")),
                   help="Seconds between readings (env PUBLISH_INTERVAL, default 300 = 5 min)")
    p.add_argument("--device-id", default=_env("DEVICE_ID", "sim_temp_01"),
                   help="Unique device id (env DEVICE_ID)")
    p.add_argument("--device-name", default=_env("DEVICE_NAME", "Simulated Temp Sensor"),
                   help="Friendly name shown in HA (env DEVICE_NAME)")
    p.add_argument("--discovery-prefix", default=_env("DISCOVERY_PREFIX", "homeassistant"),
                   help="HA MQTT discovery prefix (env DISCOVERY_PREFIX)")
    p.add_argument("--base-temp", type=float, default=float(_env("BASE_TEMP", "22.0")))
    p.add_argument("--drift", type=float, default=float(_env("DRIFT", "0.3")),
                   help="Max change per reading, degrees C")
    p.add_argument("--tmin", type=float, default=float(_env("TMIN", "16.0")))
    p.add_argument("--tmax", type=float, default=float(_env("TMAX", "30.0")))
    p.add_argument("--tls", action="store_true", default=_env("USE_TLS", "false").lower() == "true",
                   help="Enable TLS (env USE_TLS=true). Use with --port 8883.")
    p.add_argument("--ca-cert", default=_env("TLS_CA_CERT", "") or None)
    p.add_argument("--client-cert", default=_env("TLS_CLIENT_CERT", "") or None)
    p.add_argument("--client-key", default=_env("TLS_CLIENT_KEY", "") or None)
    p.add_argument("--insecure-tls", action="store_true",
                   default=_env("TLS_INSECURE", "false").lower() == "true",
                   help="Skip server cert hostname/CA check (POC only, never in prod)")
    p.add_argument("--once", action="store_true", help="Publish a single reading and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    setup_logging(a.verbose)
    return Config(
        host=a.host, port=a.port, username=a.username, password=a.password,
        interval=a.interval, device_id=a.device_id, device_name=a.device_name,
        discovery_prefix=a.discovery_prefix, base_temp=a.base_temp, drift=a.drift,
        tmin=a.tmin, tmax=a.tmax, use_tls=a.tls, ca_cert=a.ca_cert,
        client_cert=a.client_cert, client_key=a.client_key,
        insecure_tls=a.insecure_tls, once=a.once,
    )


# --------------------------------------------------------------------------- #
# Temperature model — smooth random walk clamped to [tmin, tmax]
# --------------------------------------------------------------------------- #
class TempModel:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.current = cfg.base_temp

    def next(self) -> float:
        step = random.uniform(-self.cfg.drift, self.cfg.drift)
        self.current = max(self.cfg.tmin, min(self.cfg.tmax, self.current + step))
        return round(self.current, 2)


# --------------------------------------------------------------------------- #
# MQTT plumbing
# --------------------------------------------------------------------------- #
class Simulator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.model = TempModel(cfg)
        self._running = True
        self._connected = False

        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=cfg.device_id,
            clean_session=True,
        )
        if cfg.username:
            self.client.username_pw_set(cfg.username, cfg.password)

        # Last Will: broker marks us offline if we drop unexpectedly
        self.client.will_set(cfg.availability_topic, payload="offline", qos=1, retain=True)

        if cfg.use_tls:
            self._configure_tls()

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        # auto-reconnect backoff
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    def _configure_tls(self) -> None:
        try:
            self.client.tls_set(
                ca_certs=self.cfg.ca_cert,
                certfile=self.cfg.client_cert,
                keyfile=self.cfg.client_key,
                cert_reqs=ssl.CERT_NONE if self.cfg.insecure_tls else ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            if self.cfg.insecure_tls:
                self.client.tls_insecure_set(True)
                LOG.warning("TLS certificate verification DISABLED (POC only).")
            LOG.info("TLS enabled.")
        except (ssl.SSLError, FileNotFoundError, ValueError) as exc:
            LOG.error("TLS configuration failed: %s", exc)
            raise

    # -- logging helpers ---------------------------------------------------- #
    def _log_topics(self) -> None:
        """Print the full topic map so it's obvious where each message goes."""
        LOG.info("MQTT topics in use for device '%s':", self.cfg.device_id)
        LOG.info("  state (readings) : %s", self.cfg.state_topic)
        LOG.info("  availability     : %s", self.cfg.availability_topic)
        LOG.info("  HA discovery     : %s", self.cfg.discovery_topic)

    # -- callbacks ---------------------------------------------------------- #
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            self._connected = True
            LOG.info("Connected to broker %s:%s", self.cfg.host, self.cfg.port)
            self._log_topics()
            self._publish_discovery()
            client.publish(self.cfg.availability_topic, "online", qos=1, retain=True)
        else:
            self._connected = False
            LOG.error("Connect failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._connected = False
        if reason_code != 0:
            LOG.warning("Unexpected disconnect (%s). Auto-reconnecting...", reason_code)

    # -- publishing --------------------------------------------------------- #
    def _publish_discovery(self) -> None:
        """Announce this sensor to Home Assistant via MQTT Discovery."""
        payload = {
            "name": "Temperature",
            "unique_id": f"{self.cfg.device_id}_temperature",
            "state_topic": self.cfg.state_topic,
            "availability_topic": self.cfg.availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_class": "measurement",
            "expire_after": int(max(self.cfg.interval * 3, 60)),
            "device": {
                "identifiers": [self.cfg.device_id],
                "name": self.cfg.device_name,
                "manufacturer": "IoT POC",
                "model": "Simulator v1",
            },
        }
        res = self.client.publish(
            self.cfg.discovery_topic, json.dumps(payload), qos=1, retain=True
        )
        if res.rc == mqtt.MQTT_ERR_SUCCESS:
            LOG.info("Published HA discovery config to %s", self.cfg.discovery_topic)
        else:
            LOG.error("Discovery publish failed rc=%s", res.rc)

    def _publish_reading(self) -> None:
        temp = self.model.next()
        res = self.client.publish(self.cfg.state_topic, str(temp), qos=1, retain=False)
        if res.rc == mqtt.MQTT_ERR_SUCCESS:
            LOG.info("Published %.2f °C -> %s", temp, self.cfg.state_topic)
        else:
            LOG.error("Publish failed rc=%s (temp=%.2f)", res.rc, temp)

    # -- lifecycle ---------------------------------------------------------- #
    def stop(self, *_args) -> None:
        LOG.info("Shutting down...")
        self._running = False

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        self._log_topics()
        try:
            self.client.connect(self.cfg.host, self.cfg.port, keepalive=60)
        except (OSError, ConnectionRefusedError) as exc:
            LOG.error("Could not reach broker %s:%s — %s",
                      self.cfg.host, self.cfg.port, exc)
            return 1

        self.client.loop_start()

        # wait briefly for the connection so the first reading isn't dropped
        for _ in range(50):
            if self._connected:
                break
            time.sleep(0.1)

        try:
            if self.cfg.once:
                self._publish_reading()
                time.sleep(0.5)  # let QoS1 flush
            else:
                LOG.info("Publishing every %.0f s. Ctrl-C to stop.", self.cfg.interval)
                next_at = time.monotonic()
                while self._running:
                    if self._connected:
                        self._publish_reading()
                    else:
                        LOG.debug("Not connected; skipping this tick.")
                    next_at += self.cfg.interval
                    # sleep in small slices so Ctrl-C is responsive
                    while self._running and time.monotonic() < next_at:
                        time.sleep(min(0.5, next_at - time.monotonic()))
        finally:
            try:
                self.client.publish(self.cfg.availability_topic, "offline",
                                    qos=1, retain=True)
                time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001 - best effort on shutdown
                LOG.debug("Offline publish on shutdown failed: %s", exc)
            self.client.loop_stop()
            self.client.disconnect()
        LOG.info("Stopped.")
        return 0


def main() -> int:
    cfg = parse_config(sys.argv[1:])
    LOG.info("Simulator config: broker=%s:%s device=%s tls=%s interval=%ss",
             cfg.host, cfg.port, cfg.device_id, cfg.use_tls, cfg.interval)
    return Simulator(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
