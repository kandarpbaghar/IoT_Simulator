"""Runner — MQTT connections (one client per device), scheduler, ground truth.

Each device gets its own MQTT client (unique client-id, retained availability
topic, Last Will) so the broker sees a realistic fleet. A heap-based scheduler
publishes each device on its own interval with jitter. Anomaly ground truth is
published to <prefix>/_meta/anomaly and appended to a JSONL file so phase-2
monitoring detections can be validated against what was actually injected.
"""
from __future__ import annotations

import heapq
import json
import logging
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

from .anomalies import ActiveAnomaly
from .devices import Device
from .fleet import fleet_manifest
from .signals import Plant

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:  # pragma: no cover
    raise SystemExit("paho-mqtt is not installed. Run: ./venv/bin/pip install 'paho-mqtt>=2.0'")

LOG = logging.getLogger("plant-sim.runner")


class Runner:
    def __init__(self, cfg, plants: list[Plant], devices: list[Device]) -> None:
        self.cfg = cfg
        self.plants = plants
        self.devices = devices
        self.running = True
        self.clients: dict[str, mqtt.Client] = {}
        self.connected: set[str] = set()
        self.meta_client: mqtt.Client | None = None
        self.stats = {"published": 0, "publish_errors": 0, "skipped_offline": 0,
                      "anomalies_started": 0}
        self._birthed: set[str] = set()
        self.anomaly_file: Path | None = None
        if cfg.output_dir:
            try:
                out = Path(cfg.output_dir)
                out.mkdir(parents=True, exist_ok=True)
                self.anomaly_file = out / "anomaly_events.jsonl"
            except OSError as exc:
                LOG.warning("Cannot create output dir %s: %s", cfg.output_dir, exc)

        for dev in devices:
            dev.engine.on_event = self._make_event_cb(dev)

    # ------------------------------------------------------------------ MQTT -- #
    def _new_client(self, client_id: str, will_topic: str | None,
                    dev: Device | None = None) -> mqtt.Client:
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        if self.cfg.username:
            client.username_pw_set(self.cfg.username, self.cfg.password)
        if will_topic:
            client.will_set(will_topic, payload="offline", qos=1, retain=True)
        if self.cfg.use_tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED,
                           tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.reconnect_delay_set(min_delay=1, max_delay=120)

        def on_connect(cl, userdata, flags, rc, props=None):
            if rc == 0:
                if dev is not None:  # device clients only; meta client stays out
                    self.connected.add(client_id)
                    try:
                        cl.publish(dev.topic_status, "online", qos=1, retain=True)
                        # retained birth on EVERY (re)connect: self-heals the
                        # broker's retained store and serves late subscribers
                        cl.publish(dev.topic_meta,
                                   json.dumps(dev.birth_payload(time.time())),
                                   qos=1, retain=True)
                    except (ValueError, OSError) as exc:
                        LOG.warning("[%s] birth publish failed: %s", client_id, exc)
            else:
                LOG.error("[%s] connect failed rc=%s", client_id, rc)

        def on_disconnect(cl, userdata, flags, rc, props=None):
            self.connected.discard(client_id)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        return client

    def connect_all(self) -> None:
        """Stagger connections so 200 clients don't stampede the broker."""
        LOG.info("Connecting %d device clients to %s:%s (staggered)...",
                 len(self.devices), self.cfg.host, self.cfg.port)
        self.meta_client = self._new_client("plant-sim-meta", None)
        try:
            self.meta_client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
            self.meta_client.loop_start()
        except (OSError, ValueError) as exc:
            LOG.error("Meta client connect failed: %s", exc)

        for i, dev in enumerate(self.devices):
            client = self._new_client(dev.device_id, dev.topic_status, dev)
            self.clients[dev.device_id] = client
            try:
                client.connect_async(self.cfg.host, self.cfg.port, keepalive=90)
                client.loop_start()
            except (OSError, ValueError) as exc:
                LOG.error("[%s] connect_async failed: %s", dev.device_id, exc)
            if (i + 1) % 25 == 0:
                time.sleep(1.0)

        deadline = time.time() + 30
        while time.time() < deadline and len(self.connected) < len(self.devices):
            time.sleep(0.5)
        LOG.info("%d/%d device clients connected.",
                 len(self.connected), len(self.devices))
        if not self.connected:
            LOG.error("No clients connected — check broker host/credentials. "
                      "Continuing; paho will keep retrying in the background.")

    def publish_manifest(self) -> None:
        manifest = fleet_manifest(self.plants, self.devices)
        if self.cfg.output_dir:
            try:
                path = Path(self.cfg.output_dir) / "fleet_manifest.json"
                path.write_text(json.dumps(manifest, indent=2))
                LOG.info("Fleet manifest written to %s", path)
            except OSError as exc:
                LOG.warning("Could not write fleet manifest: %s", exc)
        if self.meta_client and not self.cfg.dry_run:
            try:
                self.meta_client.publish(
                    f"{self.cfg.topic_prefix}/_meta/fleet",
                    json.dumps(manifest), qos=1, retain=True)
                LOG.info("Fleet manifest published (retained) to %s/_meta/fleet",
                         self.cfg.topic_prefix)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Manifest publish failed: %s", exc)

    # ----------------------------------------------------- anomaly ground truth -- #
    def _make_event_cb(self, dev: Device):
        def cb(phase: str, anomaly: ActiveAnomaly) -> None:
            if phase == "start":
                self.stats["anomalies_started"] += 1
            event = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": phase,                      # start | end
                "plant": dev.plant.plant_id,
                "device_id": dev.device_id,
                "device_type": dev.TYPE,
                "kind": anomaly.kind,
                "severity": round(anomaly.severity, 2),
                "duration_s": int(anomaly.until - anomaly.started),
                "expected_symptom": dev.engine.symptom(anomaly.kind),
            }
            LOG.info("ANOMALY %-5s %-18s on %s (%s) sev=%.2f dur=%ds — %s",
                     phase.upper(), anomaly.kind, dev.device_id, dev.plant.plant_id,
                     anomaly.severity, event["duration_s"], event["expected_symptom"])
            if self.anomaly_file:
                try:
                    with self.anomaly_file.open("a") as fh:
                        fh.write(json.dumps(event) + "\n")
                except OSError as exc:
                    LOG.debug("anomaly file write failed: %s", exc)
            if self.meta_client and not self.cfg.dry_run:
                try:
                    self.meta_client.publish(
                        f"{self.cfg.topic_prefix}/_meta/anomaly",
                        json.dumps(event), qos=1, retain=False)
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("anomaly meta publish failed: %s", exc)
        return cb

    # ------------------------------------------------------------- scheduler -- #
    def run(self) -> int:
        if not self.cfg.dry_run:
            self.connect_all()
        self.publish_manifest()

        start = time.time()
        # spread first publishes across each device's interval so load is smooth
        heap: list[tuple[float, int]] = []
        for i, dev in enumerate(self.devices):
            heapq.heappush(heap, (start + dev.rng.uniform(0.5, dev.interval), i))

        next_stats = start + 60
        LOG.info("Publishing telemetry for %d devices%s. Ctrl-C to stop.",
                 len(self.devices),
                 f" for {self.cfg.duration:.0f}s" if self.cfg.duration else "")
        try:
            while self.running:
                now_epoch = time.time()
                if self.cfg.duration and now_epoch - start >= self.cfg.duration:
                    LOG.info("Duration limit reached.")
                    break
                if now_epoch >= next_stats:
                    self._log_stats()
                    next_stats += 60

                t_next, idx = heap[0]
                if t_next > now_epoch:
                    time.sleep(min(t_next - now_epoch, 0.5))
                    continue
                heapq.heappop(heap)
                dev = self.devices[idx]
                self._tick_device(dev)
                heapq.heappush(
                    heap, (t_next + dev.interval * dev.rng.uniform(0.97, 1.03), idx))
        except KeyboardInterrupt:
            LOG.info("Interrupted.")
        finally:
            self.shutdown()
        return 0

    def _tick_device(self, dev: Device) -> None:
        now_epoch = time.time()
        now = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
        if self.cfg.dry_run and dev.device_id not in self._birthed:
            # no connect events in dry-run — show the retained birth once
            self._birthed.add(dev.device_id)
            LOG.info("DRY %s %s (retained)", dev.topic_meta,
                     json.dumps(dev.birth_payload(now_epoch)))
        for topic, payload, retain in dev.tick(now, now_epoch):
            body = json.dumps(payload, allow_nan=False)
            if self.cfg.dry_run:
                LOG.info("DRY %s %s", topic, body)
                self.stats["published"] += 1
                continue
            client = self.clients.get(dev.device_id)
            if client is None or dev.device_id not in self.connected:
                self.stats["skipped_offline"] += 1
                continue
            try:
                res = client.publish(topic, body, qos=self.cfg.qos, retain=retain)
                if res.rc == mqtt.MQTT_ERR_SUCCESS:
                    self.stats["published"] += 1
                else:
                    self.stats["publish_errors"] += 1
            except (ValueError, OSError) as exc:
                self.stats["publish_errors"] += 1
                LOG.debug("[%s] publish failed: %s", dev.device_id, exc)

    def _log_stats(self) -> None:
        active = [(d.device_id, k) for d in self.devices
                  for k in d.engine.active_map]
        LOG.info("STATS published=%d errors=%d skipped=%d connected=%d/%d "
                 "active_anomalies=%d anomalies_started=%d",
                 self.stats["published"], self.stats["publish_errors"],
                 self.stats["skipped_offline"], len(self.connected),
                 len(self.devices), len(active), self.stats["anomalies_started"])
        if active:
            sample = ", ".join(f"{d}:{k}" for d, k in active[:8])
            LOG.info("  e.g. %s%s", sample, " ..." if len(active) > 8 else "")

    def stop(self, *_args) -> None:
        self.running = False

    def shutdown(self) -> None:
        LOG.info("Shutting down %d clients...", len(self.clients))
        for dev in self.devices:
            client = self.clients.get(dev.device_id)
            if client and dev.device_id in self.connected:
                try:
                    client.publish(dev.topic_status, "offline", qos=1, retain=True)
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(0.5)  # let QoS1 flush
        for client in self.clients.values():
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self.meta_client:
            try:
                self.meta_client.loop_stop()
                self.meta_client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._log_stats()
        LOG.info("Stopped.")
