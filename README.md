# LL-Connect-Wireless (no-whiz fork)

A Linux daemon for controlling **Lian Li wireless fans** (V1 USB dongles, VID `0x0416`, PIDs `0x8040` TX / `0x8041` RX). This is a fork of [Yoinky3000/LL-Connect-Wireless](https://github.com/Yoinky3000/LL-Connect-Wireless) that fixes the audible periodic "whizzing" the upstream daemon (and every other Linux Lian-Li daemon I tried) exhibits in steady state, plus auto-recovers from fans that get stuck out of sync.

## What's the problem this fixes?

If you've used the Lian-Li wireless dongle on Linux you've probably noticed:

1. Even when commanded PWM never changes, your fans periodically spike up and back down — a soft, repetitive "whizzz, whizzz" sound every 1-10 seconds. It's there with `sgtaziz/lian-li-linux` and the upstream of this fork too.
2. Sometimes one set of fans gets stuck at full speed while the others run normally — only a daemon restart pulls them back into sync.

Root causes:

1. **Missing RF keepalive.** Both Linux daemons send PWM commands to fans only when the value *changes*. Once you're at a constant target, no further packets go out. The fan firmware has a short keepalive (~100ms) — when it stops hearing from the dongle, it falls back to a hardware safe-mode (full speed) until the next packet arrives. That's the whiz: brief lapse to safe-mode, then back to commanded.
2. **RF queue overflow + audible "ack" on each packet.** Solving #1 by hammering the dongle continuously causes a different problem — every transmit packet triggers a brief audible response from the fan firmware (looks like a momentary RPM ramp). And firing transactions to multiple fans back-to-back overflows the dongle's queue, so most of the new commands are silently dropped.
3. **Fan-level RF delivery loss.** Even with keepalive working, a single fan can intermittently miss a PWM update (RF interference, dongle queue saturation at a bad moment) and trip its hardware safe-mode. The dongle has no way to know — it thinks it sent the packet successfully. The fan stays at 100% until something forces a fresh, clean re-broadcast (which is what `systemctl restart` was doing).

Windows L-Connect 3 doesn't have any of these issues because it implements the proper RF keepalive protocol that Lian-Li never documented. We can't see that code, so this fork works around it instead.

## What this fork does

The architecture is split into three threads with carefully tuned cadences:

- **`discovery_thread`** (every 2s) — re-scans the dongle for the bound fan list. Also reads each fan's currently-reported PWM and compares it against the daemon's commanded target. If any fan's dongle-side bookkeeping has drifted, requests a resync burst.
- **`temp_thread`** (every 500ms) — reads CPU temp (psutil or a custom shell command, see below) and computes the curve target.
- **`transmit_thread`** (every 20ms outer) — sends the current PWM to all fans, **dual-mode**:
  - **Steady state**: rapid back-to-back transmits to all 4 fan groups, no per-fan gap. The dongle coalesces the constant identical packets and the fans don't see "new" RF events to whir at. Result: silent at constant PWM.
  - **On change** (or on a divergence-driven or periodic resync): switches to a multi-cycle "delivery burst" — 4 cycles, each with a 150ms per-fan gap. This lets the dongle process each fan's transaction cleanly and gets the new value to all 4 fans in ~2.4s. Then automatically back to silent rapid mode.

Additional pieces:

- **Periodic TX device reclaim** (every 30s, configurable). The transmit thread releases the USB TX handle, waits ~400ms for the dongle's internal queue to drain, then re-acquires the handle and fires a fresh delivery burst. This is the programmatic equivalent of `systemctl restart` for just the USB layer — it pulls any fan that's stuck in hardware safe-mode back into sync. Without this, fans can drift indefinitely after a single dropped RF packet.
- **Divergence detection.** When discovery sees a fan whose dongle-reported PWM differs from the commanded target by more than 8 bytes (~3%), it requests a paced delivery burst.
- **Master device filtered from transmits** — the dongle reports an unbound master device with `rx_type=255`. Sending PWM frames to that confuses the dongle. Filtered out, but still kept in `/status` for visibility.
- **Hysteresis (opt-in)** — set `LLCW_HYSTERESIS=5` to absorb micro PWM changes from temperature jitter. Default 0 means every commanded delta triggers a delivery burst, so fan speed tracks temperature 1:1.
- **`cpu_temp_command` config field** — set this to a shell command (or path to a script), and the daemon will execute it every 500ms and parse stdout as the CPU temperature in °C. Useful for testing curves with fake temperatures, blending sensors, or feeding any custom temperature signal you want.

## Trade-offs

- **CPU/USB usage** is higher than upstream — the transmit thread runs continuously at ~50 Hz instead of going idle when nothing changes. On any modern desktop the load is unmeasurable, but it is non-zero.
- **A real temperature change still produces a brief audible ramp** — that's unavoidable, the fans physically need to change speed. What's eliminated is the *idle-state* periodic whiz that has nothing to do with real temperature movement.
- **The periodic TX reclaim is briefly visible.** Every 30s (default) the dongle goes offline for ~400ms then comes back with a delivery burst. If your fans are physically capable of audibly responding to a clean re-broadcast, you'll hear a short blip. Crank `LLCW_PERIODIC_RESYNC_SEC` higher to make it less frequent, or set to `0` to disable entirely — at the cost of fans potentially staying stuck if they ever trip safe-mode.
- **Pump and AIO LCD aren't handled** — same as upstream. This daemon only does fans. If you have a HydroShift II AIO and want pump/LCD control, you'll need a separate solution.

## Install — fresh system (Arch / EndeavourOS)

Tested on EndeavourOS / Arch with kernel 6.x+. If you're on Debian/Ubuntu/Fedora, you can still build from source — see the [upstream README](UPSTREAM_README.md) for those package targets.

```bash
# 1. Build prereqs
sudo pacman -S --needed base-devel git python python-pip

# 2. Clone the fork
git clone https://github.com/hwnprsd/LL-Connect-Wireless ~/src/LL-Connect-Wireless
cd ~/src/LL-Connect-Wireless

# 3. Virtualenv (the compile script uses it; pyinstaller bundles from here)
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Build the Arch package
./arch/compile.sh
# Produces: arch/.result/ll-connect-wireless-<version>-x86_64.pkg.tar.zst

# 5. Install
sudo pacman -U ./arch/.result/ll-connect-wireless-*.pkg.tar.zst

# 6. Enable the user service AND make it survive logouts/reboots
systemctl --user daemon-reload
systemctl --user enable --now ll-connect-wireless.service
sudo loginctl enable-linger $USER

# 7. Verify
systemctl --user status ll-connect-wireless.service
lsusb | grep 0416    # should list both 0416:8040 (TX) and 0416:8041 (RX)
ll-connect-wireless  # live status monitor (Ctrl+C to exit)
```

If the daemon fails to start with `Device 8040 not found`, the TX dongle isn't enumerated on USB. Physically unplug both Lian-Li dongles, plug them back in, then:

```bash
systemctl --user reset-failed ll-connect-wireless.service
systemctl --user start ll-connect-wireless.service
```

## Configuration

Settings are stored at `~/.config/ll-connect-wireless/config.json` and the CLI is the same as upstream. Quick reference:

```bash
# 4-point temperature curve (recommended)
ll-connect-wireless settings set-mode curve
ll-connect-wireless settings curve set-cpu-curve "30:5,60:15,75:60,85:100"
# ^ quiet idle: 5% up to 30°C, 15% at 60°C, 60% at 75°C, 100% at 85°C

# Or a fixed PWM
ll-connect-wireless settings set-mode linear
ll-connect-wireless settings linear set-curve 30           # constant 30%
ll-connect-wireless settings linear set-curve 35:25,80:80  # linear ramp

# Live monitor
ll-connect-wireless

# Daemon status / logs
systemctl --user status ll-connect-wireless.service
journalctl --user -u ll-connect-wireless.service -f
```

### Tuning knobs (environment variables)

All optional. Drop them into a systemd override and the daemon picks them up on restart — no rebuild needed:

```bash
systemctl --user edit ll-connect-wireless.service
```

```ini
[Service]
# How often to do the full TX-handle reclaim + clean burst (seconds).
# Default 30. Set 0 to disable entirely (you'll need manual restarts if
# fans ever get stuck). Lower = faster auto-recovery, more frequent blips.
Environment=LLCW_PERIODIC_RESYNC_SEC=30

# How many PWM bytes a fan's actual reading can differ from its target
# before the daemon fires a corrective burst. Default 8 (~3% of 255).
# Set high (e.g. 999) to disable dongle-side divergence detection.
Environment=LLCW_DIVERGE_THRESHOLD=8

# Hysteresis to absorb micro PWM jitter from temperature noise.
# Default 0 (every delta triggers a burst — fan speed mirrors temp 1:1).
# Set 5 (≈2%) to smooth out small twitches, at the cost of slight lag.
Environment=LLCW_HYSTERESIS=0
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user restart ll-connect-wireless.service
```

### Custom temperature source

Drop in a script (or any shell command) that prints a number in °C. The daemon executes it every 500ms and uses stdout as the CPU temperature:

```bash
cat > ~/.config/ll-connect-wireless/cpu_temp_test.sh <<'EOF'
#!/bin/bash
echo 50
EOF
chmod +x ~/.config/ll-connect-wireless/cpu_temp_test.sh

jq '. + {cpu_temp_command: "/home/'"$USER"'/.config/ll-connect-wireless/cpu_temp_test.sh"}' \
  ~/.config/ll-connect-wireless/config.json > /tmp/cfg.new && \
  mv /tmp/cfg.new ~/.config/ll-connect-wireless/config.json

systemctl --user restart ll-connect-wireless.service
```

To revert to real Tctl: `jq 'del(.cpu_temp_command)' ...` and restart.

## Troubleshooting

**Daemon won't start, says `Device 8040 not found`.** The TX dongle has dropped off USB. Physically re-plug both Lian-Li dongles, run `lsusb | grep 0416` to confirm both `0x8040` and `0x8041` are listed, then `systemctl --user reset-failed ll-connect-wireless.service && systemctl --user start ll-connect-wireless.service`.

**A fan got stuck at full speed.** Wait up to `LLCW_PERIODIC_RESYNC_SEC` seconds (default 30) and the periodic reclaim should pull it back. If it doesn't, drop the interval (`Environment=LLCW_PERIODIC_RESYNC_SEC=10`) or force a manual recovery with `systemctl --user restart ll-connect-wireless.service`.

**Fans still whizzing periodically.** Check `LLCW_PERIODIC_RESYNC_SEC` — every reclaim cycle produces a brief audible ramp. Crank it higher (60, 120) to reduce frequency, or set to 0 to disable.

**Suspend/resume doesn't restart the daemon.** The package installs `/usr/lib/systemd/system-sleep/llcw-resume-restart.sh` for this, but it depends on `loginctl enable-linger $USER` being set. Verify with `loginctl show-user $USER | grep Linger`.

## Credits

- **Upstream**: [Yoinky3000/LL-Connect-Wireless](https://github.com/Yoinky3000/LL-Connect-Wireless) — the Python daemon + CLI this is based on.
- **Protocol research**: [ealcantara22/OpenUniLink](https://github.com/ealcantara22/OpenUniLink) — original reverse-engineering of the Lian-Li wireless dongle protocol.
- **Hardware reference**: Lian Li® L-Connect 3 Windows software (the reference for "what the protocol should look like when it's working right"). Lian Li and L-Connect are trademarks of Lian-Li Industrial Co., Ltd.

## Disclaimer

Not affiliated with, endorsed by, or supported by Lian-Li. This is a reverse-engineered implementation, use at your own risk. If your fans catch fire or your AIO leaks, that's between you and physics.
