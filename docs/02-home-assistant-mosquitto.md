# Phase 2 — Mosquitto broker on the cloud + Home Assistant MQTT

Your setup: **Home Assistant already runs as a container/Core on a cloud VM.** We add
a **Mosquitto** broker container beside it, secure it with a username/password, and
connect Home Assistant's MQTT integration to it. POC exposure choice: **port 1883
open, protected by password** (see the security note at the bottom).

Broker users we create:
| User | Purpose | Rights (ACL) |
|------|---------|--------------|
| `hass` | Home Assistant backend | full read/write |
| `iot`  | Pi sensor / simulator  | publish-only to `home/#` and `homeassistant/#` |

POC passwords live in `config/secrets.env` (gitignored). Change them anytime.

---

## Step 1 — Put the broker files on the VM

Copy the `config/` broker tree from this repo to the VM (adjust user/host):

```bash
# from your Mac, in the project root
scp -r config/mosquitto config/docker-compose.mosquitto.yml \
    <vmuser>@<cloud_vm_ip>:~/broker/
```

So on the VM you have:
```
~/broker/
├── docker-compose.mosquitto.yml
└── mosquitto/
    ├── config/{mosquitto.conf, acl}
    ├── data/
    └── log/
```

---

## Step 2 — Create the password file

On the VM (uses the mosquitto image itself, so you don't need mosquitto installed):

```bash
cd ~/broker
# create the file with the hass user (-c creates/overwrites)
docker run --rm -v "$PWD/mosquitto/config:/mosquitto/config" eclipse-mosquitto:2 \
  mosquitto_passwd -c -b /mosquitto/config/passwd hass 'HassBroker@26'
# add the iot user (no -c, so it appends)
docker run --rm -v "$PWD/mosquitto/config:/mosquitto/config" eclipse-mosquitto:2 \
  mosquitto_passwd -b /mosquitto/config/passwd iot 'IoTsensor@26'

# confirm two lines (hashed)
cat mosquitto/config/passwd
```

> Use the exact passwords from `config/secrets.env`. If you change them, update
> `secrets.env` and the HA integration + simulator too.

---

## Step 3 — Start the broker

```bash
cd ~/broker
docker compose -f docker-compose.mosquitto.yml up -d
docker logs mosquitto        # expect: "mosquitto version 2.x running"
```

If you'd rather add it to your **existing HA compose file**, paste the `mosquitto`
service block from `docker-compose.mosquitto.yml` into that file (same network as HA)
and `docker compose up -d`.

---

## Step 4 — Open port 1883 (POC: password-only)

Two layers usually apply — the VM firewall **and** the cloud provider's security group.

```bash
# Ubuntu/Debian host firewall (if ufw is active)
sudo ufw allow 1883/tcp
```

Then in your cloud console (AWS SG / DigitalOcean / Azure NSG / GCP firewall) allow
**inbound TCP 1883**. You chose *open with password*, so source `0.0.0.0/0`.

> ⚠️ This exposes the broker to the internet; the only thing protecting it is the
> password + ACL. Acceptable for a short-lived POC. **Hardening (Phase: security):**
> switch to TLS on 8883, add per-device client certs, and either firewall 1883 to
> known IPs or move it behind a WireGuard VPN. Tracked in `docs/00-project-overview.md`.

---

## Step 5 — Verify the broker

**On the VM** (loopback):
```bash
docker run --rm -it eclipse-mosquitto:2 mosquitto_sub \
  -h 172.17.0.1 -p 1883 -u iot -P 'IoTsensor@26' -t 'test/#' -v &
docker run --rm -it eclipse-mosquitto:2 mosquitto_pub \
  -h 172.17.0.1 -p 1883 -u hass -P 'HassBroker@26' -t 'test/hi' -m 'ok'
# the subscriber should print: test/hi ok
```

**From your Mac** (proves the internet path the Pi will use) — `mosquitto_sub` is
already installed locally:
```bash
mosquitto_sub -h <cloud_vm_ip> -p 1883 -u iot -P 'IoTsensor@26' -t '#' -v
# leave running; you'll see messages once the simulator publishes (Step 7)
```
If this connects, the Pi will be able to reach the broker too.

---

## Step 6 — Connect Home Assistant to the broker

In the Home Assistant web UI:

1. **Settings → Devices & Services → Add Integration → MQTT**.
2. Broker connection:
   - **Broker:** which address depends on how HA reaches the broker —

     | HA and Mosquitto are… | Use broker address |
     |------------------------|--------------------|
     | in the **same compose file / docker network** | `mosquitto` |
     | separate containers on the same VM | `172.17.0.1` (Docker bridge gateway) |
     | HA on host networking | `127.0.0.1` |

   - **Port:** `1883`
   - **Username:** `hass`
   - **Password:** `HassBroker@26`
3. Submit. HA should show **Connected**.
4. Ensure **MQTT Discovery is enabled** (it is by default) so our simulator's sensor
   auto-appears — no YAML needed.

> Prefer `configuration.yaml`? On modern HA the MQTT *connection* is UI-only; the UI
> path above is correct. Discovery prefix stays the default `homeassistant`.

---

## Step 7 — End-to-end test with the simulator (from your Mac)

Point the simulator at the cloud broker and publish:

```bash
# in the project root on your Mac
cp config/simulator.env.example config/simulator.env    # if not done yet
# edit config/simulator.env: set MQTT_HOST=<cloud_vm_ip>  (password already = IoTsensor@26)

set -a; . config/simulator.env; set +a
# fast loop so you see it immediately (5s instead of 5min)
./venv/bin/python simulator/temp_simulator.py --interval 5 -v
```

Expected:
- The simulator logs `Connected` + `Published … °C`.
- Your `mosquitto_sub` window (Step 5) shows the discovery config + readings.
- In Home Assistant: **Settings → Devices & Services → MQTT → devices** shows
  **"Simulated Temp Sensor"** with a **Temperature** entity updating every 5 s.

Once that works, stop the fast loop and run it at the real cadence
(`--interval 300`, the default) — that's the 5-minute telemetry the real Pi sensor
will replace in Phase 3.

---

## ✅ Phase 2 done when…
- [ ] `docker logs mosquitto` shows it running
- [ ] `mosquitto_sub` from your Mac connects to `<cloud_vm_ip>:1883`
- [ ] HA MQTT integration shows **Connected**
- [ ] Running the simulator makes **"Simulated Temp Sensor"** appear in HA and update

Next: **Phase 3** — run the simulator on the Pi as a service, then swap in the real
temperature sensor.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| HA MQTT "failed to connect" | Wrong broker address — try `172.17.0.1`, `mosquitto`, or the VM private IP per the table. Check `docker logs mosquitto`. |
| `mosquitto_sub` from Mac times out | Port 1883 not open in the cloud security group, or host `ufw`. Re-check Step 4. |
| "Connection Refused: not authorised" | Wrong user/password, or `passwd` file wasn't created. Re-run Step 2, then `docker restart mosquitto`. |
| Broker logs "Error: Unable to open pwfile" | Volume path wrong or file missing. Confirm `mosquitto/config/passwd` exists on the VM. |
| Sensor doesn't appear in HA | MQTT Discovery disabled, or discovery prefix mismatch. Confirm simulator `DISCOVERY_PREFIX=homeassistant` and HA discovery on. |
