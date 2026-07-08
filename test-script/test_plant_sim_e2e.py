#!/usr/bin/env python3.12
"""End-to-end test: run a small plant_sim fleet against the live broker and
verify messages actually arrive by subscribing with the 'hass' user.

(The 'iot' publisher user is write-only under home/# — subscribers MUST use hass.)

Usage:
    set -a; . config/secrets.env; . config/plant_simulator.env; set +a
    ./venv/bin/python test-script/test_plant_sim_e2e.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("MQTT_HOST", "80.225.229.166")
PORT = int(os.environ.get("MQTT_PORT", "1883"))
SUB_USER = os.environ.get("HASS_MQTT_USER", "hass")
SUB_PASS = os.environ.get("HASS_MQTT_PASSWORD", "")
PREFIX = os.environ.get("TOPIC_PREFIX", "home/plants")

DEVICES = 12
RUN_SECONDS = 35

received: list[tuple[str, dict]] = []
errors: list[str] = []
lock = threading.Lock()


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {"_raw": msg.payload[:60].decode(errors="replace")}
    with lock:
        received.append((msg.topic, payload))


def main() -> int:
    if not SUB_PASS:
        print("ERROR: HASS_MQTT_PASSWORD not set — load config/secrets.env first")
        return 2

    sub = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                      client_id="plant-sim-e2e-sub")
    sub.username_pw_set(SUB_USER, SUB_PASS)
    sub.on_message = on_message
    connected = threading.Event()
    sub.on_connect = lambda c, u, f, rc, p=None: (
        connected.set(), c.subscribe(f"{PREFIX}/#", qos=1))
    try:
        sub.connect(HOST, PORT, keepalive=30)
    except OSError as exc:
        print(f"ERROR: cannot reach broker {HOST}:{PORT} — {exc}")
        return 2
    sub.loop_start()
    if not connected.wait(10):
        print("ERROR: subscriber failed to connect within 10s")
        return 2
    print(f"Subscriber connected to {HOST}:{PORT}, watching {PREFIX}/#")

    proc = subprocess.Popen(
        [str(ROOT / "venv/bin/python"), "-m", "plant_sim",
         "--devices", str(DEVICES), "--interval-scale", "0.2",
         "--duration", str(RUN_SECONDS), "--anomaly-rate", "2000"],
        cwd=str(ROOT / "simulator"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = proc.communicate(timeout=RUN_SECONDS + 90)
    time.sleep(2)
    sub.loop_stop()
    sub.disconnect()

    with lock:
        msgs = list(received)
    data_msgs = [(t, p) for t, p in msgs if t.endswith("/data")]
    status_msgs = [(t, p) for t, p in msgs if t.endswith("/status")]
    meta_msgs = [(t, p) for t, p in msgs if "/_meta/" in t]
    birth_msgs = [(t, p) for t, p in msgs
                  if t.endswith("/meta") and "/_meta/" not in t]
    by_type = Counter(t.split("/")[3] for t, _ in data_msgs)
    by_plant = Counter(t.split("/")[2] for t, _ in data_msgs)

    print(f"\nreceived total={len(msgs)} data={len(data_msgs)} "
          f"status={len(status_msgs)} births={len(birth_msgs)} "
          f"meta={len(meta_msgs)}")
    print(f"by_type={dict(by_type)}")
    print(f"by_plant={dict(by_plant)}")

    ok = True
    if proc.returncode != 0:
        ok = False
        errors.append(f"simulator exit code {proc.returncode}")
        print(out[-3000:])
    if len(data_msgs) < DEVICES * 2:
        ok = False
        errors.append(f"too few data messages: {len(data_msgs)}")
    for t, p in data_msgs[:200]:
        if "_raw" in p:
            ok = False
            errors.append(f"non-JSON payload on {t}")
            break
        for key in ("ts", "ts_epoch_ms", "seq", "uptime_s", "metrics", "status"):
            if key not in p:
                ok = False
                errors.append(f"missing lean envelope key {key!r} on {t}")
    # retained birth messages must carry the master data
    if not birth_msgs:
        ok = False
        errors.append("no retained birth (/meta) messages seen")
    for t, p in birth_msgs[:50]:
        for key in ("device_id", "plant", "area", "device_type", "vendor",
                    "model", "fw"):
            if key not in p:
                ok = False
                errors.append(f"missing birth key {key!r} on {t}")
    if not any(p == {"_raw": "online"} or str(p.get("_raw", "")) == "online"
               for _, p in status_msgs) and not status_msgs:
        errors.append("no availability/status messages seen")
        ok = False

    if errors:
        print("FAILURES:", *errors, sep="\n  - ")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
