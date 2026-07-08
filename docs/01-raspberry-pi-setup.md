# Phase 1 — Bring up the Raspberry Pi

Goal: a freshly imaged Pi that you can use **either way** —
plug in a monitor + keyboard when you want a desktop, **or** run it headless and
reach it over SSH from your Mac. Both work from the same image.

The trick is: install the **full Raspberry Pi OS (Desktop)** *and* pre-configure
**SSH + WiFi + hostname** while flashing. That gives you the desktop when a monitor
is attached and remote access when it isn't.

---

## What you need

- Raspberry Pi (Pi 3 / 4 / 5 / Zero 2 W all fine) + power supply
- microSD card (16 GB+), and the SD card reader for your Mac
- Your Mac (macOS) — used to flash the card
- Your WiFi network name (SSID) and password
- (Optional) HDMI monitor, keyboard, mouse for the desktop path

> On a Pi 4/5 the HDMI is **micro-HDMI** — you need a micro-HDMI→HDMI cable/adapter.
> Pi Zero 2 W uses **mini-HDMI**.

---

## Step 1 — Install Raspberry Pi Imager on your Mac

Raspberry Pi Imager is not currently installed. Install it one of two ways:

```bash
# Option A — Homebrew (if you have brew)
brew install --cask raspberry-pi-imager

# Option B — download the .dmg
open https://www.raspberrypi.com/software/
```

After install, launch **Raspberry Pi Imager** (it's a GUI app).

---

## Step 2 — Choose OS and storage

In Raspberry Pi Imager:

1. **CHOOSE DEVICE** → pick your Pi model (or skip if not listed).
2. **CHOOSE OS** →
   `Raspberry Pi OS (other)` → **`Raspberry Pi OS (64-bit)`**
   (the full version *with desktop* — so a monitor gives you a GUI).
   - 64-bit works on Pi 3, 4, 5, Zero 2 W.
3. **CHOOSE STORAGE** → select your microSD card.
   ⚠️ Double-check you picked the SD card, not an external drive — flashing erases it.

Do **not** click Write yet — first do Step 3 (the pre-config).

---

## Step 3 — Pre-configure (this is what makes it work headless AND with a monitor)

Click **NEXT**, then **EDIT SETTINGS** (older versions: the ⚙️ gear icon).

### General tab
- **Set hostname:** `iot-pi`  → you'll reach it as `iot-pi.local`
- **Set username and password:**
  - username: `pi` (or your choice — remember it)
  - password: choose a strong one (remember it)
- **Configure wireless LAN:**
  - SSID: *your WiFi name*
  - Password: *your WiFi password*
  - Wireless LAN country: your country code (e.g. `US`, `IN`, `GB`) — **required** or WiFi stays off
- **Set locale settings:** your timezone + keyboard layout

### Services tab
- ✅ **Enable SSH** → choose **Use password authentication** for now
  (we switch to SSH keys in the production hardening step).

### Options tab
- (optional) enable "Eject media when finished".

Click **SAVE**, then **YES** to apply the customisation, then **YES** to erase and write.
Wait for write + verify to finish, then remove the card.

> Why this matters: with SSH + WiFi baked in, the Pi joins your network and accepts
> SSH on first boot with **no monitor needed**. And because we installed the Desktop
> image, plugging in a monitor still gives you a full GUI. You get both.

---

## Step 4 — First boot

1. Insert the microSD into the Pi.
2. (Optional) plug in monitor + keyboard if you want to watch the desktop come up.
3. Connect power.
4. First boot takes **1–3 minutes** (it expands the filesystem and reboots once).
   The green LED flickering = disk activity; wait for it to settle.

Make sure your **Mac is on the same WiFi network** you configured.

---

## Step 5 — Connect from your Mac (headless path)

Find and reach the Pi by its hostname:

```bash
# Confirm it's on the network (mDNS)
ping -c 3 iot-pi.local

# SSH in (use the username you set)
ssh pi@iot-pi.local
```

Type `yes` to accept the host key, then your password.

**If `iot-pi.local` doesn't resolve** (some networks block mDNS), find the Pi's IP:

```bash
# Look for a Raspberry Pi vendor (b8:27:eb / dc:a6:32 / e4:5f:01 / d8:3a:dd)
arp -a | grep -i -E "b8:27:eb|dc:a6:32|e4:5f:01|d8:3a:dd"
```

Then `ssh pi@<that-ip>`. (Or check your router's DHCP client list for `iot-pi`.)

You're in when the prompt shows `pi@iot-pi:~ $`.

---

## Step 6 — Update the OS and set a static-ish address

Once connected (over SSH or in the desktop Terminal):

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

Reconnect after the reboot. Recommended: give the Pi a **DHCP reservation** in your
router (bind its MAC to a fixed IP) so its address never changes — cleaner than a
static IP on the Pi for a POC.

Check the MAC to reserve:

```bash
ip link show wlan0 | awk '/link\/ether/ {print $2}'
```

---

## Step 7 — Install Python MQTT tooling (prep for Phase 3)

The Pi already has Python 3. Install pip and the MQTT client so the simulator/sensor
code can run here later:

```bash
sudo apt install -y python3-pip python3-venv
python3 --version        # confirm 3.11+ on current Raspberry Pi OS (Bookworm)
```

We'll create a project venv on the Pi in Phase 3.

---

## ✅ Phase 1 done when…

- [ ] `ssh pi@iot-pi.local` logs you in from your Mac (headless works)
- [ ] Plugging in a monitor shows the Raspberry Pi desktop (monitored works)
- [ ] `sudo apt update` runs clean (internet works)
- [ ] You noted the Pi's IP / set a DHCP reservation

Next: **Phase 2 — Home Assistant + Mosquitto in the cloud**
(`docs/02-home-assistant-mosquitto.md`).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ping iot-pi.local` fails | Mac + Pi on same WiFi? Try the IP via `arp -a`. Wait full 3 min on first boot. |
| WiFi never connects | Wrong SSID/password, or the **WLAN country** was left blank in Imager. Re-flash with it set. |
| SSH "connection refused" | SSH wasn't enabled in Imager Services tab. Re-flash, or add an empty file named `ssh` to the boot partition. |
| SSH "host key changed" warning | You re-flashed. `ssh-keygen -R iot-pi.local` then reconnect. |
| No desktop on monitor | You flashed the Lite image. Re-flash with **Raspberry Pi OS (64-bit)** *with desktop*. Check micro/mini-HDMI adapter. |
